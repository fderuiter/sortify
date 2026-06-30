# ui/dialogs.py
import tkinter as tk
from tkinter import filedialog


def select_directory():
    """Opens a GUI window for the user to select a target directory."""
    root = tk.Tk()
    root.withdraw()  # Hides the small empty Tkinter baseline window

    print("Please select the target directory you want to analyze and sort...")
    base_dir = filedialog.askdirectory(title="Select Directory to Sort")

    if not base_dir:
        print("No directory selected. Exiting.")
        return None

    return base_dir
