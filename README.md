# Smart AutoSorter AI Pro

Smart AutoSorter AI Pro is a machine-learning based document organization tool.

It extracts text from various file formats, uses TF-IDF and NMF to cluster files by semantic topics, and safely organizes them into a structured directory hierarchy.

## Features
- **Semantic Clustering:** Understands themes in your documents.
- **Robust Extraction:** Supports PDF, DOCX, CSV, and XLSX.
- **Modern UI:** Built with CustomTkinter.

## System Limits & Constraints
To ensure optimal performance and accuracy, Smart AutoSorter AI Pro enforces the following hardcoded constraints:
- **Supported File Formats:** Only `.txt`, `.docx`, `.csv`, `.xlsx`, `.xls`, and `.pdf` files are supported for text extraction and clustering.
- **Minimum File Requirement:** At least 3 supported files are required to enable AI-driven clustering. If fewer files are present, sorting will not proceed optimally.
- **Maximum Folders:** The AI will generate an upper limit of 12 subdirectories for organizing the documents.
- **Miscellaneous Folder:** Any files with insufficient text, low semantic scores, or unreadable data are automatically moved to a fallback `Miscellaneous` folder.

## Installation and Execution

Smart AutoSorter AI Pro is distributed as a zero-config standalone executable. You do not need to install Python, system packages, or manage dependencies.

### Windows
1. Download the `SmartAutoSorter-Windows.exe` from the latest release.
2. Double-click the executable to launch the application instantly.

### Linux
1. Download the `SmartAutoSorter-Linux` binary from the latest release.
2. Make the file executable: `chmod +x SmartAutoSorter-Linux`
3. Run the application: `./SmartAutoSorter-Linux`

For development, you can still run the application from source using `uv run app/main.py`.

## Architecture

The application is structured to strictly separate business logic from the user interface:

- **core/**: Contains the core business logic, text extraction, machine learning models, and file operations.
- **ui/**: Contains graphical interface components, dialogs, and progress rendering.

## Hybrid Quality Guardrails (Testing)

To prevent semantic regressions during development, Smart AutoSorter AI Pro enforces a "Hybrid Quality Guardrail." This test uses deterministic sequential ingestion and low-level mathematical metrics (reconstruction error) to verify clustering quality.

If you make an intentional algorithmic improvement to the analyzer and the `test_quality_guardrails.py` test fails due to the reconstruction error falling outside the +/- 5% tolerance window, you must update the golden baseline:

```bash
UPDATE_BASELINE=1 uv run pytest tests/test_quality_guardrails.py
```
Commit the updated `tests/baseline_metrics.json` file to establish the new expected baseline.

## Security & Privacy

For details regarding our security posture, vulnerability reporting, and network dependencies, please read our [Security Policy](SECURITY.md). 
To understand how your data is processed locally and stored, please refer to our [Privacy Policy](PRIVACY.md).

## Documentation
Check the `docs/` folder for the MkDocs configuration or run `tox -e docs` to build the site.

## Architectural Overview

This repository strictly separates the presentation layer from business logic.

*   **`main.py`**: The application launcher and entry point. It initializes the app via `run_app()` from the UI package.
*   **`config.py`**: Defines global configuration constants, machine learning parameters, and NLP stop words.

### `core/` Package (Business Logic & Intelligence)
This package contains the domain logic and data manipulation features. New extraction or processing logic should be added here.
*   **`extractor.py`**: Data ingestion layer. Parses and extracts text asynchronously from PDFs, Word docs, and Excel files.
*   **`analyzer.py`**: Core intelligence engine. Uses NLP and unsupervised Machine Learning (TF-IDF and NMF) to cluster document themes.
*   **`mover.py`**: Manages physical file organization according to the AI's plan.

### `ui/` Package (Presentation Layer)
This package contains all graphical interface code. Interface updates should be confined to these modules.
*   **`app.py`**: Main graphical user interface built with `customtkinter`.
*   **`console.py`**: Utility functions for printing status updates and visualizing directory structures.
*   **`dialogs.py`**: Helper interface for launching native OS graphical directory selection windows.
