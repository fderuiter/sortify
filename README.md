# AI-Powered File Organizer

## Setup Instructions

To get started, simply run the setup script for your operating system. The script will automatically install any system dependencies, Python packages, and launch the application.

### Linux
```bash
./setup.sh
```

### Windows
```cmd
setup.bat
```

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
