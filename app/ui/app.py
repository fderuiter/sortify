"""Main application GUI module.

This module provides the main application window and logic for the AutoSorter.
"""

import os
import threading
import time
from tkinter import filedialog, ttk

import customtkinter as ctk
from config import MAX_FOLDERS
from core.analyzer import IncrementalAnalyzer
from core.extractor import build_corpus_generator
from core.mover import execute_moves

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class AutoSorterApp(ctk.CTk):
    """Main application class for Smart AutoSorter AI Pro."""

    def __init__(self) -> None:
        """Initialize the main application window and UI components."""
        super().__init__()
        self.title("Smart AutoSorter AI Pro")
        self.geometry("750x650")

        self.base_dir: str = ""
        self.plan: dict = {}
        self.locked_files: dict = {}

        self.total_files = 0
        self.completed_files = 0
        self.start_time: float = 0.0

        self.analyzer = None

        self._debounce_timer = None
        self._update_lock = threading.Lock()

        # --- UI Build ---
        self.title_label = ctk.CTkLabel(
            self, text="AI File Organizer Pro", font=("Roboto", 24, "bold")
        )
        self.title_label.pack(pady=15)

        self.help_btn = ctk.CTkButton(
            self, text="Help", width=60, command=self.show_help_modal
        )
        self.help_btn.place(relx=0.85, rely=0.03)

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

        self.progress_bar = ctk.CTkProgressBar(self, width=500)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)

        self.meta_label = ctk.CTkLabel(
            self, text="", font=("Roboto", 12, "italic"), text_color="cyan"
        )
        self.meta_label.pack(pady=2)

        self.tree_frame = ctk.CTkFrame(self)
        self.tree_frame.pack(pady=10, fill="both", expand=True, padx=20)

        self.tree = ttk.Treeview(self.tree_frame, show="tree")
        self.tree.pack(fill="both", expand=True, side="left")

        self.scrollbar = ttk.Scrollbar(
            self.tree_frame, orient="vertical", command=self.tree.yview
        )
        self.scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self.scrollbar.set)

        self.tree.bind("<ButtonPress-1>", self.on_drag_start)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drop)
        self.dragged_item = None

        self.execute_btn = ctk.CTkButton(
            self,
            text="Approve & Execute Sort",
            command=self.execute_sort,
            fg_color="green",
            hover_color="darkgreen",
            state="disabled",
        )
        self.execute_btn.pack(pady=15)

    def _get_files_recursively(self, base: str, rel_path: str = "") -> list:
        files = []
        try:
            for entry in os.scandir(os.path.join(base, rel_path)):
                if entry.name.startswith("."):
                    continue
                entry_rel_path = (
                    os.path.join(rel_path, entry.name) if rel_path else entry.name
                )
                if entry.is_dir():
                    files.extend(self._get_files_recursively(base, entry_rel_path))
                else:
                    files.append(entry_rel_path)
        except Exception:
            pass
        return files

    def show_help_modal(self) -> None:
        """Display a help modal containing system limits and file processing logic."""
        help_window = ctk.CTkToplevel(self)
        help_window.title("Help & Information")
        help_window.geometry("500x350")
        help_window.transient(self)
        help_window.grab_set()

        help_text = (
            "Smart AutoSorter AI Pro - Help\n\n"
            "Supported File Formats:\n"
            "• .txt, .docx, .csv, .xlsx, .xls, .pdf\n\n"
            "AI Clustering Constraints:\n"
            "• A minimum of 3 supported files is required to enable AI clustering.\n"
            "• The system will generate a maximum of 12 folders (subdirectories).\n\n"
            "Miscellaneous Folder:\n"
            "• The 'Miscellaneous' folder acts as a fallback for files with insufficient text, "
            "low semantic scores, or unreadable data that the AI cannot confidently categorize."
        )

        text_label = ctk.CTkLabel(
            help_window,
            text=help_text,
            justify="left",
            font=("Roboto", 13),
            wraplength=450,
        )
        text_label.pack(padx=20, pady=20, fill="both", expand=True)

        close_btn = ctk.CTkButton(
            help_window, text="Close", command=help_window.destroy
        )
        close_btn.pack(pady=15)

    def select_directory(self) -> None:
        """Open a directory selection dialog and initialize processing threads."""
        self.base_dir = filedialog.askdirectory(title="Select Directory")
        if self.base_dir:
            items_to_sort = self._get_files_recursively(self.base_dir)
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

            self.locked_files = {}
            self.plan = {}
            self.tree.delete(*self.tree.get_children())

            self.analyzer = IncrementalAnalyzer(MAX_FOLDERS)

            self.status_label.configure(
                text="Scanning and modeling incrementally...", text_color="white"
            )
            self.start_time = time.time()

            threading.Thread(
                target=self.pipeline_worker, args=(items_to_sort,), daemon=True
            ).start()

    def item_completed_callback(self) -> None:
        """Track execution velocity and update UI progress smoothly."""
        self.completed_files += 1
        progress_percentage = self.completed_files / self.total_files
        self.after(0, self._update_progress_ui, progress_percentage)

    def _update_progress_ui(self, progress_percentage):
        self.progress_bar.set(progress_percentage)
        elapsed_time = time.time() - self.start_time
        files_per_second = (
            self.completed_files / elapsed_time if elapsed_time > 0 else 0
        )
        remaining_files = self.total_files - self.completed_files
        eta = remaining_files / files_per_second if files_per_second > 0 else 0

        self.meta_label.configure(
            text=f"Processed: {self.completed_files}/{self.total_files} items | "
            f"Speed: {files_per_second:.1f} files/sec | ETA: {int(eta)}s remaining"
        )

    def pipeline_worker(self, items_to_sort: list) -> None:
        """Run the data collection and ML algorithm incrementally in a background thread."""
        for chunk in build_corpus_generator(
            self.base_dir, items_to_sort, self.item_completed_callback, chunk_size=50
        ):
            self.analyzer.partial_fit(chunk)

            new_plan = self.analyzer.generate_sorting_plan()
            self._apply_locked_files(new_plan)
            self.plan = new_plan

            self.after(0, self.render_tree)

        self.after(0, self._finalize_pipeline)

    def _remove_file_from_plan(self, plan_node, filename: str) -> bool:
        """Recursively removes a file from the plan. Returns True if removed."""
        if not isinstance(plan_node, dict):
            return False

        if filename in plan_node:
            del plan_node[filename]
            return True

        for k in list(plan_node.keys()):
            if self._remove_file_from_plan(plan_node[k], filename):
                if not plan_node[k]:
                    del plan_node[k]
                return True
        return False

    def _apply_locked_files(self, new_plan):
        """Ensure user's manual moves override the AI clustering."""
        for f, target_path in self.locked_files.items():
            self._remove_file_from_plan(new_plan, f)

            parts = target_path.split("/")
            current = new_plan
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    if part not in current or current[part] is None:
                        current[part] = {}
                    current[part][f] = None
                else:
                    if part not in current or current[part] is None:
                        current[part] = {}
                    current = current[part]

    def _finalize_pipeline(self):
        """Final UI transition after all files are processed."""
        self.status_label.configure(
            text="AI Plan ready for review.", text_color="green"
        )
        self.execute_btn.configure(state="normal")
        self.select_btn.configure(state="normal")
        self.render_tree()

    def render_tree(self):
        """Draw the plan on the Treeview, preserving expanded nodes."""
        expanded = set()
        for item in self.tree.get_children(""):
            self._save_expanded(item, expanded)

        self.tree.delete(*self.tree.get_children())
        self._insert_nodes("", self.plan, expanded)

    def _save_expanded(self, item, expanded):
        if self.tree.item(item, "open"):
            expanded.add(item)
        for child in self.tree.get_children(item):
            self._save_expanded(child, expanded)

    def _insert_nodes(self, parent_id, plan_node, expanded):
        if not isinstance(plan_node, dict):
            return

        for name, child_node in plan_node.items():
            if child_node is None:
                self.tree.insert(
                    parent_id, "end", iid=f"file:{name}", text=os.path.basename(name)
                )
            else:
                folder_id = f"folder:{name}" if not parent_id else f"{parent_id}/{name}"
                count = self._count_files(child_node)
                self.tree.insert(
                    parent_id, "end", iid=folder_id, text=f"📂 [{name}] ({count} items)"
                )
                self._insert_nodes(folder_id, child_node, expanded)

                if folder_id in expanded or (not parent_id and len(plan_node) == 1):
                    self.tree.item(folder_id, open=True)

    def _count_files(self, plan_node):
        if plan_node is None:
            return 1
        elif isinstance(plan_node, dict):
            return sum(self._count_files(v) for v in plan_node.values())
        return 0

    def on_drag_start(self, event):
        item = self.tree.identify_row(event.y)
        if item and item.startswith("file:"):
            self.dragged_item = item
        else:
            self.dragged_item = None

    def on_drag_motion(self, event):
        pass

    def on_drop(self, event):
        if not self.dragged_item:
            return

        item = self.tree.identify_row(event.y)
        if not item:
            return

        target_folder = None
        if item.startswith("folder:"):
            target_folder = item.split(":", 1)[1]
        elif item.startswith("file:"):
            parent = self.tree.parent(item)
            if parent.startswith("folder:"):
                target_folder = parent.split(":", 1)[1]

        if target_folder:
            filename = self.dragged_item.split(":", 1)[1]
            current_parent = self.tree.parent(self.dragged_item)
            if current_parent != f"folder:{target_folder}":
                self.tree.move(self.dragged_item, f"folder:{target_folder}", "end")
                self.locked_files[filename] = target_folder

                self._update_folder_counts()
                self.trigger_model_update(filename)

    def _update_folder_counts(self):
        for item in self.tree.get_children(""):
            self._update_folder_count_recursive(item)

    def _update_folder_count_recursive(self, item):
        if item.startswith("folder:"):
            folder_name = item.split("/")[-1]
            if ":" in folder_name:
                folder_name = folder_name.split(":", 1)[1]

            count = 0
            for child in self.tree.get_children(item):
                if child.startswith("file:"):
                    count += 1
                elif child.startswith("folder:"):
                    count += self._update_folder_count_recursive(child)

            self.tree.item(item, text=f"📂 [{folder_name}] ({count} items)")
            return count
        return 0

    def trigger_model_update(self, moved_file: str):
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._debounce_timer = threading.Timer(
            0.5, self._background_model_update, args=(moved_file,)
        )
        self._debounce_timer.start()

    def _background_model_update(self, moved_file: str):
        if self._update_lock.locked():
            return

        with self._update_lock:
            if moved_file in self.analyzer.corpus:
                self.analyzer.partial_fit(
                    {moved_file: self.analyzer.corpus[moved_file]}
                )

            new_plan = self.analyzer.generate_sorting_plan()
            self._apply_locked_files(new_plan)
            self.plan = new_plan

            self.after(0, self.render_tree)

    def execute_sort(self) -> None:
        """Execute the physical file moving operations safely based on the generated plan."""
        if self.plan and self.base_dir:
            self.status_label.configure(
                text="Moving files into position...", text_color="white"
            )
            self.execute_btn.configure(state="disabled")

            execute_moves(self.base_dir, self.plan)

            self.status_label.configure(
                text="Sorting complete! Check log for skipped/locked files.",
                text_color="green",
            )
            self.meta_label.configure(text="")
            self.tree.delete(*self.tree.get_children())


def run_app() -> None:
    """Instantiate and run the main application."""
    app = AutoSorterApp()
    app.mainloop()
