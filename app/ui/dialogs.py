"""Dialog windows for the user interface.

Provides functions to open native OS dialogs.
"""

import tkinter as tk
from tkinter import filedialog
from typing import Optional


def select_directory() -> Optional[str]:
    """Open a GUI window for the user to select a target directory.

    Returns
    -------
    Optional[str]
        The path of the selected directory, or None if no directory was selected.

    """
    root = tk.Tk()
    root.withdraw()  # Hides the small empty Tkinter baseline window

    print("Please select the target directory you want to analyze and sort...")
    base_dir = filedialog.askdirectory(title="Select Directory to Sort")

    if not base_dir:
        print("No directory selected. Exiting.")
        return None

    return base_dir
