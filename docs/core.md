# Core API Reference

This document outlines the core internal workflow and architecture of the Smart Autosorter system, detailing how extraction, analysis, and verification are coordinated.

## Workflow Guide: Extractor and Analyzer

The system uses a two-phase pipeline to convert documents into structured sorting plans:

![Data Flow Architecture](assets/diagrams/architecture_dataflow.svg)

![Multi-Format Text Extraction Flow](assets/diagrams/core_text_extraction.svg)

1. **Extraction (`app.core.extractor`)**:
    - The extractor reads raw files across supported office, markup, data, email, and media formats (`.txt`, `.md`, `.rst`, `.log`, `.csv`, `.tsv`, `.json`, `.yaml`, `.yml`, `.html`, `.htm`, `.xml`, `.pdf`, `.docx`, `.pptx`, `.odt`, `.rtf`, `.eml`, `.xlsx`, `.xls`, plus images `.png`, `.jpg`, `.jpeg`, `.bmp`, `.tiff`, `.webp` via OCR and audio `.mp3`, `.wav`, `.m4a` via Whisper when the `ml` extra is installed).
    - It maps each file to its raw text payload using robust exception-handling to ensure that a failure in one document does not crash the entire run, while computing unique SHA-256 digests (`resilient_file_hash`) even for unsupported binary files.
    - `build_corpus_generator` submits files to the shared worker pool and yields the extracted text in chunks of 50 to keep memory use low, feeding `SortPipeline` across the CLI, TUI, and daemon.

2. **Analysis (`app.core.analyzer`)**:
    - Extracted text chunks are fed incrementally into the `IncrementalAnalyzer`.
    - `TfidfVectorizer` transforms the text into sparse vectors; when the optional ONNX model is installed, sentence embeddings are used instead.
    - `MiniBatchKMeans` groups the vectors, and the strategy recurses into each group to build a hierarchical sorting plan with sub-folders where appropriate.
    - Each folder is named from its documents' top TF-IDF terms, or by a local LLM when AI-assisted naming is enabled.

## Multi-Study Clinical Ingestion Pipeline

![CRO Multi-Study Pipeline](assets/diagrams/cro_multi_study_pipeline.svg)

## Centralized In-Memory Caching Architecture

![Memory Cache Layers](assets/diagrams/memory_cache_layers.svg)

## Concurrency & Worker Pool Model

![Worker Pool Concurrency](assets/diagrams/worker_pool_concurrency.svg)

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
