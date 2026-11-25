# Core API Reference

This document outlines the core internal workflow and architecture of the Smart Autosorter system, detailing how extraction, analysis, and verification are coordinated.

## Workflow Guide: Extractor and Analyzer

The system uses a two-phase pipeline to convert documents into structured sorting plans:

```mermaid
graph TD
    A["Directory Selection"]
    B(["File Extraction & Generator"])
    C(["Chunked Yielding"])
    D(["Incremental Analyzer (partial_fit)"])
    E(["TF-IDF / Embeddings + K-Means Clustering"])
    F(["Recursive Topic Grouping"])
    G[("Generate Sorting Plan")]
    H["UI Tree Rendering"]
    A -->|DirectoryPath| B
    B -->|FileStream| C
    C -->|ChunkBatch| D
    D -->|FeatureVectors| E
    E -->|TopicClusters| F
    F -->|SortingPlan| G
    G -->|PlanViewModel| H
    click A "docs/user_guide.md#first-run-steps--setup-wizard" "Directory Selection Step"
    click B "docs/user_guide.md#supported-file-formats" "File Extraction Step"
    click C tooltip "Chunked Yielding Step"
    click D tooltip "Analyzer Step"
    click E "docs/user_guide.md#ai-clustering-constraints" "Clustering Step"
    click F tooltip "Topic Grouping Step"
    click G tooltip "Sorting Plan Step"
    click H "docs/ui.md#appuimixinstree_view" "UI Rendering Step"
```
```mermaid
sequenceDiagram
    autonumber
    participant FS as FileScanner
    participant EX as Extractor Engine
    participant SN as Text Sanitizer
    participant CG as Corpus Generator
    FS->>EX: Scan target directory for supported files
    activate EX
    EX->>EX: Extract raw text payload per format
    EX->>SN: Pass raw text payload
    activate SN
    SN->>SN: Sanitize text and filter stop words
    SN-->>EX: Return sanitized text yield
    deactivate SN
    EX-->>CG: Yield document text chunk
    deactivate EX
```
1. **Extraction (`app.core.extractor`)**:
    - The extractor reads raw files across supported office, markup, data, email, and media formats (`.txt`, `.md`, `.rst`, `.log`, `.csv`, `.tsv`, `.json`, `.yaml`, `.yml`, `.html`, `.htm`, `.xml`, `.pdf`, `.docx`, `.pptx`, `.odt`, `.rtf`, `.eml`, `.xlsx`, `.xls`, plus images `.png`, `.jpg`, `.jpeg`, `.bmp`, `.tiff`, `.webp` via OCR and audio `.mp3`, `.wav`, `.m4a` via Whisper when the `ml` extra is installed).
    - It maps each file to its raw text payload using robust exception-handling to ensure that a failure in one document does not crash the entire run, while computing unique SHA-256 digests (`resilient_file_hash`) even for unsupported binary files.
    - `build_corpus_generator` submits files to the shared worker pool and yields the extracted text in chunks of 50 to keep memory use low. `build_corpus_generator_async` is the `asyncio` variant used by the daemon.

2. **Analysis (`app.core.analyzer`)**:
    - Extracted text chunks are fed incrementally into the `IncrementalAnalyzer`.
    - `TfidfVectorizer` transforms the text into sparse vectors; when the optional ONNX model is installed, sentence embeddings are used instead.
    - `MiniBatchKMeans` groups the vectors, and the strategy recurses into each group to build a hierarchical sorting plan with sub-folders where appropriate.
    - Each folder is named from its documents' top TF-IDF terms, or by a local LLM when AI-assisted naming is enabled.

## Multi-Study Clinical Ingestion Pipeline

```mermaid
flowchart TD
    subgraph scan_stage ["Drive Ingestion & Scan"]
        forensic_scan[["app.core.forensic_scanner"]]
        study_disambiguation{app.core.study_disambiguator}
    end
    subgraph taxonomy_stage ["Taxonomy & Compliance Rules"]
        clinical_taxonomy["app.core.clinical_taxonomy"]
        clinical_compliance{app.core.clinical_compliance}
    end
    subgraph binding_stage ["TMF Binder Relocation"]
        clinical_renamer["app.core.clinical_renamer"]
        cro_pipeline_output(["TMF Clean Binders Output"])
    end
    forensic_scan -->|Raw Drives| study_disambiguation
    study_disambiguation -->|Protocols Disambiguated| clinical_taxonomy
    clinical_taxonomy -->|Mapped Taxonomy| clinical_compliance
    clinical_compliance -->|Compliance Validated| clinical_renamer
    clinical_renamer -->|Compiled Binders| cro_pipeline_output
```
## Centralized In-Memory Caching Architecture

```mermaid
flowchart LR
    subgraph core_cache_box ["Centralized LRU Cache (app.core.cache)"]
        bounded_cache[("BoundedMemoryCache[K, V]")]
    end
    subgraph cache_clients ["Subsystem Consumers"]
        db_conn_cache["app.core.db_conn (_connection_cache)"]
        link_manager_cache["app.core.link_manager (_registry)"]
        semantic_cache["app.core.semantic_embeddings (_model_properties_cache)"]
        jev_cache["app.core.jev_classifier (memory_cache)"]
        db_cache["app.core.db (doc_cache)"]
    end
    db_conn_cache -->|Max Size: 50| bounded_cache
    link_manager_cache -->|Max Size: 10000| bounded_cache
    semantic_cache -->|Max Size: 500| bounded_cache
    jev_cache -->|Fast Path Cache| bounded_cache
    db_cache -->|Max Size: 10000| bounded_cache
```
## Concurrency & Worker Pool Model

```mermaid
flowchart TD
    subgraph pool_singleton ["Shared Singleton (app.core.worker_pool)"]
        shared_worker_pool(["SharedWorkerPool Singleton"])
    end
    subgraph pool_tasks ["Concurrent Execution Callers"]
        session_tasks["app.core.session (Lifecycle Management)"]
        mover_tasks["app.core.mover (Parallel File Relocation)"]
        db_worker_tasks["app.core.db_worker (Background Heavy Tasks)"]
        extractor_tasks["app.core.extractor (Parallel Text Ingestion)"]
        analyzer_tasks["app.core.analyzer_strategies (Parallel NLP)"]
    end
    session_tasks -->|"get_instance() / shutdown()"| shared_worker_pool
    mover_tasks -->|"submit() relocation tasks"| shared_worker_pool
    db_worker_tasks -->|offload VLM/OCR/GGUF| shared_worker_pool
    extractor_tasks -->|parallel document extraction| shared_worker_pool
    analyzer_tasks -->|offload embedding vector calculations| shared_worker_pool
```
## Verifier Logic

Before any files are moved, `VerificationEngine.verify_integrity` (`app.core.verifier`) checks the sorting plan and reports problems by category:

- **Collisions**: two files planned for the same destination, or a destination whose parent path is blocked by a file.
- **Circular renames**: moves that would swap or cycle file names.
- **Broken links**: symlinks and Windows shortcuts whose targets would break after the moves.
- **Escaping paths**: destinations that would resolve outside the target folder.
- **Long paths**: destinations longer than 260 characters. The same limit is applied on every OS.
- **Invalid or unconfirmed renames**: proposed names with illegal characters or a changed extension, and renames the user hasn't confirmed.

It does not check free disk space or whether a source file is locked by another program; those failures surface during the move itself.

---

## Module Definitions

### Fast-Path Triage Engine (`app.core.jev_classifier`)

The `JevClassifierEngine` provides ultra-fast, local, non-generative document triage and classification designed to enforce a sub-150ms per-file SLA.

- **In-Engine Fast Snippet Extraction**: When callers omit pre-extracted `text_content`, the engine automatically performs lightweight snippet extraction directly from text, email, rich-text, office, and PDF files under 10MB:
  - `.pdf`: Reads page 0 text (up to 4096 characters) using `pypdf.PdfReader`.
  - `.docx`: Reads section headers, body paragraphs, nested tables, and footers (up to 4096 characters) using `docx.Document`.
  - `.xlsx`: Reads initial rows from sheet 0 (up to 4096 characters) using `openpyxl.load_workbook`.
  - `.xls`: Reads initial rows from sheet 0 (up to 4096 characters) using `xlrd.open_workbook`.
  - `.pptx`: Reads text across slides, grouped shapes, slide tables, and speaker notes (up to 4096 characters) using `pptx.Presentation`.
  - `.eml`, `.rtf`, `.odt`, `.html`/`.htm`, and native markup/tabular files (`.txt`, `.md`, `.rst`, `.csv`, `.tsv`, `.json`, `.yaml`, `.yml`, `.xml`, `.log`): Reads headers and stripped body snippets (up to 4096 characters) without extracting email attachments to disk.
- **Stream Descriptor Resilience**: All document readers accept string or `Path` file paths wrapped in explicit `with open(file_path, "rb") as f:` binary context managers or internal stream lifecycle managers, ensuring file descriptors are released immediately and preventing file handle locking (`WinError 32`) on Windows platforms.
- **Rule-Based Triage & Caching**: Combines category keyword and extension matching with two-tier in-memory caching (`BoundedMemoryCache`), assigning sensitivity ratings, archival priorities, and confidence scores without invoking heavy LLM inference.
