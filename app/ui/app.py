"""Main application GUI module.

This module provides the main application window and logic for the AutoSorter.
"""

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

import customtkinter as ctk
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import settings
from app.core.analyzer import IncrementalAnalyzer
from app.core.extractor import build_corpus_generator
from app.core.mover import execute_moves
from app.core.verifier import VerificationEngine

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
        self.manual_folders: set = set()
        self.plan_errors: dict = {}

        self.total_files = 0
        self.completed_files = 0
        self.start_time: float = 0.0

        self.analyzer = None
        self.verifier = VerificationEngine()

        self._debounce_timer = None
        self._update_lock = threading.Lock()
        
        self.observer = None
        self._fs_debounce_timer = None
        self._pending_files = set()

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

        self._create_context_menus()
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Button-2>", self.on_right_click)

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
            self.manual_folders = set()
            self.plan = {}
            self.tree.delete(*self.tree.get_children())

            self.analyzer = IncrementalAnalyzer(settings.MAX_FOLDERS)

            self.status_label.configure(
                text="Scanning and modeling incrementally...", text_color="white"
            )
            self.start_time = time.time()
            
            self._start_watcher()

            threading.Thread(
                target=self.pipeline_worker, args=(items_to_sort,), daemon=True
            ).start()

    def _start_watcher(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            
        class Handler(FileSystemEventHandler):
            def __init__(self, app_ref):
                self.app = app_ref
            def on_created(self, event):
                if not event.is_directory:
                    self.app._queue_file(event.src_path)
            def on_modified(self, event):
                if not event.is_directory:
                    self.app._queue_file(event.src_path)
                    
        self.observer = Observer()
        self.observer.schedule(Handler(self), self.base_dir, recursive=True)
        self.observer.start()

    def _queue_file(self, file_path):
        try:
            rel_path = os.path.relpath(file_path, self.base_dir)
            if rel_path.startswith('.'):
                return
                
            with self._update_lock:
                self._pending_files.add(rel_path)
                
            if self._fs_debounce_timer:
                self._fs_debounce_timer.cancel()
            self._fs_debounce_timer = threading.Timer(2.0, self._process_pending_files)
            self._fs_debounce_timer.start()
        except Exception:
            pass
            
    def _process_pending_files(self):
        with self._update_lock:
            files_to_process = list(self._pending_files)
            self._pending_files.clear()
            
        if not files_to_process:
            return
            
        self.after(0, lambda: self.status_label.configure(text="Processing new files...", text_color="cyan"))
        
        # Don't reset completed_files, just add to total
        self.total_files += len(files_to_process)
        
        threading.Thread(
            target=self.pipeline_worker, args=(files_to_process,), daemon=True
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
            self.analyzer.partial_fit(self.base_dir, chunk)

            new_plan = self.analyzer.generate_sorting_plan(self.base_dir)
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
                    if part not in current or not isinstance(current[part], dict) or current[part].get("__type__") == "file":
                        current[part] = {}
                        
                    target_file_path = os.path.join(target_path, os.path.basename(f))
                    norm_source = os.path.normpath(f)
                    norm_target = os.path.normpath(target_file_path)
                    status = "Already Sorted" if norm_source == norm_target else "Pending Move"
                    
                    current[part][f] = {
                        "__type__": "file",
                        "status": status,
                        "source_path": f
                    }
                else:
                    if part not in current or not isinstance(current[part], dict) or current[part].get("__type__") == "file":
                        current[part] = {}
                    current = current[part]

        for folder_path in self.manual_folders:
            parts = folder_path.split("/")
            current = new_plan
            for part in parts:
                if part not in current or not isinstance(current[part], dict) or current[part].get("__type__") == "file":
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
            if child_node is None or (isinstance(child_node, dict) and child_node.get("__type__") == "file"):
                error_msg = self.plan_errors.get(name)
                icon = "❌ " if error_msg else "✅ "
                text = f"{icon}{os.path.basename(name)}"
                if error_msg:
                    text += f" - {error_msg}"
                
                status = child_node.get("status", "Pending Move") if isinstance(child_node, dict) else "Pending Move"
                if status == "Already Sorted":
                    text += " [Already Sorted]"

                self.tree.insert(
                    parent_id, "end", iid=f"file:{name}", text=text
                )
            else:
                folder_id = f"folder:{name}" if not parent_id else f"{parent_id}/{name}"
                count = self._count_files(child_node)
                icon = "❌ " if self._node_has_errors(child_node) else "✅ "
                self.tree.insert(
                    parent_id, "end", iid=folder_id, text=f"{icon}📂 [{name}] ({count} moves)"
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
                    self.base_dir,
                    {moved_file: self.analyzer.corpus[moved_file]}
                )

            new_plan = self.analyzer.generate_sorting_plan(self.base_dir)
            self._apply_locked_files(new_plan)
            self.plan = new_plan
            
            self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
            has_errors = bool(self.plan_errors)
            
            # The background update might re-enable/disable the button depending on the resolution.
            self.after(0, lambda: self.execute_btn.configure(state="disabled" if has_errors else "normal"))

            self.after(0, self.render_tree)

    def _prune_empty_folders(self, plan_node: dict) -> bool:
        if not isinstance(plan_node, dict) or plan_node.get("__type__") == "file":
            return True
            
        keys_to_delete = []
        has_content = False
        for k, v in plan_node.items():
            if v is None:
                has_content = True
            elif not isinstance(v, dict) or v.get("__type__") == "file":
                has_content = True
            else:
                keep = self._prune_empty_folders(v)
                if not keep:
                    keys_to_delete.append(k)
                else:
                    has_content = True
                    
        for k in keys_to_delete:
            del plan_node[k]
            
        return has_content

    def execute_sort(self) -> None:
        """Execute the physical file moving operations safely based on the generated plan."""
        if self.plan and self.base_dir:
            self._prune_empty_folders(self.plan)
            
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


    def _create_context_menus(self):
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="Rename Folder", command=self._rename_folder)
        self.context_menu.add_command(label="Delete Empty Folder", command=self._delete_folder)
        self.context_menu.add_command(label="Create Folder Inside", command=self._create_folder_inside)
        
        self.bg_context_menu = tk.Menu(self, tearoff=0)
        self.bg_context_menu.add_command(label="Create Root Folder", command=self._create_root_folder)
        self.context_item = None

    def on_right_click(self, event):
        """Handle right click events on the tree view."""
        item = self.tree.identify_row(event.y)
        self.context_item = item
        if item and item.startswith("folder:"):
            self.context_menu.tk_popup(event.x_root, event.y_root)
        elif not item:
            self.bg_context_menu.tk_popup(event.x_root, event.y_root)

    def _get_node_by_path(self, path):
        if not path:
            return self.plan
        parts = path.split("/")
        current = self.plan
        for p in parts:
            if p in current and isinstance(current[p], dict) and current[p].get("__type__") != "file":
                current = current[p]
            else:
                return None
        return current

    def _lock_all_files_in_folder(self, node, new_folder_path):
        if not node:
            return
        def _collect_files(n, current_subpath):
            for k, v in n.items():
                if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                    self.locked_files[k] = current_subpath
                elif isinstance(v, dict):
                    _collect_files(v, f"{current_subpath}/{k}")
        _collect_files(node, new_folder_path)

    def _rename_folder(self):
        if not self.context_item:
            return
        current_path = self.context_item.split(":", 1)[1]
        old_name = current_path.split("/")[-1]
        parent_path = "/".join(current_path.split("/")[:-1])
        
        dialog = ctk.CTkInputDialog(text="Enter new folder name:", title="Rename Folder")
        new_name = dialog.get_input()
        if not new_name:
            return
        new_name = new_name.replace("/", "").replace("\\", "")
        if not new_name or new_name == old_name:
            return
            
        parent_node = self._get_node_by_path(parent_path) if parent_path else self.plan
        if parent_node is not None and new_name in parent_node:
            return

        new_path = f"{parent_path}/{new_name}" if parent_path else new_name
        
        node = self._get_node_by_path(current_path)
        self._lock_all_files_in_folder(node, new_path)
        
        new_manual = set()
        for mf in list(self.manual_folders):
            if mf == current_path:
                self.manual_folders.remove(mf)
                new_manual.add(new_path)
            elif mf.startswith(current_path + "/"):
                self.manual_folders.remove(mf)
                new_manual.add(new_path + mf[len(current_path):])
        self.manual_folders.update(new_manual)
        
        for f, target in list(self.locked_files.items()):
            if target == current_path:
                self.locked_files[f] = new_path
            elif target.startswith(current_path + "/"):
                self.locked_files[f] = new_path + target[len(current_path):]
                
        self.manual_folders.add(new_path)
        self._rebuild_plan()

    def _delete_folder(self):
        if not self.context_item:
            return
        current_path = self.context_item.split(":", 1)[1]
        
        node = self._get_node_by_path(current_path)
        if node is not None:
            has_files = False
            def _check(n):
                nonlocal has_files
                for k, v in n.items():
                    if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                        has_files = True
                    elif isinstance(v, dict):
                        _check(v)
            _check(node)
            if has_files:
                return

        if current_path in self.manual_folders:
            self.manual_folders.remove(current_path)
        self._rebuild_plan()

    def _create_folder_inside(self):
        if not self.context_item:
            return
        parent_path = self.context_item.split(":", 1)[1]
        self._prompt_and_create_folder(parent_path)

    def _create_root_folder(self):
        self._prompt_and_create_folder("")

    def _prompt_and_create_folder(self, parent_path):
        if parent_path:
            depth = len(parent_path.split("/"))
            if depth >= 5:
                return
        dialog = ctk.CTkInputDialog(text="Enter new folder name:", title="New Folder")
        new_name = dialog.get_input()
        if not new_name:
            return
        new_name = new_name.replace("/", "").replace("\\", "")
        if not new_name:
            return
        
        parent_node = self._get_node_by_path(parent_path) if parent_path else self.plan
        if parent_node is not None and new_name in parent_node:
            return

        new_path = f"{parent_path}/{new_name}" if parent_path else new_name
        self.manual_folders.add(new_path)
        self._rebuild_plan()

    def _rebuild_plan(self):
        if self.analyzer:
            new_plan = self.analyzer.generate_sorting_plan()
            self._apply_locked_files(new_plan)
            self.plan = new_plan
            self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
            has_errors = bool(self.plan_errors)
            self.execute_btn.configure(state="disabled" if has_errors else "normal")
            self.render_tree()


def run_app() -> None:
    """Instantiate and run the main application."""
    app = AutoSorterApp()
    app.mainloop()
