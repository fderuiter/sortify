"""Main application GUI module using NiceGUI."""

import asyncio
import logging
import os

from nicegui import ui

from app.core.session import AppSession
from app.ui.dialog_helper import ask_directory_async

logger = logging.getLogger(__name__)


class AutoSorterApp:
    """Main application class for the NiceGUI interface."""

    def __init__(self, settings):
        self.settings = settings
        self.base_dir = ""
        self.plan = {}
        self.locked_files = {}
        self.manual_folders = set()
        self.protected_folders = set()
        self.plan_errors = {}
        self.expanded_nodes = set()

        self.total_files = 0
        self.completed_files = 0
        self.start_time = 0.0
        self._cancel_analysis_flag = False

        self.app_session = None

        self.tree_nodes = []
        self._pending_files = set()
        self.observer = None
        self._debounce_task = None
        self._cancel_recalc_flag = False
        self.loop = None

        self.contextual_rename = self.settings.CONTEXTUAL_RENAMING
        self.preserve_hierarchy = self.settings.PRESERVE_HIERARCHY

    def build_ui(self):
        """Build the main user interface."""
        ui.add_head_html("<style> .q-tree__node-header { padding: 4px; } </style>")

        with ui.header().classes("items-center justify-between"):
            ui.label("AI File Organizer Pro").classes("text-h6").props(
                'aria-label="Application Title"'
            )
            with ui.row():
                ui.button("Settings", on_click=self.show_settings_view).props(
                    'aria-label="Settings Button"'
                )
                ui.button("Help", on_click=self.show_help_view).props(
                    'aria-label="Help Button"'
                )

        with ui.column().classes("w-full items-center mt-4"):
            self.select_btn = ui.button(
                "Select Directory to Sort", on_click=self.select_directory
            ).props('aria-label="Select Directory Button"')
            self.status_label = (
                ui.label("Waiting for directory...")
                .classes("text-gray-500")
                .props('aria-label="Status Label"')
            )
            self.progress_bar = (
                ui.linear_progress(value=0)
                .classes("w-1/2 mt-4")
                .props('aria-label="Progress Bar"')
            )

            self.cancel_btn = (
                ui.button("Cancel Analysis", on_click=self.cancel_analysis)
                .classes("bg-red-500 mt-2")
                .props('aria-label="Cancel Analysis Button"')
            )
            self.cancel_btn.set_visibility(False)

            self.meta_label = (
                ui.label("")
                .classes("text-cyan-500 mt-2")
                .props('aria-label="Metadata Label"')
            )

            self.warnings_label = (
                ui.label("")
                .classes("text-red-500 mt-2 font-bold text-center")
                .props('aria-label="Warnings Label"')
            )
            self.warnings_label.set_visibility(False)

            with ui.row().classes("mt-4 items-center"):
                ui.switch(
                    "Enable Contextual Renaming",
                    value=self.contextual_rename,
                    on_change=self.toggle_contextual_rename,
                ).props('aria-label="Contextual Renaming Switch"')
                ui.switch(
                    "Preserve Hierarchy",
                    value=self.preserve_hierarchy,
                    on_change=self.toggle_preserve_hierarchy,
                ).props('aria-label="Preserve Hierarchy Switch"')
                self.ai_naming_switch = ui.switch(
                    "AI-Assisted Naming",
                    value=getattr(self.settings, "AI_ASSISTED_NAMING", False),
                    on_change=self.toggle_ai_assisted_naming,
                ).props('aria-label="AI-Assisted Naming Switch"')

        with ui.row().classes("w-full h-96 mt-4 p-4"):
            self.tree_view = (
                ui.tree([], label_key="text", children_key="children")
                .classes("w-full")
                .props('default-expand-all aria-label="Sorting Plan Tree"')
            )

        with ui.row().classes("w-full justify-center mt-4"):
            self.execute_btn = (
                ui.button("Approve & Execute Sort", on_click=self.execute_sort)
                .classes("bg-green-500")
                .props('aria-label="Approve and Execute Sort Button"')
            )
            self.execute_btn.disable()

        with ui.dialog() as self.recalc_dialog:
            self.recalc_dialog.props("persistent")
            with ui.card().classes("items-center"):
                ui.label("Recalculating plan...")
                ui.spinner(size="lg")
                ui.button("Cancel", on_click=self.cancel_recalc).props(
                    'aria-label="Cancel Recalculation Button"'
                )

        # Check wizard on startup
        ui.timer(0.1, self.check_setup_wizard, once=True)
        ui.timer(0.2, self.check_abandoned_sessions, once=True)

        if self.base_dir:
            ui.timer(0.3, self.start_analysis, once=True)

    def check_abandoned_sessions(self):
        """Check for abandoned sessions on startup and prompt for recovery."""
        from app.core.session import scan_abandoned_sessions_async

        async def run():
            abandoned = await scan_abandoned_sessions_async()
            if not abandoned:
                return

            session_info = abandoned[0]

            with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
                dialog.props("persistent")
                ui.label("Interrupted Session Detected").classes("text-h6 text-red-500")
                ui.label(
                    "An application crash occurred during a previous file sorting operation. Files may be partially moved."
                )
                ui.label(f"Location: {session_info['base_dir']}")

                with ui.row().classes("w-full justify-end mt-4 gap-2"):

                    def on_resume():
                        dialog.close()
                        self.resume_session(session_info)

                    def on_revert():
                        dialog.close()
                        self.revert_session(session_info)

                    ui.button("Revert", on_click=on_revert).props(
                        'color="negative" aria-label="Revert Button"'
                    )
                    ui.button("Resume", on_click=on_resume).props(
                        'color="positive" aria-label="Resume Button"'
                    )
            dialog.open()

        asyncio.create_task(run())

    def resume_session(self, session_info):
        """Resume an interrupted sorting operation."""
        import json

        self.base_dir = session_info["base_dir"]
        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info["session_id"]
        )

        try:
            with open(session_info["plan_path"], "r") as f:
                self.plan = json.load(f)
        except Exception as e:
            ui.notify(f"Could not load plan: {e}", type="negative")
            self.app_session.close()
            return

        self.status_label.set_text("Resuming sorting operation...")

        async def run():
            success = False
            try:
                summary = await asyncio.to_thread(
                    self.app_session.execute_moves, self.plan, True
                )
                ui.notify(f"Resumed and sorted successfully: {summary}")
                self.status_label.set_text("Sorting complete.")
                success = True
            except Exception as e:
                logger.error(f"Error resuming sort: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Sorting failed.")
            finally:
                self.plan = {}
                self.render_tree()
                if success and self.app_session:
                    self.app_session.close()
                    self.app_session = None

        asyncio.create_task(run())

    def revert_session(self, session_info):
        """Revert an interrupted sorting operation."""
        self.base_dir = session_info["base_dir"]
        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info["session_id"]
        )
        self.status_label.set_text("Reverting sorting operation...")

        async def run():
            success = False
            try:
                await asyncio.to_thread(
                    self.app_session.rollback, session_info["session_id"], True
                )
                ui.notify("Reverted successfully.")
                self.status_label.set_text("Reversion complete.")
                success = True
            except Exception as e:
                logger.error(f"Error reverting sort: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Reversion failed.")
            finally:
                self.plan = {}
                self.render_tree()
                if success and self.app_session:
                    self.app_session.close()
                    self.app_session = None

        asyncio.create_task(run())

    def check_setup_wizard(self):
        """Check if the setup wizard needs to be shown on startup."""
        from app.config import get_app_dir
        from app.core.path_utils import get_base_path

        base_path = get_base_path(__file__)

        local_model_dir = os.path.join(base_path, "offline_bundle", "model")
        user_model_dir = get_app_dir() / "model"

        if (
            os.path.exists(os.path.join(local_model_dir, "config.json"))
            or (user_model_dir / "config.json").exists()
        ):
            if self.settings.AI_CONSENT_GRANTED is None:
                self.settings.AI_CONSENT_GRANTED = True
            return
        if self.settings.AI_CONSENT_GRANTED is False:
            return

        # Run wizard dialog
        from app.ui.wizard import show_wizard

        show_wizard(self, self.settings)

    def show_settings_view(self):
        """Show the settings dialog."""
        from app.ui.settings import show_settings

        show_settings(self, self.settings)

    def show_help_view(self):
        """Display help information."""
        ui.notify("Help documentation is available in the user manual.")

    def select_directory(self):
        """Prompt the user to select a directory for analysis."""

        def on_selected(path):
            if path:
                self.base_dir = path
                self.start_analysis()

        ask_directory_async(None, "Select Directory", on_selected, None, None)

    def start_analysis(self):
        """Start the background analysis of the selected directory."""
        self.stop_watcher()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self.app_session = AppSession(self.settings, self.base_dir)
        self.status_label.set_text("Scanning directory...")
        self.cancel_btn.set_visibility(True)
        self._cancel_analysis_flag = False

        asyncio.create_task(self._scan_and_process_worker())

    async def _scan_and_process_worker(self):
        try:
            from app.core.scanner import get_files_recursively

            files = await asyncio.to_thread(
                get_files_recursively, self.app_session.base_dir
            )
            self.total_files = len(files)
            self.completed_files = 0

            from app.core.verifier import is_ml_available

            if not is_ml_available():
                has_images_or_pdfs = any(
                    os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg", ".pdf")
                    for f in files
                )
                if has_images_or_pdfs:
                    self.show_ml_warning_dialog("Visual text extraction (OCR)")

            from app.core.metadata import MetadataPass

            self.status_label.set_text(
                "Discovering metadata and checking fast rules..."
            )

            def bypass_callback():
                def increment():
                    self.completed_files += 1
                    if self.total_files > 0:
                        self.progress_bar.set_value(
                            self.completed_files / self.total_files
                        )

                if self.loop:
                    self.loop.call_soon_threadsafe(increment)

            bypassed_files = await asyncio.to_thread(
                MetadataPass.run,
                self.app_session.base_dir,
                files,
                self.settings,
                self.app_session.db,
                bypass_callback,
                lambda: getattr(self, "_cancel_analysis_flag", False),
            )
            # Ensure final progress bar sync after metadata pass
            if self.total_files > 0:
                self.progress_bar.set_value(self.completed_files / self.total_files)

            bypassed_set = set(bypassed_files)
            items_to_sort = [f for f in files if f not in bypassed_set]

            if items_to_sort:
                self.status_label.set_text("Extracting remaining document text...")

            generator = self.app_session.process_items(
                items_to_sort,
                None,
                lambda: getattr(self, "_cancel_analysis_flag", False),
            )

            def get_next_chunk():
                try:
                    return next(generator)
                except StopIteration:
                    return None

            while True:
                if self._cancel_analysis_flag:
                    break

                chunk = await asyncio.to_thread(get_next_chunk)
                if chunk is None:
                    break

                await asyncio.to_thread(self.app_session.partial_fit, chunk)
                self.completed_files += len(chunk)
                if self.total_files > 0:
                    self.progress_bar.set_value(self.completed_files / self.total_files)
                await asyncio.sleep(0.01)

            if not self._cancel_analysis_flag:
                self.plan = await asyncio.to_thread(
                    self.app_session.generate_sorting_plan
                )
                self.verify_current_plan()
                self.render_tree()
                self.status_label.set_text("Analysis complete.")
                self.execute_btn.enable()
                self.start_watcher()
        except Exception as e:
            logger.error(f"Error scanning directory: {e}")
            self.status_label.set_text(f"Error: {e}")
        finally:
            self.cancel_btn.set_visibility(False)

    def cancel_analysis(self):
        """Cancel an ongoing analysis."""
        self._cancel_analysis_flag = True
        self.status_label.set_text("Analysis cancelled.")
        self.cancel_btn.set_visibility(False)

    def cancel_recalc(self):
        """Cancel the recalculation process."""
        self._cancel_recalc_flag = True
        self.recalc_dialog.close()

    def toggle_contextual_rename(self, e):
        """Toggle contextual renaming and rebuild the sorting plan."""
        self.settings.CONTEXTUAL_RENAMING = e.value
        self._rebuild_plan_async()

    def toggle_preserve_hierarchy(self, e):
        """Toggle hierarchy preservation and rebuild the sorting plan."""
        self.settings.PRESERVE_HIERARCHY = e.value
        self._rebuild_plan_async()

    def toggle_ai_assisted_naming(self, e):
        """Toggle AI-assisted naming."""
        from app.core.verifier import is_ml_available

        if e.value and not is_ml_available():
            self.show_ml_warning_dialog("AI-assisted naming")
            self.ai_naming_switch.value = False
            self.settings.AI_ASSISTED_NAMING = False
        else:
            self.settings.AI_ASSISTED_NAMING = e.value
            self._rebuild_plan_async()

    def show_ml_warning_dialog(self, feature_name: str):
        """Show a clear, non-blocking warning dialogue explaining that the feature requires the full ML package."""
        with ui.dialog() as dialog, ui.card().classes("w-96 p-6"):
            ui.label("Feature Unavailable").classes(
                "text-xl font-bold mb-4 text-red-500"
            ).props('aria-label="Warning Dialog Title"')
            ui.label(
                f"The '{feature_name}' feature requires heavy machine learning dependencies (like PyTorch and EasyOCR) "
                "which are excluded from this lightweight build."
            ).classes("mb-4").props('aria-label="Warning Description"')
            ui.label(
                "Please download the full ML installer bundle to access offline AI naming and visual text extraction."
            ).classes("text-sm text-gray-500 mb-4").props(
                'aria-label="Warning Suggestion"'
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("OK", on_click=dialog.close).classes(
                    "bg-blue-500 text-white"
                ).props('aria-label="Warning OK Button"')
        dialog.open()

    def _rebuild_plan_async(self):
        if not self.app_session or not self.base_dir:
            return

        if self._debounce_task:
            self._debounce_task.cancel()

        async def delayed_run():
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

            self._cancel_recalc_flag = False
            self.recalc_dialog.open()
            self.status_label.set_text("Rebuilding plan...")

            def check_cancel():
                return getattr(self, "_cancel_recalc_flag", False)

            try:
                plan = await asyncio.to_thread(
                    self.app_session.analyzer.generate_sorting_plan,
                    self.base_dir,
                    self.settings,
                    self.locked_files,
                    check_cancel,
                )

                if self._cancel_recalc_flag:
                    self.status_label.set_text("Recalculation cancelled.")
                    return

                self.plan = plan
                self.verify_current_plan()
                self.render_tree()
                self.status_label.set_text("Plan rebuilt.")
            except Exception as e:
                logger.error(f"Error rebuilding plan: {e}")
                self.status_label.set_text("Error rebuilding plan.")
            finally:
                self.recalc_dialog.close()

        self._debounce_task = asyncio.create_task(delayed_run())

    def render_tree(self):
        """Render the tree view of the sorting plan."""
        self.tree_nodes = []
        self._flatten(self.plan, "", self.tree_nodes)
        if hasattr(self, "tree_view"):
            self.tree_view._props["nodes"] = self.tree_nodes
            self.tree_view.update()

    def _flatten(self, node, current_path, nodes_list):
        for k, v in node.items():
            node_id = f"{current_path}/{k}" if current_path else k
            if isinstance(v, dict) and "__type__" not in v:
                children = []
                nodes_list.append(
                    {"id": node_id, "text": k, "children": children, "icon": "folder"}
                )
                self._flatten(v, node_id, children)
            else:
                text = k
                icon = "insert_drive_file"
                if isinstance(v, dict):
                    status = v.get("status", "")
                    if status:
                        text += f" [{status}]"
                    if "error" in status.lower() or "locked" in status.lower():
                        icon = "error"
                if k in self.plan_errors or node_id in self.plan_errors:
                    err_msg = self.plan_errors.get(node_id) or self.plan_errors.get(k)
                    text += f" (Error: {err_msg})"
                    icon = "error"
                nodes_list.append({"id": node_id, "text": text, "icon": icon})

    def execute_sort(self):
        """Execute the sorting plan."""
        if not self.app_session or not self.plan:
            return

        self.execute_btn.disable()
        self.status_label.set_text("Executing sort...")
        self.progress_bar.set_value(0)
        self.stop_watcher()

        async def run():
            success = False
            try:
                summary = await asyncio.to_thread(
                    self.app_session.execute_moves, self.plan
                )
                ui.notify(f"Sorted successfully: {summary}")
                self.status_label.set_text("Sorting complete.")
                success = True
            except Exception as e:
                logger.error(f"Error executing sort: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Sorting failed.")

                with ui.dialog() as error_dialog, ui.card().classes("w-full max-w-md"):
                    ui.label("Move Transaction Error").classes("text-h6 text-red-500")
                    ui.label(f"The organization process failed: {e}").classes(
                        "text-body1"
                    )
                    ui.label(
                        "An automated rollback was successfully executed to restore files and index database."
                    ).classes("text-body2 text-gray-600")
                    with ui.row().classes("w-full justify-end mt-4"):
                        ui.button("Close", on_click=error_dialog.close).props(
                            'color="primary" aria-label="Close Error Dialog"'
                        )
                error_dialog.open()
            finally:
                self.plan = {}
                self.render_tree()
                self.execute_btn.enable()
                self.start_watcher()
                if success and self.app_session:
                    self.app_session.close()
                    self.app_session = None

        asyncio.create_task(run())

    def start_watcher(self):
        """Start the watchdog folder observer to monitor base_dir."""
        if not self.base_dir or not os.path.exists(self.base_dir):
            return

        self.stop_watcher()

        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class FolderChangeHandler(FileSystemEventHandler):
            def __init__(self, app):
                self.app = app

            def on_any_event(self, event):
                if (
                    ".branches" in event.src_path
                    or "autosorter.db" in event.src_path
                    or "history.db" in event.src_path
                    or "cache.db" in event.src_path
                    or "plan.json" in event.src_path
                ):
                    return
                if self.app.loop:
                    self.app.loop.call_soon_threadsafe(self.app._rebuild_plan_async)

        self.observer = Observer()
        handler = FolderChangeHandler(self)
        self.observer.schedule(handler, self.base_dir, recursive=True)
        self.observer.start()
        logger.info(f"Started folder observer for {self.base_dir}")

    def stop_watcher(self):
        """Stop the watchdog folder observer."""
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except Exception as e:
                logger.error(f"Error stopping folder observer: {e}")
            finally:
                self.observer = None

    def get_tree_state(self):
        """Get a representation of the tree state."""

        # Format tree state to match the old dump_state snapshot requirements
        def _convert(nodes):
            res = []
            for n in nodes:
                item = {
                    "iid": n["id"],
                    "text": n["text"],
                    "children": _convert(n.get("children", [])),
                }
                # mock old "open" property which was default True or False
                item["open"] = True if n.get("children") else False
                res.append(item)
            return res

        return _convert(self.tree_nodes)

    def verify_current_plan(self):
        """Run path integrity verification on the current plan and update warnings."""
        if not self.base_dir or not self.plan:
            if hasattr(self, "warnings_label"):
                self.warnings_label.set_text("")
                self.warnings_label.set_visibility(False)
            return

        from app.core.verifier import VerificationEngine

        integrity_result = VerificationEngine.verify_plan_integrity(
            self.base_dir, self.plan
        )

        self.plan_errors = {}
        if not integrity_result["success"]:
            for item in integrity_result.get("collisions", []):
                src_abs = item.get("source")
                if src_abs:
                    rel_src = os.path.relpath(src_abs, self.base_dir).replace("\\", "/")
                    self.plan_errors[rel_src] = item["message"]
                    self.plan_errors[os.path.basename(src_abs)] = item["message"]
                dst_abs = item.get("path")
                if dst_abs:
                    rel_dst = os.path.relpath(dst_abs, self.base_dir).replace("\\", "/")
                    self.plan_errors[rel_dst] = item["message"]
                    self.plan_errors[os.path.basename(dst_abs)] = item["message"]

            for item in integrity_result.get("circular_renames", []):
                path_abs = item.get("path")
                if path_abs:
                    rel_path = os.path.relpath(path_abs, self.base_dir).replace(
                        "\\", "/"
                    )
                    self.plan_errors[rel_path] = item["message"]
                    self.plan_errors[os.path.basename(path_abs)] = item["message"]

            for item in integrity_result.get("broken_links", []):
                path_abs = item.get("path")
                if path_abs:
                    rel_path = os.path.relpath(path_abs, self.base_dir).replace(
                        "\\", "/"
                    )
                    self.plan_errors[rel_path] = item["message"]
                    self.plan_errors[os.path.basename(path_abs)] = item["message"]

            warnings_text = "\n".join(integrity_result["warnings"])
            if hasattr(self, "warnings_label"):
                self.warnings_label.set_text(warnings_text)
                self.warnings_label.set_visibility(True)
        else:
            if hasattr(self, "warnings_label"):
                self.warnings_label.set_text("")
                self.warnings_label.set_visibility(False)


def run_app(settings, directory=None) -> None:
    """Run the NiceGUI application."""
    app_instance = AutoSorterApp(settings)
    if directory:
        if os.path.exists(directory):
            app_instance.base_dir = os.path.abspath(directory)
    app_instance.build_ui()
    ui.run(
        host="127.0.0.1",
        title="Smart AutoSorter AI Pro",
        port=8080,
        reload=False,
        show=True,
    )
