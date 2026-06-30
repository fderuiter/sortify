"""Main application GUI module.

This module provides the main application window and logic for the AutoSorter.
"""

import os
import threading
import time
from tkinter import filedialog

import customtkinter as ctk
from config import MAX_FOLDERS
from core.analyzer import generate_sorting_plan
from core.extractor import build_corpus
from core.mover import execute_moves

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class AutoSorterApp(ctk.CTk):
    """Main application class for Smart AutoSorter AI Pro.

    Inherits from customtkinter.CTk to provide the main GUI window.
    """

    def __init__(self) -> None:
        """Initialize the main application window and UI components."""
        super().__init__()
        self.title("Smart AutoSorter AI Pro")
        self.geometry("750x600")

        self.base_dir: str = ""
        self.plan: dict = {}
        self.original_items: set = set()

        # Benchmarking / Progress Metrics
        self.total_files = 0
        self.completed_files = 0
        self.start_time: float = 0.0

        # --- UI Build ---
        self.title_label = ctk.CTkLabel(
            self, text="AI File Organizer Pro", font=("Roboto", 24, "bold")
        )
        self.title_label.pack(pady=15)

        self.select_btn = ctk.CTkButton(
            self, text="Select Directory to Sort", command=self.select_directory
        )
        self.select_btn.pack(pady=10)

        self.status_label = ctk.CTkLabel(
            self,
            text="Waiting for directory...",
            text_color="gray",
            font=("Roboto", 13),
        )
        self.status_label.pack(pady=5)

        # Progress Bar Layout
        self.progress_bar = ctk.CTkProgressBar(self, width=500)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)

        self.meta_label = ctk.CTkLabel(
            self, text="", font=("Roboto", 12, "italic"), text_color="cyan"
        )
        self.meta_label.pack(pady=2)

        # Display Preview Box
        self.textbox = ctk.CTkTextbox(self, width=650, height=250, state="normal")
        self.textbox.pack(pady=10)
        self.textbox.bind("<KeyRelease>", self.disable_reclustering)

        self.execute_btn = ctk.CTkButton(
            self,
            text="Approve & Execute Sort",
            command=self.execute_sort,
            fg_color="green",
            hover_color="darkgreen",
            state="disabled",
        )
        self.execute_btn.pack(pady=15)

    def disable_reclustering(self, event=None) -> None:
        """Disable AI re-clustering (directory selection) once manual edits begin."""
        if self.select_btn.cget("state") != "disabled":
            self.select_btn.configure(state="disabled")
            self.status_label.configure(
                text="Manual edits detected. Re-clustering disabled.", text_color="yellow"
            )

    def select_directory(self) -> None:
        """Open a directory selection dialog and initialize processing threads.

        Returns
        -------
        None

        """
        self.base_dir = filedialog.askdirectory(title="Select Directory")
        if self.base_dir:
            items_to_sort = [
                f for f in os.listdir(self.base_dir) if not f.startswith(".")
            ]
            self.original_items = set(items_to_sort)
            self.total_files = len(items_to_sort)

            if self.total_files == 0:
                self.status_label.configure(
                    text="Selected directory is empty.", text_color="red"
                )
                return

            self.completed_files = 0
            self.progress_bar.set(0)
            self.select_btn.configure(state="disabled")
            self.execute_btn.configure(state="disabled")

            self.status_label.configure(
                text="Initializing scanning threads...", text_color="white"
            )
            self.start_time = time.time()

            # FIRE BACKGROUND THREAD: Keeps UI interactive and moving fluidly
            threading.Thread(
                target=self.pipeline_worker, args=(items_to_sort,), daemon=True
            ).start()

    def item_completed_callback(self) -> None:
        """Track execution velocity and update UI progress smoothly.

        Thread-safe counter tracking execution velocity and calculating remaining time.

        Returns
        -------
        None

        """
        self.completed_files += 1
        progress_percentage = self.completed_files / self.total_files
        self.progress_bar.set(progress_percentage)

        # Metrics Calculations
        elapsed_time = time.time() - self.start_time
        files_per_second = (
            self.completed_files / elapsed_time if elapsed_time > 0 else 0
        )
        remaining_files = self.total_files - self.completed_files

        # Calculate Estimated Time Remaining (ETA)
        eta = remaining_files / files_per_second if files_per_second > 0 else 0

        # Push stats updates smoothly to UI
        self.meta_label.configure(
            text=f"Processed: {self.completed_files}/{self.total_files} items | "
            f"Speed: {files_per_second:.1f} files/sec | ETA: {int(eta)}s remaining"
        )

    def pipeline_worker(self, items_to_sort: list) -> None:
        """Run the data collection and ML algorithm in a background thread.

        Parameters
        ----------
        items_to_sort : list
            A list of item names in the directory to be processed.

        Returns
        -------
        None

        """
        # 1. Asynchronous Text Extraction
        corpus = build_corpus(
            self.base_dir, items_to_sort, self.item_completed_callback
        )

        # 2. Transition Status to Processing Phase
        self.status_label.configure(
            text="Data compiled. Modeling semantic themes...", text_color="yellow"
        )

        # 3. Process Topic Clustering
        self.plan = generate_sorting_plan(corpus, MAX_FOLDERS)

        # 4. Render Layout Proposal Tree
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")

        for folder, files in self.plan.items():
            self.textbox.insert("end", f"[{folder}]\n")
            for f in files:
                self.textbox.insert("end", f"{f}\n")
            self.textbox.insert("end", "\n")

        # Reset Interaction States on UI
        self.status_label.configure(
            text="AI Plan ready for review.", text_color="green"
        )
        self.execute_btn.configure(state="normal")
        self.select_btn.configure(state="normal")

    def execute_sort(self) -> None:
        """Execute the physical file moving operations safely based on the generated plan.

        Returns
        -------
        None

        """
        if not self.base_dir:
            return

        text_plan = self.textbox.get("1.0", "end").strip()
        parsed_plan: dict[str, list[str]] = {}
        current_folder = None
        
        illegal_chars = set('\\/:*?"<>|')

        lines = text_plan.split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line in self.original_items:
                if current_folder is None:
                    self.status_label.configure(text="Error: Missing folder headers.", text_color="red")
                    return
                parsed_plan[current_folder].append(line)
            elif line.startswith('[') and line.endswith(']'):
                current_folder = line[1:-1].strip()
                if not current_folder:
                    self.status_label.configure(text="Error: Empty folder name.", text_color="red")
                    return
                if any(c in illegal_chars for c in current_folder):
                    self.status_label.configure(text=f"Error: Illegal characters in folder '{current_folder}'.", text_color="red")
                    return
                if current_folder not in parsed_plan:
                    parsed_plan[current_folder] = []
            else:
                self.status_label.configure(text=f"Error: Unknown or arbitrary file '{line}'.", text_color="red")
                return

        self.plan = parsed_plan

        self.status_label.configure(
            text="Moving files into position...", text_color="white"
        )
        self.execute_btn.configure(state="disabled")
        self.textbox.configure(state="disabled")

        # Execute physical operations safely
        execute_moves(self.base_dir, self.plan)

        self.status_label.configure(
            text="Sorting complete! Check log for skipped/locked files.",
            text_color="green",
        )
        self.meta_label.configure(text="")


def run_app() -> None:
    """Instantiate and run the main application.

    Returns
    -------
    None

    """
    app = AutoSorterApp()
    app.mainloop()
