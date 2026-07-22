"""Main application GUI module using NiceGUI."""

import os
import asyncio
import logging
import os

from nicegui import ui

from app.core.session import AppSession
from app.core.verifier import VerificationEngine
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
        self.verifier = VerificationEngine()

        self.tree_nodes = []
        self._pending_files = set()
        self.observer = None
        self._debounce_task = None
        self._cancel_recalc_flag = False

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
            self.recalc_dialog.props('persistent')
            with ui.card().classes('items-center'):
                ui.label("Recalculating plan...")
                ui.spinner(size='lg')
                ui.button("Cancel", on_click=self.cancel_recalc).props('aria-label="Cancel Recalculation Button"')

        # Check wizard on startup
        ui.timer(0.1, self.check_setup_wizard, once=True)
        
        if self.base_dir:
            ui.timer(0.2, self.start_analysis, once=True)

    def check_setup_wizard(self):
        """Check if the setup wizard needs to be shown on startup."""
        import sys

        from app.config import get_app_dir
        
        if getattr(sys, "frozen", False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            
        local_model_dir = os.path.join(base_path, "offline_bundle", "model")
        user_model_dir = get_app_dir() / "model"
        
        if os.path.exists(os.path.join(local_model_dir, "config.json")) or (user_model_dir / "config.json").exists():
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
        self.app_session = AppSession(self.settings, self.base_dir)
        self.status_label.set_text("Scanning directory...")
        self.cancel_btn.set_visibility(True)
        self._cancel_analysis_flag = False

        asyncio.create_task(self._scan_and_process_worker())

    async def _scan_and_process_worker(self):
        try:
            from app.core.scanner import get_files_recursively
            files = await asyncio.to_thread(get_files_recursively, self.app_session.base_dir)
            self.total_files = len(files)
            self.completed_files = 0
            
            from app.core.metadata import MetadataPass

            bypassed_files = await asyncio.to_thread(
                MetadataPass.run,
                self.app_session.base_dir,
                files,
                self.settings,
                self.app_session.db,
                None,
                lambda: getattr(self, "_cancel_analysis_flag", False)
            )
            self.completed_files += len(bypassed_files)
            if self.total_files > 0:
                self.progress_bar.set_value(self.completed_files / self.total_files)
            
            bypassed_set = set(bypassed_files)
            items_to_sort = [f for f in files if f not in bypassed_set]
            
            generator = self.app_session.process_items(
                items_to_sort, 
                None, 
                lambda: getattr(self, "_cancel_analysis_flag", False)
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
                self.render_tree()
                self.status_label.set_text("Analysis complete.")
                self.execute_btn.enable()
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
                    check_cancel
                )

                if self._cancel_recalc_flag:
                    self.status_label.set_text("Recalculation cancelled.")
                    return

                errors = await asyncio.to_thread(
                    self.verifier.verify_plan,
                    self.base_dir,
                    plan,
                    check_cancel
                )

                if self._cancel_recalc_flag:
                    self.status_label.set_text("Recalculation cancelled.")
                    return

                self.plan = plan
                self.plan_errors = errors
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
                if k in self.plan_errors:
                    text += f" (Error: {self.plan_errors[k]})"
                    icon = "error"
                nodes_list.append({"id": node_id, "text": text, "icon": icon})

    def execute_sort(self):
        """Execute the sorting plan."""
        if not self.app_session or not self.plan:
            return

        self.execute_btn.disable()
        self.status_label.set_text("Executing sort...")
        self.progress_bar.set_value(0)

        async def run():
            try:
                summary = await asyncio.to_thread(
                    self.app_session.execute_moves, self.plan
                )
                ui.notify(f"Sorted successfully: {summary}")
                self.status_label.set_text("Sorting complete.")
            except Exception as e:
                logger.error(f"Error executing sort: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Sorting failed.")
            finally:
                self.plan = {}
                self.render_tree()

        asyncio.create_task(run())

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


def run_app(settings, directory=None) -> None:
    """Run the NiceGUI application."""
    app_instance = AutoSorterApp(settings)
    if directory:
        if os.path.exists(directory):
            app_instance.base_dir = os.path.abspath(directory)
    app_instance.build_ui()
    ui.run(title="Smart AutoSorter AI Pro", port=8080, reload=False, show=True)
