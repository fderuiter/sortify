"""Main application GUI module.

This module provides the main application window and logic for the AutoSorter.
"""

import os
import re
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

import customtkinter as ctk
from pydantic import ValidationError
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.core.analyzer import IncrementalAnalyzer
from app.core.extractor import build_corpus_generator
from app.core.mover import execute_moves
from app.core.scanner import get_files_recursively
from app.core.verifier import VerificationEngine
from app.ui.settings import SettingsView

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
        self.manual_folders: set = set()
        self.protected_folders: set = set()
        self.plan_errors: dict = {}
        self.expanded_nodes = set()
        self.flat_plan = []
        self.current_start = 0
        self.visible_items = 100

        self.total_files = 0
        self.completed_files = 0
        self._initial_cached_files = 0
        self.start_time: float = 0.0

        self.analyzer = None
        self.verifier = VerificationEngine()

        self._debounce_timer = None
        self._update_lock = threading.Lock()

        self.observer = None
        self._fs_debounce_timer = None
        self._pending_files = set()

        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.settings_frame = ctk.CTkFrame(self, fg_color="transparent")

        self._build_menu()

        self._build_main_ui()
        self._build_settings_ui()

        self.check_setup_wizard()

    def check_setup_wizard(self):
        """Check if the setup wizard needs to be run."""
        from app.config import get_app_dir
        from app.ui.wizard import SetupWizard

        model_dir = get_app_dir() / "model"

        # If model already exists and we haven't explicitely denied, assume OK
        if (model_dir / "config.json").exists():
            if self.settings.AI_CONSENT_GRANTED is None:
                self.settings.AI_CONSENT_GRANTED = True
            self.show_main_view()
            return

        if self.settings.AI_CONSENT_GRANTED is False:
            self.show_main_view()
            return

        # Needs setup
        SetupWizard(self, self.settings, self.show_main_view)

    def _build_menu(self):
        menubar = tk.Menu(self)
        control_menu = tk.Menu(menubar, tearoff=0)
        control_menu.add_command(
            label="Settings & Limits", command=self.show_settings_modal
        )
        control_menu.add_command(
            label="History & Rollback", command=self.show_history_modal
        )
        menubar.add_cascade(label="Control Center", menu=control_menu)
        self.config(menu=menubar)

    def _build_main_ui(self):

        self.title_label = ctk.CTkLabel(
            self.main_frame, text="AI File Organizer Pro", font=("Roboto", 24, "bold")
        )
        self.title_label.pack(pady=15)

        self.help_btn = ctk.CTkButton(
            self.main_frame, text="Help", width=60, command=self.show_help_modal
        )
        self.help_btn.place(relx=0.75, rely=0.03)

        self.select_btn = ctk.CTkButton(
            self.main_frame,
            text="Select Directory to Sort",
            command=self.select_directory,
        )
        self.select_btn.pack(pady=10)

        self.status_label = ctk.CTkLabel(
            self.main_frame,
            text="Waiting for directory...",
            text_color="gray",
            font=("Roboto", 13),
        )
        self.status_label.pack(pady=5)

        self.progress_bar = ctk.CTkProgressBar(self.main_frame, width=500)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=10)

        self.meta_label = ctk.CTkLabel(
            self.main_frame, text="", font=("Roboto", 12, "italic"), text_color="cyan"
        )
        self.meta_label.pack(pady=2)

        self.tree_frame = ctk.CTkFrame(self.main_frame)
        self.tree_frame.pack(pady=10, fill="both", expand=True, padx=20)

        self.tree = ttk.Treeview(self.tree_frame, show="tree")
        self.tree.pack(fill="both", expand=True, side="left")

        self.scrollbar = ttk.Scrollbar(
            self.tree_frame, orient="vertical", command=self.on_scroll
        )
        self.scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=self.scrollbar.set)

        self.tree.bind("<MouseWheel>", self.on_mouse_wheel)
        self.tree.bind("<Button-4>", self.on_mouse_wheel)
        self.tree.bind("<Button-5>", self.on_mouse_wheel)
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click)

        self.tree.bind("<ButtonPress-1>", self.on_drag_start)
        self.tree.bind("<B1-Motion>", self.on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self.on_drop)
        self.dragged_item = None

        self._create_context_menus()
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Button-2>", self.on_right_click)

        self.execute_btn = ctk.CTkButton(
            self.main_frame,
            text="Approve & Execute Sort",
            command=self.execute_sort,
            fg_color="green",
            hover_color="darkgreen",
            state="disabled",
        )
        self.contextual_rename_var = ctk.BooleanVar(
            value=self.settings.CONTEXTUAL_RENAMING
        )
        self.contextual_rename_switch = ctk.CTkSwitch(
            self,
            text="Enable Contextual Renaming",
            variable=self.contextual_rename_var,
            command=self.toggle_contextual_rename,
        )
        self.contextual_rename_switch.pack(pady=5)

        self.preserve_hierarchy_var = ctk.BooleanVar(
            value=self.settings.PRESERVE_HIERARCHY
        )
        self.preserve_hierarchy_switch = ctk.CTkSwitch(
            self,
            text="Preserve Hierarchy",
            variable=self.preserve_hierarchy_var,
            command=self.toggle_preserve_hierarchy,
        )
        self.preserve_hierarchy_switch.pack(pady=5)

        self.execute_btn.pack(pady=15)

        self.settings_btn = ctk.CTkButton(
            self.main_frame,
            text="⚙ Settings",
            width=80,
            command=self.show_settings_view,
        )
        self.settings_btn.place(relx=0.05, rely=0.03)

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
                self.manual_folders,
            )
        self.destroy()

    def _build_settings_ui(self):
        self.settings_view = SettingsView(
            self.settings_frame,
            settings=self.settings,
            on_back=self.show_main_view,
            on_settings_changed=self.on_settings_changed,
            fg_color="transparent",
        )
        self.settings_view.pack(fill="both", expand=True)

    def show_main_view(self):
        """Switch the main interface to the main sorting view."""
        self.settings_frame.pack_forget()
        self.main_frame.pack(fill="both", expand=True)
        if self.plan:
            self.render_tree()

    def show_settings_view(self):
        """Switch the main interface to the settings view."""
        self.main_frame.pack_forget()
        self.settings_frame.pack(fill="both", expand=True)

    def on_settings_changed(self, new_stop_words):
        """Handle updates to application settings like stop words."""
        self.settings.STOP_WORDS = new_stop_words

        if self.analyzer:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                0.5, self._background_settings_update
            )
            self._debounce_timer.start()

    def _background_settings_update(self):
        if self.analyzer:
            self.analyzer.reload_stop_words()
            if self.analyzer.corpus:
                self._background_model_update(None)

    def toggle_contextual_rename(self) -> None:
        """Toggle contextual renaming and refresh the plan if active."""
        self.settings.CONTEXTUAL_RENAMING = self.contextual_rename_var.get()
        if self.plan:
            self.status_label.configure(
                text="Updating plan for contextual renaming...", text_color="white"
            )
            self.execute_btn.configure(state="disabled")

            def _update():
                new_plan = self.analyzer.generate_sorting_plan(
                    self.base_dir, self.settings
                )
                self._apply_locked_files(new_plan)
                self.plan = new_plan

                self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
                has_errors = bool(self.plan_errors)

                self.after(
                    0,
                    lambda: self.status_label.configure(
                        text="AI Plan ready for review.", text_color="green"
                    ),
                )
                self.after(
                    0,
                    lambda: self.execute_btn.configure(
                        state="disabled" if has_errors else "normal"
                    ),
                )
                self.after(0, self.render_tree)

            threading.Thread(target=_update, daemon=True).start()

    def toggle_preserve_hierarchy(self) -> None:
        """Toggle hierarchy preservation and refresh the plan if active."""
        self.settings.PRESERVE_HIERARCHY = self.preserve_hierarchy_var.get()
        if self.plan:
            self.status_label.configure(
                text="Updating plan for hierarchy preservation...", text_color="white"
            )
            self.execute_btn.configure(state="disabled")

            def _update():
                new_plan = self.analyzer.generate_sorting_plan(
                    self.base_dir, self.settings
                )
                self._apply_locked_files(new_plan)
                self.plan = new_plan

                self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
                has_errors = bool(self.plan_errors)

                self.after(
                    0,
                    lambda: self.status_label.configure(
                        text="AI Plan ready for review.", text_color="green"
                    ),
                )
                self.after(
                    0,
                    lambda: self.execute_btn.configure(
                        state="disabled" if has_errors else "normal"
                    ),
                )
                self.after(0, self.render_tree)

            threading.Thread(target=_update, daemon=True).start()

    def _get_files_recursively(self, base: str, rel_path: str = "") -> list:
        return get_files_recursively(base, rel_path)

    def show_settings_modal(self) -> None:
        """Display a settings modal to configure dynamic limits."""
        settings_window = ctk.CTkToplevel(self)
        settings_window.title("Control Center")
        settings_window.geometry("500x650")
        settings_window.transient(self)
        settings_window.grab_set()

        frame = ctk.CTkScrollableFrame(settings_window)
        frame.pack(fill="both", expand=True, padx=10, pady=10)

        def create_slider_group(
            parent, label_text, from_val, to_val, num_steps, initial_val, help_url=None
        ):
            header = ctk.CTkFrame(parent, fg_color="transparent")
            header.pack(pady=(10, 0))

            ctk.CTkLabel(header, text=label_text, font=("Roboto", 14)).pack(side="left")
            if help_url:
                ctk.CTkButton(
                    header,
                    text="?",
                    width=24,
                    height=24,
                    command=lambda url=help_url: webbrowser.open(url),
                ).pack(side="left", padx=10)

            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=5)

            slider = ctk.CTkSlider(
                row, from_=from_val, to=to_val, number_of_steps=num_steps
            )
            slider.set(initial_val)
            slider.pack(side="left", fill="x", expand=True, padx=(0, 10))

            entry_var = tk.StringVar(value=str(int(initial_val)))
            entry = ctk.CTkEntry(row, textvariable=entry_var, width=50)
            entry.pack(side="right")

            def on_slider(*args):
                entry_var.set(str(int(slider.get())))

            slider.configure(command=on_slider)

            def on_entry(*args):
                try:
                    val = int(entry_var.get())
                    if from_val <= val <= to_val:
                        slider.set(val)
                except ValueError:
                    pass

            entry_var.trace_add("write", on_entry)

            return slider, entry_var

        folders_slider, folders_var = create_slider_group(
            frame,
            "Max Folders:",
            2,
            50,
            48,
            self.settings.MAX_FOLDERS,
            help_url="https://docs.smartautosorter.com/user_guide/#system-limits",
        )
        workers_slider, workers_var = create_slider_group(
            frame, "Max Background Workers:", 1, 64, 63, self.settings.MAX_WORKERS
        )

        ctk.CTkLabel(
            frame, text="Advanced AI Parameters", font=("Roboto", 16, "bold")
        ).pack(pady=(20, 5))

        depth_slider, depth_var = create_slider_group(
            frame, "Max Recursion Depth:", 1, 10, 9, self.settings.MAX_DEPTH
        )
        features_slider, features_var = create_slider_group(
            frame, "Max Clustering Features:", 1, 10, 9, self.settings.MAX_FEATURES
        )

        error_label = ctk.CTkLabel(frame, text="", text_color="red")
        error_label.pack(pady=5)

        def reset_ai_defaults():
            depth_slider.set(5)
            depth_var.set("5")
            features_slider.set(3)
            features_var.set("3")

        ctk.CTkButton(
            frame, text="Reset AI Defaults", command=reset_ai_defaults, fg_color="gray"
        ).pack(pady=5)

        def apply_settings():
            try:
                folders = int(folders_var.get())
                workers = int(workers_var.get())
                depth = int(depth_var.get())
                features = int(features_var.get())

                if folders < 1 or workers < 1 or depth < 1 or features < 1:
                    raise ValueError("Values must be positive integers.")

                self.settings.MAX_FOLDERS = folders
                self.settings.MAX_WORKERS = workers
                self.settings.MAX_DEPTH = depth
                self.settings.MAX_FEATURES = features

            except ValidationError as e:
                error_label.configure(
                    text=f"Configuration rejected: {e.errors()[0]['msg']}"
                )
                return
            except ValueError:
                error_label.configure(text="Invalid numeric limits provided.")
                return

            if self.analyzer:
                self.analyzer.max_folders = folders
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

        ctk.CTkButton(frame, text="Apply", command=apply_settings).pack(pady=20)

    def _apply_settings_worker(self):
        with self._update_lock:
            new_plan = self.analyzer.generate_sorting_plan(self.base_dir, self.settings)
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

    def show_history_modal(self) -> None:
        """Display the history and rollback modal."""
        modal = tk.Toplevel(self)
        modal.title("History & Rollbacks")
        modal.geometry("600x400")
        modal.transient(self)
        modal.grab_set()

        import datetime
        from tkinter import messagebox

        from app.core.history import history_manager

        columns = ("session_id", "date", "status")
        tree = ttk.Treeview(modal, columns=columns, show="headings")
        tree.heading("session_id", text="Session ID")
        tree.heading("date", text="Date")
        tree.heading("status", text="Status")

        tree.column("session_id", width=150)
        tree.column("date", width=150)
        tree.column("status", width=100)

        tree.pack(fill="both", expand=True, padx=10, pady=10)

        sessions = history_manager.get_sessions()
        for s in sessions:
            dt = datetime.datetime.fromtimestamp(s["timestamp"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            tree.insert(
                "",
                "end",
                iid=s["session_id"],
                values=(s["session_id"][:8] + "...", dt, s["status"]),
            )

        def on_rollback():
            selected = tree.selection()
            if not selected:
                messagebox.showwarning("Warning", "No session selected.", parent=modal)
                return
            session_id = selected[0]
            s_data = next((x for x in sessions if x["session_id"] == session_id), None)
            if s_data and s_data["status"] == "rolled_back":
                messagebox.showinfo(
                    "Info", "Session already rolled back.", parent=modal
                )
                return

            try:
                missing = history_manager.check_missing_files(session_id)
                if missing:
                    msg = f"Cannot rollback! {len(missing)} files missing (e.g. {missing[0]}). Proceed?"
                    ans = messagebox.askyesno(
                        "Missing Files Warning", msg, parent=modal
                    )
                    if not ans:
                        return

                history_manager.rollback(session_id, ignore_missing=True)
            except Exception as e:
                messagebox.showerror("Rollback Failed", str(e), parent=modal)
                return

            messagebox.showinfo("Success", "Rollback successful!", parent=modal)

            modal.destroy()

            if self.base_dir == s_data["base_dir"]:
                self.plan = {}
                self.render_tree()
                self.execute_btn.configure(state="disabled")

        btn_frame = ctk.CTkFrame(modal)
        btn_frame.pack(fill="x", padx=10, pady=10)

        rollback_btn = ctk.CTkButton(
            btn_frame,
            text="Rollback Selected",
            command=on_rollback,
            fg_color="red",
            hover_color="darkred",
        )
        rollback_btn.pack(side="right")

    def show_help_modal(self) -> None:
        """Display a help modal containing system limits and file processing logic by opening the online documentation."""
        webbrowser.open("https://docs.smartautosorter.com/user_guide/#system-limits")

    def select_directory(self) -> None:
        """Open a directory selection dialog and initialize processing threads."""
        self.base_dir = filedialog.askdirectory(title="Select Directory")
        if self.base_dir:
            self.total_files = 0
            self.completed_files = 0
            self._initial_cached_files = 0
            self.progress_bar.set(0)
            self.select_btn.configure(state="disabled")
            self.execute_btn.configure(state="disabled")

            self.locked_files = {}
            self.manual_folders = set()
            self.protected_folders = set()
            self.plan = {}
            self.expanded_nodes = set()
            self.flat_plan = []
            self.current_start = 0
            self.tree.delete(*self.tree.get_children())

            from app.config import get_app_dir

            user_model_path = get_app_dir() / "model"

            if self.settings.AI_CONSENT_GRANTED is True:
                model_path = str(user_model_path)
            else:
                model_path = None

            self.analyzer = IncrementalAnalyzer(
                self.settings.MAX_FOLDERS,
                self.settings.STOP_WORDS,
                model_path=model_path,
            )

            self.status_label.configure(
                text="Scanning directory...", text_color="white"
            )

            threading.Thread(target=self._scan_and_process_worker, daemon=True).start()

    def _scan_and_process_worker(self):
        items_to_sort = self._get_files_recursively(self.base_dir)
        self.total_files = len(items_to_sort)

        if self.total_files == 0:
            self.after(
                0,
                lambda: self.status_label.configure(
                    text="Selected directory is empty.", text_color="red"
                ),
            )
            self.after(0, lambda: self.select_btn.configure(state="normal"))
            return

        from app.core.cache import load_cache

        cached_corpus, cached_locked, cached_idx, cached_manual_folders = load_cache(
            self.base_dir
        )

        if cached_corpus is not None:
            pruned_corpus = {
                k: v for k, v in cached_corpus.items() if k in items_to_sort
            }
            self.locked_files = {
                k: v for k, v in cached_locked.items() if k in pruned_corpus
            }
            self.manual_folders = (
                cached_manual_folders if cached_manual_folders is not None else set()
            )
            self.analyzer.corpus = pruned_corpus
            self.analyzer.index_to_word = cached_idx

            self.completed_files = len(pruned_corpus)
            self._initial_cached_files = self.completed_files
            if self.total_files > 0:
                self.after(
                    0,
                    lambda: self.progress_bar.set(
                        self.completed_files / self.total_files
                    ),
                )

            items_to_sort = [f for f in items_to_sort if f not in pruned_corpus]

            if pruned_corpus:
                new_plan = self.analyzer.generate_sorting_plan(
                    self.base_dir, self.settings
                )
                self.after(0, lambda p=new_plan: self._apply_locked_files(p))
                self.plan = new_plan
                self.after(0, self.render_tree)
                self.after(
                    0,
                    lambda: self._update_progress_ui(
                        self.completed_files / self.total_files
                    ),
                )

        if not items_to_sort:
            self.after(0, self._finalize_pipeline)
            return

        self.after(
            0,
            lambda: self.status_label.configure(
                text="Scanning and modeling incrementally...", text_color="white"
            ),
        )

        # Move start_time here to track actual work
        self.start_time = time.time()
        self.after(0, self._start_watcher)

        # Run pipeline_worker in this background thread
        self.pipeline_worker(items_to_sort)

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

            def on_moved(self, event):
                if not event.is_directory:
                    self.app._queue_move(event.src_path, event.dest_path)

            def on_deleted(self, event):
                if not event.is_directory:
                    self.app._queue_delete(event.src_path)

        self.observer = Observer()
        self.observer.schedule(Handler(self), self.base_dir, recursive=True)
        self.observer.start()

    def _queue_file(self, file_path):
        try:
            rel_path = os.path.relpath(file_path, self.base_dir)
            if rel_path.startswith("."):
                return

            with self._update_lock:
                self._pending_files.add(rel_path)

            if self._fs_debounce_timer:
                self._fs_debounce_timer.cancel()
            self._fs_debounce_timer = threading.Timer(2.0, self._process_pending_files)
            self._fs_debounce_timer.start()
        except Exception:
            pass

    def _queue_move(self, src_path, dest_path):
        try:
            from app.core.db import db
            rel_src = os.path.relpath(src_path, self.base_dir)
            rel_dest = os.path.relpath(dest_path, self.base_dir)
            if rel_src.startswith(".") or rel_dest.startswith("."):
                return
            
            with self._update_lock:
                if self.analyzer and rel_src in self.analyzer.corpus:
                    self.analyzer.corpus[rel_dest] = self.analyzer.corpus.pop(rel_src)
                if rel_src in self.locked_files:
                    self.locked_files[rel_dest] = self.locked_files.pop(rel_src)
            
            db.update_document_path(self.base_dir, rel_src, rel_dest)
            
            # Re-run plan generation
            self._queue_file(dest_path)
        except Exception:
            pass

    def _queue_delete(self, file_path):
        try:
            from app.core.db import db
            rel_path = os.path.relpath(file_path, self.base_dir)
            if rel_path.startswith("."):
                return
            
            with self._update_lock:
                if self.analyzer and rel_path in self.analyzer.corpus:
                    del self.analyzer.corpus[rel_path]
                if rel_path in self.locked_files:
                    del self.locked_files[rel_path]
            
            db.remove_document(self.base_dir, rel_path)
            
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

        self.after(
            0,
            lambda: self.status_label.configure(
                text="Processing new files...", text_color="cyan"
            ),
        )

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
            active_model_name=self.analyzer.active_model_name,
            active_dimension=self.analyzer.active_dimension,
        ):
            self.analyzer.partial_fit(self.base_dir, chunk)

            new_plan = self.analyzer.generate_sorting_plan(self.base_dir, self.settings)
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

                    filename = os.path.basename(f)
                    target_filename = filename
                    if self.settings.CONTEXTUAL_RENAMING:
                        parent_dir = os.path.dirname(f)
                        if parent_dir:
                            parent_folder = os.path.basename(parent_dir)
                            if parent_folder:
                                safe_parent = re.sub(
                                    r"[^A-Za-z0-9]", "_", parent_folder
                                )
                                target_filename = f"{safe_parent}_{filename}"

                    target_file_path = os.path.join(target_path, target_filename)
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
                        "target_filename": target_filename,
                    }
                else:
                    if (
                        part not in current
                        or not isinstance(current[part], dict)
                        or current[part].get("__type__") == "file"
                    ):
                        current[part] = {}
                    current = current[part]

        for folder_path in self.manual_folders:
            parts = folder_path.split("/")
            current = new_plan
            for part in parts:
                if (
                    part not in current
                    or not isinstance(current[part], dict)
                    or current[part].get("__type__") == "file"
                ):
                    current[part] = {}
                current = current[part]

        self._inject_deletion_candidates(new_plan)

    def _inject_deletion_candidates(self, plan):
        """Find source directories that will be empty and add them to the plan."""
        if not self.base_dir or not os.path.exists(self.base_dir):
            return

        def _get_files_in_plan(node):
            files = []
            for k, v in node.items():
                if isinstance(v, dict) and v.get("__type__") == "file":
                    files.append(v)
                elif isinstance(v, dict) and v.get("__type__") not in (
                    "file",
                    "directory",
                ):
                    files.extend(_get_files_in_plan(v))
            return files

        plan_files = _get_files_in_plan(plan)
        moving_sources = {
            f["source_path"] for f in plan_files if f.get("status") != "Already Sorted"
        }

        all_dirs = set()
        files_remaining = set()

        for root, dirs, files in os.walk(self.base_dir):
            rel_root = os.path.relpath(root, self.base_dir)
            if rel_root == ".":
                rel_root = ""

            for d in dirs:
                path = (
                    os.path.normpath(os.path.join(rel_root, d)).replace("\\", "/")
                    if rel_root
                    else d
                )
                all_dirs.add(path)

            for f in files:
                path = (
                    os.path.normpath(os.path.join(rel_root, f)).replace("\\", "/")
                    if rel_root
                    else f
                )
                if path not in moving_sources:
                    files_remaining.add(path)

        non_empty_dirs = set()
        for f in files_remaining:
            p = os.path.dirname(f)
            while p:
                non_empty_dirs.add(p)
                p = os.path.dirname(p)

        plan_keys = set(plan.keys())

        for d in all_dirs:
            if d in non_empty_dirs:
                continue

            top_level = d.split("/")[0]
            if top_level in plan_keys:
                continue

            protected = d in getattr(self, "protected_folders", set())
            plan[d] = {
                "__type__": "directory",
                "status": "Kept" if protected else "To Be Deleted",
                "protected": protected,
                "source_path": os.path.join(self.base_dir, d),
            }

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
            self.manual_folders,
        )

    def render_tree(self):
        """Draw the plan on the Treeview, preserving expanded nodes."""
        self.flat_plan = self._flatten(self.plan, "", 0)
        self.update_tree_view()

    def _flatten(self, node, path, depth):
        flat = []
        if not isinstance(node, dict) or node.get("__type__") in ("file", "directory"):
            return flat

        for k, v in node.items():
            is_file = (v is None) or (
                isinstance(v, dict) and v.get("__type__") in ("file", "directory")
            )
            node_id = (
                f"file:{k}" if is_file else (f"{path}/{k}" if path else f"folder:{k}")
            )

            flat.append(
                {
                    "id": node_id,
                    "name": k,
                    "node": v,
                    "depth": depth,
                    "is_file": is_file,
                    "parent_id": path or "",
                }
            )
            if not is_file and node_id in self.expanded_nodes:
                flat.extend(self._flatten(v, node_id, depth + 1))
        return flat

    def update_tree_view(self):
        """Update the visible portion of the tree view based on scroll position."""
        self.tree.delete(*self.tree.get_children())
        total = len(self.flat_plan)
        if total == 0:
            return

        self.current_start = max(
            0, min(self.current_start, total - min(self.visible_items, total))
        )
        visible = self.flat_plan[
            self.current_start : self.current_start + self.visible_items
        ]

        for item in visible:
            indent = "    " * item["depth"]
            if item["is_file"]:
                node = item["node"]
                if isinstance(node, dict) and node.get("__type__") == "directory":
                    display_name = item["name"]
                    status = node.get("status")
                    if not getattr(self.settings, "CLEANUP_EMPTY_FOLDERS", True):
                        icon = "📁 "
                        text = f"{indent}{icon}{display_name} [Skipped by Settings]"
                    elif status == "To Be Deleted":
                        icon = "🗑️ "
                        text = f"{indent}{icon}{display_name} [To Be Deleted]"
                    else:
                        icon = "📁 "
                        text = f"{indent}{icon}{display_name} [Kept]"
                    self.tree.insert("", "end", iid=item["id"], text=text)
                    continue

                error_msg = self.plan_errors.get(item["name"])
                icon = "❌ " if error_msg else "✅ "
                display_name = (
                    item["node"].get("target_filename", item["name"])
                    if isinstance(item["node"], dict)
                    else item["name"]
                )
                text = f"{indent}{icon}{display_name}"
                if isinstance(item["node"], dict):
                    if item["node"].get("is_conflicted"):
                        compliance = item["node"].get("compliance_path", "Unknown")
                        historical = item["node"].get("historical_path", "Unknown")
                        text += f" [⚠️ CONFLICT: Compliance={compliance} | Historical={historical}]"
                        
                    routed_by = item["node"].get("routed_by")
                    if routed_by == "keyword":
                        kw = item["node"].get("keyword", "")
                        text += f" [Keyword: {kw}]"
                    elif routed_by == "pattern":
                        kw = item["node"].get("keyword", "")
                        text += f" [Pattern: {kw}]"
                    
                    ext_status = item["node"].get("extraction_status")
                    if ext_status == "EMPTY":
                        text += " [No text found]"
                    elif ext_status == "ENCRYPTED":
                        text += " [Encrypted]"
                    elif ext_status == "UNSUPPORTED":
                        text += " [Unsupported format: Files of this type cannot be read]"
                    elif ext_status == "FAILED":
                        text += " [Extraction failed]"
                if error_msg:
                    text += f" - {error_msg}"
                status = (
                    item["node"].get("status", "Pending Move")
                    if isinstance(item["node"], dict)
                    else "Pending Move"
                )
                if status == "Already Sorted":
                    text += " [Already Sorted]"

                self.tree.insert("", "end", iid=item["id"], text=text)
            else:
                count = self._count_files(item["node"])
                icon = "❌ " if self._node_has_errors(item["node"]) else "✅ "
                chevron = "▼ " if item["id"] in self.expanded_nodes else "▶ "
                self.tree.insert(
                    "",
                    "end",
                    iid=item["id"],
                    text=f"{indent}{chevron}{icon}📂 [{item['name']}] ({count} moves)",
                )

        self.scrollbar.set(
            self.current_start / total, (self.current_start + len(visible)) / total
        )

    def _node_has_errors(self, plan_node):
        if plan_node is None:
            return False
        if isinstance(plan_node, dict) and plan_node.get("__type__") in (
            "file",
            "directory",
        ):
            return False

        for k, v in plan_node.items():
            if v is None:
                if k in self.plan_errors:
                    return True
            else:
                if self._node_has_errors(v):
                    return True
        return False

    def _count_files(self, plan_node):
        if plan_node is None:
            return 1
        elif isinstance(plan_node, dict):
            if plan_node.get("__type__") == "file":
                return 0 if plan_node.get("status") == "Already Sorted" else 1
            if plan_node.get("__type__") == "directory":
                return 0
            return sum(self._count_files(v) for v in plan_node.values())
        return 0

    def on_scroll(self, *args):
        """Handle scroll events for the virtualized tree view."""
        if not self.flat_plan:
            return
        total = len(self.flat_plan)
        if args[0] == "moveto":
            self.current_start = int(float(args[1]) * total)
        elif args[0] == "scroll":
            self.current_start += int(args[1]) * (
                self.visible_items if args[2] == "pages" else 1
            )

        self.current_start = max(
            0, min(self.current_start, total - min(self.visible_items, total))
        )
        self.update_tree_view()

    def on_mouse_wheel(self, event):
        """Handle mouse wheel scroll events for the tree view."""
        if not self.flat_plan:
            return
        if event.num == 4 or event.delta > 0:
            self.current_start -= 1
        elif event.num == 5 or event.delta < 0:
            self.current_start += 1

        total = len(self.flat_plan)
        self.current_start = max(
            0, min(self.current_start, total - min(self.visible_items, total))
        )
        self.update_tree_view()
        return "break"

    def on_tree_click(self, event):
        """Handle click events on the tree view, expanding or collapsing folders, or showing conflict menu."""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item.startswith("folder:") or ("/" in item and not item.startswith("file:")):
            if item in self.expanded_nodes:
                self.expanded_nodes.remove(item)
            else:
                self.expanded_nodes.add(item)
            self.render_tree()
        elif item.startswith("file:"):
            node = next((x["node"] for x in self.flat_plan if x["id"] == item), None)
            if node and isinstance(node, dict) and node.get("is_conflicted"):
                conflict_menu = tk.Menu(self, tearoff=0)
                compliance_path = node.get("compliance_path", "Unknown")
                historical_path = node.get("historical_path", "Unknown")
                conflict_menu.add_command(
                    label=f"Route to Compliance: {compliance_path}",
                    command=lambda p=compliance_path: self._resolve_conflict(item, p)
                )
                conflict_menu.add_command(
                    label=f"Route to Historical: {historical_path}",
                    command=lambda p=historical_path: self._resolve_conflict(item, p)
                )
                conflict_menu.tk_popup(event.x_root, event.y_root)

    def get_node_parent(self, item_id):
        """Retrieve the parent ID for a given node ID in the flattened plan."""
        for x in self.flat_plan:
            if x["id"] == item_id:
                return x["parent_id"]
        return ""

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
            parent = self.get_node_parent(item)
            if parent.startswith("folder:") or "/" in parent:
                target_folder = parent.split(":", 1)[1] if ":" in parent else parent

        if target_folder:
            filename = self.dragged_item.split(":", 1)[1]
            current_parent = self.get_node_parent(self.dragged_item)
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

    def _background_model_update(self, moved_file: str | None = None):
        if self._update_lock.locked():
            return

        with self._update_lock:
            if moved_file in self.analyzer.corpus:
                text = self.analyzer.corpus[moved_file]
                self.analyzer.partial_fit(
                    self.base_dir, {moved_file: text}
                )
                
                if text.startswith("[STATUS:") or text == "":
                    base_name = os.path.basename(moved_file)
                    name_without_ext = os.path.splitext(base_name)[0]
                    import re
                    words = re.findall(r'[a-zA-Z0-9]+', name_without_ext)
                    words = [w.lower() for w in words if len(w) > 2 and w.lower() not in self.settings.STOP_WORDS]
                    
                    target = self.locked_files.get(moved_file)
                    if target and words:
                        learned_rules = dict(getattr(self.settings, "LEARNED_RULES", {}))
                        for w in words:
                            learned_rules[w] = target
                        self.settings.LEARNED_RULES = learned_rules

            new_plan = self.analyzer.generate_sorting_plan(self.base_dir, self.settings)
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
                self.manual_folders,
            )

    def _prune_empty_folders(self, plan_node: dict) -> bool:
        if not isinstance(plan_node, dict) or plan_node.get("__type__") in (
            "file",
            "directory",
        ):
            return True

        keys_to_delete = []
        has_content = False
        for k, v in plan_node.items():
            if v is None:
                has_content = True
            elif not isinstance(v, dict) or v.get("__type__") in ("file", "directory"):
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

            if self.verifier.has_cloud_targets(self.base_dir, self.plan):
                dialog = ctk.CTkToplevel(self)
                dialog.title("Privacy Warning")
                dialog.geometry("450x250")
                dialog.transient(self)
                dialog.grab_set()

                label = ctk.CTkLabel(
                    dialog,
                    text="WARNING: Cloud Sync Detected!\n\nFiles moved to this location will be uploaded\nto third-party servers. This breaks the 100% local\nprivacy promise.\n\nDo you want to proceed?",
                    font=("Roboto", 14),
                    justify="center",
                )
                label.pack(pady=20)

                result = tk.BooleanVar(value=False)

                def on_proceed():
                    result.set(True)
                    dialog.destroy()

                def on_cancel():
                    result.set(False)
                    dialog.destroy()

                dialog.protocol("WM_DELETE_WINDOW", on_cancel)

                btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
                btn_frame.pack(fill="x", pady=10)

                proceed_btn = ctk.CTkButton(
                    btn_frame,
                    text="Proceed",
                    command=on_proceed,
                    fg_color="green",
                    hover_color="darkgreen",
                )
                proceed_btn.pack(side="left", padx=30, expand=True)

                cancel_btn = ctk.CTkButton(
                    btn_frame,
                    text="Cancel",
                    command=on_cancel,
                    fg_color="red",
                    hover_color="darkred",
                )
                cancel_btn.pack(side="right", padx=30, expand=True)

                self.wait_window(dialog)

                if not result.get():
                    return

            self.status_label.configure(
                text="Moving files into position...", text_color="white"
            )
            self.execute_btn.configure(state="disabled")

            # Pause filesystem observer so execution moves aren't tracked as manual moves
            if self.observer:
                self.observer.stop()
                self.observer.join()
                self.observer = None

            summary = execute_moves(self.base_dir, self.plan, self.settings)
            
            # Restart observer
            self._start_watcher()

            deleted = summary.get("deleted_folders", 0)
            protected = summary.get("protected_folders", 0)

            self.status_label.configure(
                text="Sorting complete! Check log for skipped/locked files.",
                text_color="green",
            )
            self.meta_label.configure(
                text=f"Cleanup Summary: {deleted} folders deleted | {protected} folders kept"
            )
            self.tree.delete(*self.tree.get_children())

    def _create_context_menus(self):
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(
            label="Rename Folder", command=self._rename_folder
        )
        self.context_menu.add_command(
            label="Delete Empty Folder", command=self._delete_folder
        )
        self.context_menu.add_command(
            label="Create Folder Inside", command=self._create_folder_inside
        )

        self.bg_context_menu = tk.Menu(self, tearoff=0)
        self.bg_context_menu.add_command(
            label="Create Root Folder", command=self._create_root_folder
        )
        self.candidate_context_menu = tk.Menu(self, tearoff=0)
        self.candidate_context_menu.add_command(
            label="Toggle Keep Folder", command=self._toggle_keep_folder
        )
        self.context_item = None

    def on_right_click(self, event):
        """Handle right click events on the tree view."""
        item = self.tree.identify_row(event.y)
        self.context_item = item
        if item and item.startswith("folder:"):
            self.context_menu.tk_popup(event.x_root, event.y_root)
        elif item and item.startswith("file:"):
            node = next((x["node"] for x in self.flat_plan if x["id"] == item), None)
            if node and isinstance(node, dict):
                if node.get("is_conflicted"):
                    conflict_menu = tk.Menu(self, tearoff=0)
                    compliance_path = node.get("compliance_path", "Unknown")
                    historical_path = node.get("historical_path", "Unknown")
                    conflict_menu.add_command(
                        label=f"Route to Compliance: {compliance_path}",
                        command=lambda p=compliance_path: self._resolve_conflict(item, p)
                    )
                    conflict_menu.add_command(
                        label=f"Route to Historical: {historical_path}",
                        command=lambda p=historical_path: self._resolve_conflict(item, p)
                    )
                    conflict_menu.tk_popup(event.x_root, event.y_root)
                elif node.get("__type__") == "directory":
                    self.candidate_context_menu.tk_popup(event.x_root, event.y_root)
        elif not item:
            self.bg_context_menu.tk_popup(event.x_root, event.y_root)

    def _resolve_conflict(self, item, selected_path):
        filename = item.split(":", 1)[1]
        self.locked_files[filename] = selected_path
        from app.core.cache import save_cache_async
        save_cache_async(
            self.base_dir,
            self.analyzer.corpus,
            self.locked_files,
            self.analyzer.index_to_word,
            self.manual_folders,
        )
        self._rebuild_plan()

    def _toggle_keep_folder(self):
        if not self.context_item or not self.context_item.startswith("file:"):
            return

        node = next(
            (x["node"] for x in self.flat_plan if x["id"] == self.context_item), None
        )
        if (
            not node
            or not isinstance(node, dict)
            or node.get("__type__") != "directory"
        ):
            return

        folder_path = self.context_item.split(":", 1)[1]
        if folder_path in self.protected_folders:
            self.protected_folders.remove(folder_path)
            node["protected"] = False
            node["status"] = "To Be Deleted"
        else:
            self.protected_folders.add(folder_path)
            node["protected"] = True
            node["status"] = "Kept"

        self.update_tree_view()

    def _get_node_by_path(self, path):
        if not path:
            return self.plan
        parts = path.split("/")
        current = self.plan
        for p in parts:
            if (
                p in current
                and isinstance(current[p], dict)
                and current[p].get("__type__") != "file"
            ):
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

        dialog = ctk.CTkInputDialog(
            text="Enter new folder name:", title="Rename Folder"
        )
        dialog.transient(self)
        new_name = dialog.get_input()
        if not new_name:
            return
            
        from app.core.path_utils import sanitize_name
        new_name = sanitize_name(new_name)
        
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
                new_manual.add(new_path + mf[len(current_path) :])
        self.manual_folders.update(new_manual)

        for f, target in list(self.locked_files.items()):
            if target == current_path:
                self.locked_files[f] = new_path
            elif target.startswith(current_path + "/"):
                self.locked_files[f] = new_path + target[len(current_path) :]

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
                    if v is None or (
                        isinstance(v, dict) and v.get("__type__") == "file"
                    ):
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
        dialog.transient(self)
        new_name = dialog.get_input()
        if not new_name:
            return
            
        from app.core.path_utils import sanitize_name
        new_name = sanitize_name(new_name)
        
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
            new_plan = self.analyzer.generate_sorting_plan(self.base_dir, self.settings)
            self._apply_locked_files(new_plan)
            self.plan = new_plan
            self.plan_errors = self.verifier.verify_plan(self.base_dir, self.plan)
            has_errors = bool(self.plan_errors)
            self.execute_btn.configure(state="disabled" if has_errors else "normal")
            self.render_tree()


def run_app(settings) -> None:
    """Instantiate and run the main application."""
    app = AutoSorterApp(settings)
    app.mainloop()
