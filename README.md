# Smart AutoSorter AI Pro

## Execution

To run the application, use `uv run`:

```bash
uv run app/main.py
```

## Architecture

The application is structured to strictly separate business logic from the user interface:

- **core/**: Contains the core business logic, text extraction, machine learning models, and file operations.
- **ui/**: Contains graphical interface components, dialogs, and progress rendering.

## Platform Dependencies (Linux)

If you are running this application on a Linux system, you must have the `tkinter` system package installed to render the graphical user interface. You can install this on Debian/Ubuntu-based systems using:

```bash
sudo apt-get install python3-tk
```

Ensure you are running the application in a valid graphical display environment.
