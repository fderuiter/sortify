# Core API Reference

This document outlines the core internal workflow and architecture of the Smart Autosorter system, detailing how extraction, analysis, and verification are coordinated.

## Workflow Guide: Extractor and Analyzer

The system uses a two-phase pipeline to convert documents into structured sorting plans:

![Data Flow Architecture](assets/diagrams/architecture_dataflow.svg)

![Multi-Format Text Extraction Flow](assets/diagrams/core_text_extraction.svg)

1. **Extraction (`app.core.extractor`)**:
    - The extractor reads raw files across multiple supported formats (TXT, CSV, PDF, DOCX, XLSX).
    - It maps each file to its raw text payload using robust exception-handling to ensure that a failure in one document does not crash the entire run.
    - An asynchronous generator (`build_corpus_generator`) processes these files concurrently in a thread pool, yielding chunks of extracted text to keep memory usage low.

2. **Analysis (`app.core.analyzer`)**:
    - Extracted text chunks are fed incrementally into the `IncrementalAnalyzer`.
    - `TfidfVectorizer` transforms the text into sparse numerical embeddings.
    - `NMF` is used to perform incremental topic modeling.
    - Finally, a recursive clustering function creates a hierarchical sorting plan based on the dominant topics, identifying sub-topics where appropriate.

## Multi-Study Clinical Ingestion Pipeline

![CRO Multi-Study Pipeline](assets/diagrams/cro_multi_study_pipeline.svg)

## Centralized In-Memory Caching Architecture

![Memory Cache Layers](assets/diagrams/memory_cache_layers.svg)

## Concurrency & Worker Pool Model

![Worker Pool Concurrency](assets/diagrams/worker_pool_concurrency.svg)

## Verifier Logic

Before any files are moved, the `app.core.verifier` ensures the sorting plan is safe and valid. The `VerificationEngine` proactively checks for execution errors:

- **Volume and Disk Space Constraints**: It tracks the expected changes in disk usage across volumes and confirms that sufficient free space exists for the destination directory before attempting a move.
- **Path Length Restrictions**: It prevents operations that would exceed OS-level path limits (e.g., 260 characters on Windows or 4096 characters on Unix systems).
- **File Accessibility**: It validates that the source files exist, are accessible, and are not locked by other processes.

---

## Module Definitions

### Fast-Path Triage Engine (`app.core.jev_classifier`)

The `JevClassifierEngine` provides ultra-fast, local, non-generative document triage and classification designed to enforce a sub-150ms per-file SLA.

- **In-Engine Fast Snippet Extraction**: When callers omit pre-extracted `text_content`, the engine automatically performs lightweight header snippet extraction directly from binary and PDF files under 10MB:
  - `.pdf`: Reads page 0 text (up to 4096 characters) using `pypdf.PdfReader`.
  - `.docx`: Reads initial paragraph text (up to 4096 characters) using `docx.Document`.
  - `.xlsx`: Reads initial rows from sheet 0 (up to 4096 characters) using `openpyxl.load_workbook`.
  - `.xls`: Reads initial rows from sheet 0 (up to 4096 characters) using `xlrd.open_workbook`.
  - `.pptx`: Reads text from slide 0 (up to 4096 characters) using `pptx.Presentation`.
- **Stream Descriptor Resilience**: All document readers accept string or `Path` file paths wrapped in explicit `with open(file_path, "rb") as f:` binary context managers or internal stream lifecycle managers, ensuring file descriptors are released immediately and preventing file handle locking (`WinError 32`) on Windows platforms.
- **Rule-Based Triage & Caching**: Combines category keyword and extension matching with two-tier in-memory caching (`BoundedMemoryCache`), assigning sensitivity ratings, archival priorities, and confidence scores without invoking heavy LLM inference.
