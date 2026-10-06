# Smart AutoSorter AI Pro

Smart AutoSorter AI Pro is a machine-learning based document organization tool.

It extracts text from various file formats, uses TF-IDF and NMF to cluster files by semantic topics, and safely organizes them into a structured directory hierarchy.

## Features
- **Semantic Clustering:** Understands themes in your documents.
- **Robust Extraction:** Supports PDF, DOCX, CSV, and XLSX.
- **Modern UI:** Lightweight terminal user interface and interactive CLI workflows.

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

For core contributors and development setup, please refer to the [Contributor Guide](docs/contributor.md).

## Architecture

The application is structured to strictly separate business logic from the user interface:

- **app/cli/**: Modular CLI subcommand registry and domain-specific command handlers (`ledger`, `quarantine`).
- **app/core/**: Contains the core business logic, security helpers, text extraction, machine learning models, and file operations.
- **app/ui/**: Terminal User Interface (Textual TUI) and notification dispatchers.

## Terminal UI and Visual Snapshot Testing

To catch UI structural and visual regressions before they reach the user, we employ visual snapshot testing frameworks (`tests/test_visual_snapshots.py`).

To run the UI snapshot tests:
```bash
uv run pytest tests/test_visual_snapshots.py
```

If you intentionally alter the structure of the sorting tree or UI components, you must update the golden snapshots:
```bash
UPDATE_SNAPSHOTS=1 uv run pytest tests/test_visual_snapshots.py
```
Commit the updated snapshot files located in `tests/snapshots/` so reviewers can verify the visual and structural differences.
## Security & Privacy

For details regarding our security posture, vulnerability reporting, and network dependencies, please read our [Security Policy](SECURITY.md). 
To understand how your data is processed locally and stored, please refer to our [Privacy Policy](PRIVACY.md).

## Documentation
Check the `docs/` folder for the MkDocs configuration or run `uv run mkdocs build` to build the site.

## Architectural Overview

This repository strictly separates the presentation layer from business logic.

![Core Architecture Diagram](docs/assets/diagrams/core_architecture.svg)

*   **`app/config.py`**: Defines global configuration constants, machine learning parameters, and NLP stop words.

### `app/core/` Package (Business Logic & Intelligence)
This package contains the domain logic and data manipulation features. New extraction or processing logic should be added here.
*   **`app/core/extractor.py`**: Data ingestion layer. Parses and extracts text asynchronously from PDFs, Word docs, and Excel files.
*   **`app/core/analyzer.py`**: Core intelligence engine. Uses NLP and unsupervised Machine Learning (TF-IDF and NMF) to cluster document themes.
*   **`app/core/mover.py`**: Manages physical file organization according to the AI's plan.

### `app/ui/` Package (Presentation Layer)
This package contains all Terminal User Interface code.
*   **`app/ui/tui.py`**: Active Textual terminal user interface and accessibility auditing hooks.
