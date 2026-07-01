"""Main application GUI module.

This module provides the main application window and logic for the AutoSorter.
"""

import os
import threading
import time
import webbrowser
from tkinter import filedialog, ttk

import customtkinter as ctk

from app.core.analyzer import IncrementalAnalyzer
from app.core.extractor import build_corpus_generator
from app.core.mover import execute_moves
from app.core.verifier import VerificationEngine

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class AutoSorterApp(ctk.CTk):
    """Main application class for Smart AutoSorter AI Pro."""

    def __init__(self, settings) -> None:
        """Initialize the main application window and UI components."""
        super().__init__()
        self.settings = settings
        self.title("Smart AutoSorter AI Pro")
        self.geometry("750x650")

        self.base_dir: str = ""
        self.plan: dict = {}
        self.locked_files: dict = {}
        self.plan_errors: dict = {}

        self.total_files = 0
        self.completed_files = 0
        self._initial_cached_files = 0
        self.start_time: float = 0.0

        self.analyzer = None
        self.verifier = VerificationEngine()

        self._debounce_timer = None
        self._update_lock = threading.Lock()

        # --- UI Build ---
        self.title_label = ctk.CTkLabel(
            self, text="AI File Organizer Pro", font=("Roboto", 24, "bold")
        )
        self.title_label.pack(pady=15)

        self.settings_btn = ctk.CTkButton(
            self, text="Settings", width=70, command=self.show_settings_modal
        )
        self.settings_btn.place(relx=0.85, rely=0.03)

        self.help_btn = ctk.CTkButton(
            self, text="Help", width=60, command=self.show_help_modal
        )
        self.help_btn.place(relx=0.75, rely=0.03)

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

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def on_close(self):
        """Handle application close event by saving the cache synchronously."""
        if self.base_dir and self.analyzer:
            from app.core.cache import save_cache_sync

            self.status_label.configure(text="Saving cache...", text_color="yellow")
            self.update()
            save_cache_sync(
                self.base_dir,
                self.analyzer.corpus,
                self.locked_files,
                self.analyzer.index_to_word,
            )
        self.destroy()

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

    def show_settings_modal(self) -> None:
        """Display a settings modal to configure dynamic limits."""
        settings_window = ctk.CTkToplevel(self)
        settings_window.title("Settings")
        settings_window.geometry("400x300")
        settings_window.transient(self)
        settings_window.grab_set()

        ctk.CTkLabel(settings_window, text="Max Folders:", font=("Roboto", 14)).pack(
            pady=(20, 5)
        )
        folders_slider = ctk.CTkSlider(
            settings_window, from_=2, to=30, number_of_steps=28
        )
        folders_slider.set(self.settings.MAX_FOLDERS)
        folders_slider.pack(pady=5)

        folders_val = ctk.CTkLabel(settings_window, text=str(self.settings.MAX_FOLDERS))
        folders_val.pack()
        folders_slider.configure(
            command=lambda v: folders_val.configure(text=str(int(v)))
        )

        ctk.CTkLabel(
            settings_window, text="Max Background Workers:", font=("Roboto", 14)
        ).pack(pady=(15, 5))
        workers_slider = ctk.CTkSlider(
            settings_window, from_=1, to=32, number_of_steps=31
        )
        workers_slider.set(self.settings.MAX_WORKERS)
        workers_slider.pack(pady=5)

        workers_val = ctk.CTkLabel(settings_window, text=str(self.settings.MAX_WORKERS))
        workers_val.pack()
        workers_slider.configure(
            command=lambda v: workers_val.configure(text=str(int(v)))
        )

        def apply_settings():
            self.settings.MAX_FOLDERS = int(folders_slider.get())
            self.settings.MAX_WORKERS = int(workers_slider.get())

            if self.analyzer:
                self.analyzer.update_config(self.settings.MAX_FOLDERS)
                # Re-generate plan with new limits if we have data
                if self.analyzer.corpus:
                    self.status_label.configure(
                        text="Applying new settings...", text_color="white"
                    )
                    # Background update avoids freezing UI
                    threading.Thread(
                        target=self._apply_settings_worker, daemon=True
                    ).start()

            settings_window.destroy()

        ctk.CTkButton(settings_window, text="Apply", command=apply_settings).pack(
            pady=20
        )

    def _apply_settings_worker(self):
        with self._update_lock:
            new_plan = self.analyzer.generate_sorting_plan()
            self._apply_locked_files(new_plan)
            self.plan = new_plan

            self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
            has_errors = bool(self.plan_errors)

            self.after(
                0,
                lambda: self.execute_btn.configure(
                    state="disabled" if has_errors else "normal"
                ),
            )
            self.after(
                0,
                lambda: self.status_label.configure(
                    text="AI Plan ready for review.", text_color="green"
                ),
            )
            self.after(0, self.render_tree)

    def show_help_modal(self) -> None:
        """Display a help modal containing system limits and file processing logic by opening the online documentation."""
        webbrowser.open("https://docs.smartautosorter.com/user_guide/#system-limits")

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
            self._initial_cached_files = 0
            self.progress_bar.set(0)
            self.select_btn.configure(state="disabled")
            self.execute_btn.configure(state="disabled")

            self.locked_files = {}
            self.plan = {}
            self.tree.delete(*self.tree.get_children())

            self.analyzer = IncrementalAnalyzer(
                self.settings.MAX_FOLDERS, self.settings.STOP_WORDS
            )

            # --- CACHE INTEGRATION ---
            from app.core.cache import load_cache

            cached_corpus, cached_locked, cached_idx = load_cache(self.base_dir)

            if cached_corpus is not None:
                pruned_corpus = {
                    k: v for k, v in cached_corpus.items() if k in items_to_sort
                }
                self.locked_files = {
                    k: v for k, v in cached_locked.items() if k in pruned_corpus
                }
                self.analyzer.corpus = pruned_corpus
                self.analyzer.index_to_word = cached_idx

                self.completed_files = len(pruned_corpus)
                self._initial_cached_files = self.completed_files
                if self.total_files > 0:
                    self.progress_bar.set(self.completed_files / self.total_files)

                items_to_sort = [f for f in items_to_sort if f not in pruned_corpus]

                if pruned_corpus:
                    new_plan = self.analyzer.generate_sorting_plan()
                    self._apply_locked_files(new_plan)
                    self.plan = new_plan
                    self.render_tree()
                    self._update_progress_ui(self.completed_files / self.total_files)

            if not items_to_sort:
                self._finalize_pipeline()
                return

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
        # Only consider files processed in this session for speed
        session_completed = self.completed_files - getattr(
            self, "_initial_cached_files", 0
        )

        # We need to compute speed accurately
        files_per_second = session_completed / elapsed_time if elapsed_time > 0 else 0
        if files_per_second == 0 and elapsed_time > 0:
            files_per_second = self.completed_files / elapsed_time

        remaining_files = self.total_files - self.completed_files
        eta = remaining_files / files_per_second if files_per_second > 0 else 0

        self.meta_label.configure(
            text=f"Processed: {self.completed_files}/{self.total_files} items | "
            f"Speed: {files_per_second:.1f} files/sec | ETA: {int(eta)}s remaining"
        )

    def pipeline_worker(self, items_to_sort: list) -> None:
        """Run the data collection and ML algorithm incrementally in a background thread."""
        for chunk in build_corpus_generator(
            self.base_dir,
            items_to_sort,
            self.item_completed_callback,
            max_workers=self.settings.MAX_WORKERS,
            chunk_size=50,
        ):
            self.analyzer.partial_fit(chunk)

            new_plan = self.analyzer.generate_sorting_plan()
            self._apply_locked_files(new_plan)
            self.plan = new_plan

            self.after(0, self.render_tree)

        self.after(0, self._finalize_pipeline)

    def _remove_file_from_plan(self, plan_node, filename: str) -> bool:
        """Recursively removes a file from the plan. Returns True if removed."""
        if not isinstance(plan_node, dict) or plan_node.get("__type__") == "file":
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
                    if (
                        part not in current
                        or not isinstance(current[part], dict)
                        or current[part].get("__type__") == "file"
                    ):
                        current[part] = {}

                    target_file_path = os.path.join(target_path, os.path.basename(f))
                    norm_source = os.path.normpath(f)
                    norm_target = os.path.normpath(target_file_path)
                    status = (
                        "Already Sorted"
                        if norm_source == norm_target
                        else "Pending Move"
                    )

                    current[part][f] = {
                        "__type__": "file",
                        "status": status,
                        "source_path": f,
                    }
                else:
                    if (
                        part not in current
                        or not isinstance(current[part], dict)
                        or current[part].get("__type__") == "file"
                    ):
                        current[part] = {}
                    current = current[part]

    def _finalize_pipeline(self):
        """Execute final UI transition after all files are processed."""
        self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
        has_errors = bool(self.plan_errors)
        self.status_label.configure(
            text="AI Plan ready for review.", text_color="green"
        )
        self.execute_btn.configure(state="disabled" if has_errors else "normal")
        self.select_btn.configure(state="normal")
        self.render_tree()

        from app.core.cache import save_cache_async

        save_cache_async(
            self.base_dir,
            self.analyzer.corpus,
            self.locked_files,
            self.analyzer.index_to_word,
        )

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

    def _node_has_errors(self, plan_node):
        if plan_node is None:
            return False
        for k, v in plan_node.items():
            if v is None:
                if k in self.plan_errors:
                    return True
            else:
                if self._node_has_errors(v):
                    return True
        return False

    def _insert_nodes(self, parent_id, plan_node, expanded):
        if not isinstance(plan_node, dict) or plan_node.get("__type__") == "file":
            return

        for name, child_node in plan_node.items():
            if child_node is None or (
                isinstance(child_node, dict) and child_node.get("__type__") == "file"
            ):
                error_msg = self.plan_errors.get(name)
                icon = "❌ " if error_msg else "✅ "
                text = f"{icon}{os.path.basename(name)}"
                if error_msg:
                    text += f" - {error_msg}"

                status = (
                    child_node.get("status", "Pending Move")
                    if isinstance(child_node, dict)
                    else "Pending Move"
                )
                if status == "Already Sorted":
                    text += " [Already Sorted]"

                self.tree.insert(parent_id, "end", iid=f"file:{name}", text=text)
            else:
                folder_id = f"folder:{name}" if not parent_id else f"{parent_id}/{name}"
                count = self._count_files(child_node)
                icon = "❌ " if self._node_has_errors(child_node) else "✅ "
                self.tree.insert(
                    parent_id,
                    "end",
                    iid=folder_id,
                    text=f"{icon}📂 [{name}] ({count} moves)",
                )
                self._insert_nodes(folder_id, child_node, expanded)

                if folder_id in expanded or (not parent_id and len(plan_node) == 1):
                    self.tree.item(folder_id, open=True)

    def _count_files(self, plan_node):
        if plan_node is None:
            return 1
        elif isinstance(plan_node, dict):
            if plan_node.get("__type__") == "file":
                return 0 if plan_node.get("status") == "Already Sorted" else 1
            return sum(self._count_files(v) for v in plan_node.values())
        return 0

    def on_drag_start(self, event):
        """Handle the start of a drag event in the tree view."""
        item = self.tree.identify_row(event.y)
        if item and item.startswith("file:"):
            self.dragged_item = item
        else:
            self.dragged_item = None

    def on_drag_motion(self, event):
        """Handle the motion of a drag event."""
        pass

    def on_drop(self, event):
        """Handle the drop event to move a file in the tree view."""
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
        self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
        has_errors = bool(self.plan_errors)
        self.execute_btn.configure(state="disabled" if has_errors else "normal")

        for item in self.tree.get_children(""):
            self._update_folder_count_recursive(item)

    def _update_folder_count_recursive(self, item):
        if item.startswith("folder:"):
            folder_name = item.split("/")[-1]
            if ":" in folder_name:
                folder_name = folder_name.split(":", 1)[1]

            count = 0
            has_errors = False
            for child in self.tree.get_children(item):
                if child.startswith("file:"):
                    file_key = child.split(":", 1)[1]
                    if file_key in self.plan_errors:
                        has_errors = True

                    text = self.tree.item(child, "text")
                    if "[Already Sorted]" not in text:
                        count += 1
                elif child.startswith("folder:"):
                    c, e = self._update_folder_count_recursive(child)
                    count += c
                    if e:
                        has_errors = True

            icon = "❌ " if has_errors else "✅ "
            self.tree.item(item, text=f"{icon}📂 [{folder_name}] ({count} moves)")
            return count, has_errors
        return 0, False

    def trigger_model_update(self, moved_file: str):
        """Trigger an incremental update of the ML model after a file is moved."""
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

            self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
            has_errors = bool(self.plan_errors)

            # The background update might re-enable/disable the button depending on the resolution.
            self.after(
                0,
                lambda: self.execute_btn.configure(
                    state="disabled" if has_errors else "normal"
                ),
            )

            self.after(0, self.render_tree)

            from app.core.cache import save_cache_async

            save_cache_async(
                self.base_dir,
                self.analyzer.corpus,
                self.locked_files,
                self.analyzer.index_to_word,
            )

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


def run_app(settings) -> None:
    """Instantiate and run the main application."""
    app = AutoSorterApp(settings)
    app.mainloop()
