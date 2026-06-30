# Document Analyzer

A tool for asynchronous document extraction, analysis, and clustering, featuring a CustomTkinter graphical user interface.

## Quick Start (First-Time Local Setup)

To run the document analyzer on your local machine for the first time, you need to install the core dependencies. Ensure you have a standard Python 3 installation on your host machine.

1. **Install Dependencies:**
   Install the required libraries listed in the provided manifest using pip:
   ```bash
   pip install -r requirements.txt
   ```

2. **Launch the Application:**
   The primary entry point for the application is `main.py` located in the root directory. To launch the graphical user interface, run the following command:
   ```bash
   python main.py
   ```

## Linux Environment Preparation

**Troubleshooting Note for Linux Users:**
Running the graphical user interface on Linux requires the system-level `tkinter` package. Before running the Python application, you must install the `python3-tk` package using your system's package manager. Failure to do so will result in runtime crashes or `ModuleNotFoundError` when the application attempts to render the GUI.

For example, on Debian/Ubuntu-based systems, run:
```bash
sudo apt-get install python3-tk
```
*(Use `dnf`, `pacman`, or your respective package manager on other Linux distributions.)*

## Project Directory Structure

The project business logic and UI modules are structured as follows:

- **`main.py`**: Application entry point. Run this file to start the GUI.
- **`config.py`**: Global configuration constants and ML parameters.
- **`core/`**:
  - `extractor.py`: Asynchronous text extraction from files (PDF, DOCX, CSV, Excel).
  - `analyzer.py`: Machine learning logic (TF-IDF, NMF) for document clustering.
  - `mover.py`: File organization and movement logic.
- **`ui/`**:
  - `app.py`: Primary GUI built with CustomTkinter.
  - `console.py`: Command-line/terminal interactions.
  - `dialogs.py`: Native system dialogs using tkinter.
