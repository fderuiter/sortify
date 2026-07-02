# Core API Reference

This document outlines the core internal workflow and architecture of the Smart Autosorter system, detailing how extraction, analysis, and verification are coordinated.

## Workflow Guide: Extractor and Analyzer

The system uses a two-phase pipeline to convert documents into structured sorting plans:

1. **Extraction (`app.core.extractor`)**:
    - The extractor reads raw files across multiple supported formats (TXT, CSV, PDF, DOCX, XLSX).
    - It maps each file to its raw text payload using robust exception-handling to ensure that a failure in one document does not crash the entire run.
    - An asynchronous generator (`build_corpus_generator`) processes these files concurrently in a thread pool, yielding chunks of extracted text to keep memory usage low.

2. **Analysis (`app.core.analyzer`)**:
    - Extracted text chunks are fed incrementally into the `IncrementalAnalyzer`.
    - `SentenceTransformer` transforms the text into dense numerical embeddings.
    - `KMeans` is used to perform incremental topic modeling.
    - Finally, a recursive clustering function creates a hierarchical sorting plan based on the dominant topics, identifying sub-topics where appropriate.

## Verifier Logic

Before any files are moved, the `app.core.verifier` ensures the sorting plan is safe and valid. The `VerificationEngine` proactively checks for execution errors:

- **Volume and Disk Space Constraints**: It tracks the expected changes in disk usage across volumes and confirms that sufficient free space exists for the destination directory before attempting a move.
- **Path Length Restrictions**: It prevents operations that would exceed OS-level path limits (e.g., 260 characters on Windows or 4096 characters on Unix systems).
- **File Accessibility**: It validates that the source files exist, are accessible, and are not locked by other processes.

---

## Module Definitions

