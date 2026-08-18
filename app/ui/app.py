"""Main application GUI module using NiceGUI."""

import asyncio
import logging
import os

from nicegui import ui

from app.core.session import AppSession
from app.ui.dialog_helper import ask_directory_async, get_dialog_card_classes

logger = logging.getLogger(__name__)


class AutoSorterApp:
    """Main application class for the NiceGUI interface."""

    def __init__(self, settings):
        self.settings = settings
        self.base_dir = ""
        self.plan = {}
        self.locked_files = {}
        self._ratings_cache = {}
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
        self.sorting_strategy = getattr(self.settings, "SORTING_STRATEGY", "default")
        self.clinical_smart_renaming = getattr(
            self.settings, "CLINICAL_SMART_RENAMING", False
        )
        self.clinical_generate_audit_report = getattr(
            self.settings, "CLINICAL_GENERATE_AUDIT_REPORT", True
        )

    def build_ui(self):
        """Build the main user interface with modern styling, presets, and interactive controls."""
        ui.add_head_html("""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
body {
    font-family: 'Inter', sans-serif;
    background-color: #f8fafc;
    color: #0f172a;
}
.q-tree__node-header { padding: 6px 8px; border-radius: 6px; transition: background-color 0.15s ease; }
.q-tree__node-header:hover { background-color: #f1f5f9; }
.tree-node-row .action-buttons { opacity: 0.2; transition: opacity 0.15s ease; }
.tree-node-row:hover .action-buttons { opacity: 1.0; }
.drag-target-active { outline: 2px dashed #3b82f6; background-color: #eff6ff !important; }
</style>
""")

        with ui.header().classes(
            "bg-slate-900 text-white px-6 py-3 items-center justify-between shadow-md"
        ):
            with ui.row().classes("items-center gap-3"):
                ui.icon("folder_special", size="md", color="blue-4")
                ui.label("Sortify AI Pro").classes("text-xl font-bold tracking-tight")
            with ui.row().classes("items-center gap-2"):
                ui.button(
                    "CRO Forensic Ingest",
                    icon="security",
                    on_click=self.show_cro_forensic_dialog,
                ).classes("bg-blue-600 text-white").props(
                    'size="sm" unelevated aria-label="CRO Forensic Ingest Button"'
                )
                ui.button(
                    "Settings", icon="tune", on_click=self.show_settings_view
                ).props(
                    'flat text-color="white" size="sm" aria-label="Settings Button"'
                )
                ui.button(
                    "Help", icon="help_outline", on_click=self.show_help_view
                ).props('flat text-color="white" size="sm" aria-label="Help Button"')

        with ui.column().classes("w-full max-w-5xl mx-auto p-6 items-center gap-4"):
            # 1. Directory Selection & Presets Card
            with ui.card().classes(
                "w-full p-5 bg-white rounded-xl shadow-sm border border-slate-200"
            ):
                ui.label("Target Directory").classes(
                    "text-xs font-bold text-slate-500 uppercase tracking-wider mb-2"
                )
                with ui.row().classes("w-full items-center gap-3"):
                    self.path_input = (
                        ui.input(
                            placeholder="Enter absolute directory path...",
                            value=self.base_dir,
                        )
                        .classes("flex-grow")
                        .props("outlined dense")
                    )
                    self.path_input.on("keydown.enter", self.on_scan_clicked)

                    self.scan_btn = (
                        ui.button(
                            "Scan & Organize",
                            icon="bolt",
                            on_click=self.on_scan_clicked,
                        )
                        .classes("bg-blue-600 text-white font-medium")
                        .props('unelevated aria-label="Scan and Organize Button"')
                    )

                    self.browse_btn = ui.button(
                        "Browse...", icon="folder_open", on_click=self.select_directory
                    ).props(
                        'outlined color="grey-8" aria-label="Browse Directory Button"'
                    )

                with ui.row().classes("w-full items-center gap-2 mt-2 flex-wrap"):
                    ui.label("Quick Presets:").classes(
                        "text-xs font-medium text-slate-400"
                    )
                    demo_path = os.path.abspath("sandbox/demo_workspace")
                    ui.button(
                        "Demo Workspace",
                        icon="science",
                        on_click=lambda: self.load_preset(demo_path),
                    ).props('size="xs" outline color="primary" rounded')
                    ui.button(
                        "Downloads",
                        icon="download",
                        on_click=lambda: self.load_preset(
                            os.path.expanduser("~/Downloads")
                        ),
                    ).props('size="xs" outline color="grey-8" rounded')
                    ui.button(
                        "Documents",
                        icon="description",
                        on_click=lambda: self.load_preset(
                            os.path.expanduser("~/Documents")
                        ),
                    ).props('size="xs" outline color="grey-8" rounded')

            # 2. Status and Progress Bar
            with ui.card().classes(
                "w-full p-4 bg-white rounded-xl shadow-sm border border-slate-200 items-center text-center"
            ):
                self.status_label = (
                    ui.label("Ready. Select or enter a directory above to start.")
                    .classes("text-sm font-medium text-slate-600")
                    .props('aria-label="Status Label"')
                )
                self.progress_bar = (
                    ui.linear_progress(value=0)
                    .classes("w-full max-w-xl mt-3 rounded-full")
                    .props('aria-label="Progress Bar" color="blue"')
                )
                self.file_progress_bar = (
                    ui.linear_progress(value=0)
                    .classes("w-full max-w-xl mt-2 rounded-full")
                    .props('aria-label="File Progress Bar" color="indigo"')
                )
                self.file_progress_bar.set_visibility(False)

                self.file_progress_label = (
                    ui.label("")
                    .classes("text-slate-400 text-xs mt-1")
                    .props('aria-label="File Progress Label"')
                )
                self.file_progress_label.set_visibility(False)

                self.cancel_btn = (
                    ui.button("Cancel Analysis", on_click=self.cancel_analysis)
                    .classes("bg-red-500 text-white mt-2")
                    .props('size="sm" unelevated aria-label="Cancel Analysis Button"')
                )
                self.cancel_btn.set_visibility(False)

                self.meta_label = (
                    ui.label("")
                    .classes("text-cyan-600 text-xs font-mono mt-2")
                    .props('aria-label="Metadata Label"')
                )

                self.warnings_label = (
                    ui.label("")
                    .classes("text-red-500 mt-2 font-bold text-center text-xs")
                    .props('aria-label="Warnings Label"')
                )
                self.warnings_label.set_visibility(False)

                self.ai_warnings_label = (
                    ui.label("")
                    .classes(
                        "text-amber-700 mt-2 text-xs font-semibold text-center bg-amber-50 border border-amber-200 p-2 rounded-lg w-full max-w-lg"
                    )
                    .props('aria-label="AI Offline Warning Label"')
                )
                self.ai_warnings_label.set_visibility(False)

            # 3. Strategy Configuration Row
            with ui.row().classes(
                "w-full items-center justify-between flex-wrap gap-4 px-2"
            ):
                self.strategy_selector = (
                    ui.select(
                        {
                            "default": "Standard Semantic",
                            "generative": "Generative AI",
                            "clinical_tmf": "Clinical TMF (Sponsor)",
                            "clinical_isf": "Clinical ISF (Site Binder)",
                        },
                        value=self.sorting_strategy,
                        label="Sorting Strategy",
                        on_change=self.change_sorting_strategy,
                    )
                    .classes("w-64")
                    .props('outlined dense aria-label="Sorting Strategy Selector"')
                )

                with ui.row().classes("items-center gap-4 flex-wrap"):
                    ui.switch(
                        "Contextual Renaming",
                        value=self.contextual_rename,
                        on_change=self.toggle_contextual_rename,
                    ).props('dense aria-label="Contextual Renaming Switch"')
                    ui.switch(
                        "Preserve Hierarchy",
                        value=self.preserve_hierarchy,
                        on_change=self.toggle_preserve_hierarchy,
                    ).props('dense aria-label="Preserve Hierarchy Switch"')
                    self.ai_naming_switch = ui.switch(
                        "AI Naming",
                        value=getattr(self.settings, "AI_ASSISTED_NAMING", False),
                        on_change=self.toggle_ai_assisted_naming,
                    ).props('dense aria-label="AI-Assisted Naming Switch"')

            is_clinical_init = self.sorting_strategy in ("clinical_tmf", "clinical_isf")
            with ui.row().classes(
                "w-full items-center flex-wrap justify-between gap-4 bg-blue-50 p-3 rounded-xl border border-blue-100"
            ) as self.clinical_controls_row:
                self.clinical_controls_row.set_visibility(is_clinical_init)
                with ui.row().classes("items-center gap-4"):
                    ui.label("Clinical Controls:").classes(
                        "text-sm font-bold text-blue-900"
                    )
                    ui.switch(
                        "Smart Clinical Renaming",
                        value=self.clinical_smart_renaming,
                        on_change=self.toggle_clinical_renaming,
                    ).props('dense aria-label="Clinical Renaming Switch"')
                    ui.switch(
                        "Generate Audit Report",
                        value=self.clinical_generate_audit_report,
                        on_change=self.toggle_clinical_audit_report,
                    ).props('dense aria-label="Clinical Audit Report Switch"')
                self.compliance_btn = (
                    ui.button(
                        "Compliance Checklist",
                        on_click=self.show_compliance_checklist_dialog,
                        icon="fact_check",
                    )
                    .classes("bg-blue-600 text-white")
                    .props(
                        'size="sm" unelevated aria-label="Compliance Audit Checklist Button"'
                    )
                )

            # 4. Proposed Plan Tree Card & Action Toolbar
            with ui.card().classes(
                "w-full p-5 bg-white rounded-xl shadow-sm border border-slate-200"
            ):
                with ui.row().classes(
                    "w-full items-center justify-between mb-3 pb-3 border-b border-slate-100"
                ):
                    with ui.row().classes("items-center gap-3"):
                        ui.label("Proposed Organization Plan").classes(
                            "text-base font-bold text-slate-800"
                        )
                        self.folder_count_badge = ui.badge(
                            "0 folders", color="blue-7"
                        ).props("rounded text-xs")
                        self.file_count_badge = ui.badge(
                            "0 files", color="slate-6"
                        ).props("rounded text-xs")

                    with ui.row().classes("items-center gap-2"):
                        ui.button(
                            "New Folder",
                            icon="create_new_folder",
                            on_click=self.show_new_folder_dialog,
                        ).props('size="sm" outline color="primary" rounded')
                        ui.button(
                            "Expand All",
                            icon="unfold_more",
                            on_click=self.expand_all_nodes,
                        ).props('size="sm" flat color="grey-8"')
                        ui.button(
                            "Collapse All",
                            icon="unfold_less",
                            on_click=self.collapse_all_nodes,
                        ).props('size="sm" flat color="grey-8"')

                with ui.scroll_area().classes("w-full h-96 p-2"):
                    self.tree_view = (
                        ui.tree([], label_key="text", children_key="children")
                        .classes("w-full")
                        .props('default-expand-all aria-label="Sorting Plan Tree"')
                    )
                    # Vue slot for rich drag-drop, badge chips, rename, lock, and quality ratings
                    self.tree_view.add_slot(
                        "default-header",
                        """
                        <div class="row items-center justify-between w-full group tree-node-row py-1"
                             :draggable="prop.node.is_file"
                             @dragstart="(e) => { 
                                 if (prop.node.is_file) {
                                     e.dataTransfer.setData('text/plain', prop.node.id);
                                     e.dataTransfer.effectAllowed = 'move';
                                 }
                             }"
                             @dragover="(e) => { 
                                 if (!prop.node.is_file) {
                                     e.preventDefault(); 
                                 }
                             }"
                             @drop="(e) => { 
                                 if (!prop.node.is_file) {
                                     e.preventDefault();
                                     const sourceId = e.dataTransfer.getData('text/plain');
                                     $parent.$emit('node-drop', { source: sourceId, target: prop.node.id });
                                 }
                             }">
                            <div class="row items-center gap-2">
                                <q-icon :name="prop.node.icon" 
                                        :color="prop.node.is_file ? (prop.node.is_locked ? 'amber-9' : 'primary') : 'amber-8'" 
                                        size="xs" />
                                <span class="font-medium text-sm text-slate-800">{{ prop.node.text }}</span>
                                <q-badge v-if="prop.node.badge" 
                                         :color="prop.node.badge_color || 'grey-7'" 
                                         text-color="white" 
                                         class="text-xs" rounded>
                                    {{ prop.node.badge }}
                                </q-badge>
                            </div>
                            <!-- Action buttons -->
                            <div v-if="prop.node.is_file" class="action-buttons row items-center q-gutter-xs">
                                <q-btn flat round dense 
                                       :icon="prop.node.is_locked ? 'lock' : 'lock_open'" 
                                       size="xs" 
                                       :color="prop.node.is_locked ? 'amber-9' : 'grey-6'"
                                       @click.stop="$parent.$emit('node-toggle-lock', { file_id: prop.node.id })">
                                    <q-tooltip>{{ prop.node.is_locked ? 'Unlock automatic sorting' : 'Lock to this folder' }}</q-tooltip>
                                </q-btn>
                                <q-btn flat round dense 
                                       :icon="prop.node.rating === 'positive' ? 'thumb_up' : 'thumb_up_off_alt'"
                                       size="xs" 
                                       :color="prop.node.rating === 'positive' ? 'green-7' : 'grey-6'" 
                                       @click.stop="$parent.$emit('node-rate', { file_id: prop.node.id, rating: 'positive' })">
                                    <q-tooltip>Accurate folder placement</q-tooltip>
                                </q-btn>
                                <q-btn flat round dense 
                                       :icon="prop.node.rating === 'negative' ? 'thumb_down' : 'thumb_down_off_alt'"
                                       size="xs" 
                                       :color="prop.node.rating === 'negative' ? 'red-7' : 'grey-6'" 
                                       @click.stop="$parent.$emit('node-rate', { file_id: prop.node.id, rating: 'negative' })">
                                    <q-tooltip>Incorrect folder placement</q-tooltip>
                                </q-btn>
                            </div>
                            <div v-else class="action-buttons row items-center q-gutter-xs">
                                <q-btn flat round dense icon="edit" size="xs" color="grey-6"
                                       @click.stop="$parent.$emit('folder-rename', { folder_id: prop.node.id })">
                                    <q-tooltip>Rename folder</q-tooltip>
                                </q-btn>
                            </div>
                        </div>
                    """,
                    )
                    self.tree_view.on("node-drop", self.handle_node_drop)
                    self.tree_view.on("node-rate", self.handle_node_rate)
                    self.tree_view.on("node-toggle-lock", self.handle_node_toggle_lock)
                    self.tree_view.on("folder-rename", self.show_rename_folder_dialog)

            # 5. Execution Action Bar & Post-Sort Undo Rollback
            with ui.row().classes("w-full justify-center items-center gap-3 mt-2"):
                self.execute_btn = (
                    ui.button(
                        "Approve & Execute Sort",
                        icon="play_arrow",
                        on_click=self.execute_sort,
                    )
                    .classes("bg-emerald-600 text-white font-semibold px-6 py-2 shadow")
                    .props(
                        'unelevated rounded aria-label="Approve and Execute Sort Button"'
                    )
                )
                self.execute_btn.disable()

                self.undo_btn = (
                    ui.button(
                        "Undo Last Sort (Rollback)",
                        icon="undo",
                        on_click=self.undo_last_sort,
                    )
                    .classes("bg-amber-600 text-white font-semibold px-6 py-2 shadow")
                    .props('unelevated rounded aria-label="Undo Last Sort Button"')
                )
                self.undo_btn.set_visibility(False)

        with ui.dialog() as self.recalc_dialog:
            self.recalc_dialog.props("persistent")
            with ui.card().classes(
                "items-center w-full max-w-md min-w-[320px] p-6 rounded-xl"
            ):
                ui.label("Recalculating plan...").classes(
                    "font-semibold text-slate-800"
                )
                ui.spinner(size="lg", color="blue")
                ui.button("Cancel", on_click=self.cancel_recalc).props(
                    'flat color="grey-7" aria-label="Cancel Recalculation Button"'
                )

        # Check wizard and recovery on startup
        ui.timer(0.05, self.update_ai_warning, once=True)
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

            if session_info.get("is_rollback_recovery"):
                self.show_rollback_recovery_dialog(session_info)
                return

            if session_info.get("has_trapped_files"):
                self.show_recovery_wizard(session_info)
                return

            with (
                ui.dialog() as dialog,
                ui.card().classes(get_dialog_card_classes("md")),
            ):
                dialog.props("persistent")
                ui.label("Interrupted Session Detected").classes("text-h6 text-red-500")
                ui.label(
                    "An application crash occurred during a previous file sorting operation. Files may be partially moved."
                )
                ui.label(f"Location: {session_info['base_dir']}")

                with ui.row().classes("w-full justify-end mt-4 gap-2 flex-wrap"):

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

    def show_rollback_recovery_dialog(self, session_info):
        """Display the dedicated rollback recovery dialog for an interrupted rollback session."""
        with (
            ui.dialog() as dialog,
            ui.card().classes(get_dialog_card_classes("md")),
        ):
            dialog.props("persistent")
            ui.label("Interrupted Rollback Recovery").classes(
                "text-h6 text-red-600"
            ).props('aria-label="Rollback Recovery Title"')
            ui.label(
                "An unexpected crash interrupted a previous rollback operation. The system has detected an active journal file."
            ).classes("text-sm text-gray-700")
            ui.label(f"Directory: {session_info['base_dir']}").classes(
                "text-xs text-gray-500 font-mono"
            )

            with ui.row().classes("w-full justify-end mt-4 gap-2 flex-wrap"):

                def on_revert():
                    dialog.close()
                    self.revert_rollback_session(session_info)

                def on_resume():
                    dialog.close()
                    self.resume_rollback_session(session_info)

                ui.button("Revert", on_click=on_revert).props(
                    'color="negative" aria-label="Revert Button"'
                )
                ui.button("Resume", on_click=on_resume).props(
                    'color="positive" aria-label="Resume Button"'
                )
        dialog.open()

    def resume_rollback_session(self, session_info):
        """Resume and complete an interrupted rollback operation."""
        self.base_dir = session_info["base_dir"]
        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info["session_id"]
        )
        self.status_label.set_text("Resuming rollback operation...")

        async def run():
            success = False
            try:
                await asyncio.to_thread(
                    self.app_session.history_manager.resume_rollback,
                    session_info["session_id"],
                )
                ui.notify("Rollback resumed and completed successfully.")
                self.status_label.set_text("Rollback resume complete.")
                success = True
            except Exception as e:
                logger.error(f"Error resuming rollback: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Rollback resume failed.")
            finally:
                self.plan = {}
                self.render_tree()
                if success and self.app_session:
                    self.app_session.close()
                    self.app_session = None

        asyncio.create_task(run())

    def revert_rollback_session(self, session_info):
        """Revert an interrupted rollback operation back to previous state."""
        self.base_dir = session_info["base_dir"]
        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info["session_id"]
        )
        self.status_label.set_text("Reverting rollback operation...")

        async def run():
            success = False
            try:
                await asyncio.to_thread(
                    self.app_session.history_manager.revert_rollback,
                    session_info["safety_session_id"],
                )
                ui.notify("Rollback reverted successfully.")
                self.status_label.set_text("Rollback reversion complete.")
                success = True
            except Exception as e:
                logger.error(f"Error reverting rollback: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Rollback reversion failed.")
            finally:
                self.plan = {}
                self.render_tree()
                if success and self.app_session:
                    self.app_session.close()
                    self.app_session = None

        asyncio.create_task(run())

    def show_recovery_wizard(self, session_info):
        """Display the interactive recovery wizard for a failed rollback session with trapped files."""
        import os
        import shutil

        try:
            import sqlite3
        except Exception:
            try:
                from sqlcipher3 import dbapi2 as sqlite3
            except Exception:
                sqlite3 = None

        from app.core.mover import get_safe_path
        from app.ui.dialog_helper import ask_directory_async

        base_dir = session_info["base_dir"]
        safety_folder = session_info["safety_folder"]
        session_id = session_info["session_id"]
        session_dir = session_info["session_dir"]
        history_db_path = os.path.join(session_dir, "history.db")

        def _update_session_resolved_sync(db_path, sess_id):
            conn = None
            try:
                conn = sqlite3.connect(db_path, timeout=30.0)
                with conn:
                    conn.execute(
                        "UPDATE sessions SET status = 'resolved' WHERE session_id = ?",
                        (sess_id,),
                    )
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        # Get list of all files in safety folder to show/process
        files_to_recover = []
        if os.path.exists(safety_folder):
            for root, dirs, files in os.walk(safety_folder):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, safety_folder)
                    files_to_recover.append((full_path, rel_path))

        with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("lg")):
            dialog.props("persistent")

            # Title & Header
            ui.label("Startup Recovery Wizard").classes(
                "text-xl font-bold text-red-600"
            ).props('aria-label="Recovery Wizard Title"')
            ui.label(
                f"An interrupted or failed file rollback was detected. There are {len(files_to_recover)} trapped files in a hidden safety folder."
            ).classes("text-sm text-gray-700")
            ui.label(f"Original directory: {base_dir}").classes(
                "text-xs text-gray-500 font-mono"
            )

            # Custom area for wizard steps
            wizard_content = ui.column().classes("w-full gap-4")

            # Step 1: Options
            with wizard_content:
                ui.label("Choose a recovery method:").classes("font-semibold text-sm")

                # Option 1: Restore to original paths
                def select_original():
                    run_recovery(restore_to_original=True)

                ui.button(
                    "Restore to Original Folders", on_click=select_original
                ).classes("w-full bg-blue-600 text-white").props(
                    'aria-label="Restore to Original Folders"'
                )

                ui.label("OR").classes(
                    "text-center w-full text-xs font-bold text-gray-400"
                )

                # Option 2: Export to custom folder
                default_export_path = os.path.join(base_dir, "Recovered Files")
                export_input = ui.input(
                    label="Custom Export Folder", value=default_export_path
                ).classes("w-full")

                def on_browse_click():
                    def on_dir_selected(path):
                        if path:
                            export_input.set_value(path)

                    ask_directory_async(
                        None, "Select Export Folder", on_dir_selected, None, None
                    )

                with ui.row().classes("w-full items-center gap-2"):
                    ui.button("Browse", on_click=on_browse_click).classes(
                        "bg-gray-200 text-black"
                    )

                    def select_export():
                        custom_path = export_input.value.strip()
                        if not custom_path:
                            ui.notify(
                                "Please specify or browse for a custom export folder.",
                                type="warning",
                            )
                            return
                        run_recovery(restore_to_original=False, custom_path=custom_path)

                    ui.button(
                        "Export to Custom Folder", on_click=select_export
                    ).classes("bg-green-600 text-white flex-1").props(
                        'aria-label="Export to Custom Folder"'
                    )

            def run_recovery(restore_to_original=True, custom_path=None):
                # Clear step 1 content
                wizard_content.clear()

                with wizard_content:
                    ui.label("Recovering files... Please wait.").classes(
                        "font-semibold text-sm"
                    )
                    progress = ui.linear_progress(value=0).classes("w-full")
                    status_lbl = ui.label("Initializing...").classes(
                        "text-xs text-gray-500"
                    )

                async def do_work():
                    total = len(files_to_recover)
                    success_count = 0
                    errors = []

                    for idx, (full_path, rel_path) in enumerate(files_to_recover):
                        try:
                            # Determine target directory and filename
                            if restore_to_original:
                                target_full_path = os.path.join(base_dir, rel_path)
                            else:
                                target_full_path = os.path.join(custom_path, rel_path)

                            dest_dir = os.path.dirname(target_full_path)
                            filename = os.path.basename(rel_path)

                            os.makedirs(dest_dir, exist_ok=True)

                            # Apply safe pathing rules (prevent overwrites)
                            safe_dst = get_safe_path(
                                dest_dir, filename, source_path=full_path
                            )

                            # Move file
                            shutil.move(full_path, safe_dst)
                            success_count += 1
                        except Exception as ex:
                            errors.append(f"{rel_path}: {str(ex)}")

                        progress.set_value((idx + 1) / total if total > 0 else 1.0)
                        status_lbl.set_text(f"Recovered {idx + 1} of {total} files...")
                        await asyncio.sleep(0.01)

                    # Update session status to 'resolved' and prune/clear hidden folder
                    try:
                        shutil.rmtree(safety_folder, ignore_errors=True)

                        # Also check if .branches is empty and remove it if so
                        branches_dir = os.path.dirname(safety_folder)
                        if os.path.exists(branches_dir) and not os.listdir(
                            branches_dir
                        ):
                            shutil.rmtree(branches_dir, ignore_errors=True)

                        if os.path.exists(history_db_path):
                            await asyncio.to_thread(
                                _update_session_resolved_sync,
                                history_db_path,
                                session_id,
                            )
                    except Exception as db_ex:
                        errors.append(f"DB update failed: {str(db_ex)}")

                    wizard_content.clear()
                    with wizard_content:
                        if errors:
                            ui.label("Recovery completed with errors:").classes(
                                "font-semibold text-sm text-red-500"
                            )
                            with ui.scroll_area().classes("h-32 w-full border p-2"):
                                for err in errors:
                                    ui.label(err).classes("text-xs text-red-500")
                        else:
                            ui.label("All files successfully recovered!").classes(
                                "font-semibold text-sm text-green-600"
                            )
                            ui.label(
                                "The hidden safety folder has been cleared, and the session status is updated to resolved."
                            ).classes("text-xs text-gray-600")

                        def on_finish():
                            dialog.close()
                            if self.base_dir:
                                self.start_analysis()

                        ui.button("Finish", on_click=on_finish).classes(
                            "w-full bg-blue-600 text-white mt-4"
                        ).props('aria-label="Finish Button"')

                asyncio.create_task(do_work())

        dialog.open()

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
        from app.core.offline_loader import detect_offline_bundle

        detection = detect_offline_bundle("model")

        if self.settings.AI_CONSENT_GRANTED is None:
            if detection["bundle_found"]:
                self.settings.AI_CONSENT_GRANTED = True
            from app.ui.wizard import show_wizard

            show_wizard(self, self.settings)
        elif self.settings.AI_CONSENT_GRANTED is True and detection["bundle_found"]:
            return

    def show_settings_view(self):
        """Show the settings dialog."""
        from app.ui.settings import show_settings

        show_settings(self, self.settings)

    def show_help_view(self):
        """Display help information."""
        from app.ui.help_modal import show_help

        show_help(self)

    def select_directory(self):
        """Prompt the user to select a directory for analysis."""

        def on_selected(path):
            if path:
                self.base_dir = path
                if hasattr(self, "path_input"):
                    self.path_input.set_value(path)
                self.start_analysis()

        ask_directory_async(None, "Select Directory", on_selected, None, None)

    def on_scan_clicked(self):
        """Handle click on Scan & Organize button."""
        if hasattr(self, "path_input"):
            val = self.path_input.value.strip()
            if val and os.path.isdir(val):
                self.base_dir = os.path.abspath(val)
                self.start_analysis()
            else:
                ui.notify(
                    "Please enter a valid existing folder directory.", type="warning"
                )
        elif self.base_dir:
            self.start_analysis()

    def load_preset(self, path: str):
        """Load a quick preset folder path."""
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path) and os.path.isdir(abs_path):
            if hasattr(self, "path_input"):
                self.path_input.set_value(abs_path)
            self.base_dir = abs_path
            self.start_analysis()
        else:
            ui.notify(f"Preset path does not exist: {abs_path}", type="warning")

    def start_analysis(self):
        """Start the background analysis of the selected directory."""
        self.stop_watcher()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        self.plan = {}
        self.locked_files = {}
        self._ratings_cache = {}
        if hasattr(self, "undo_btn"):
            self.undo_btn.set_visibility(False)
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

            bypassed_files = await asyncio.to_thread(
                MetadataPass.run,
                self.app_session.base_dir,
                files,
                self.settings,
                self.app_session.db,
                None,
                lambda: getattr(self, "_cancel_analysis_flag", False),
            )
            self.completed_files += len(bypassed_files)
            if self.total_files > 0:
                self.progress_bar.set_value(self.completed_files / self.total_files)

            bypassed_set = set(bypassed_files)
            items_to_sort = [f for f in files if f not in bypassed_set]

            def file_progress_cb(pct):
                def update_ui():
                    if hasattr(self, "file_progress_bar"):
                        self.file_progress_bar.set_visibility(True)
                        self.file_progress_bar.set_value(pct)
                    if hasattr(self, "file_progress_label"):
                        self.file_progress_label.set_visibility(True)
                        self.file_progress_label.set_text(
                            f"Active file progress: {pct * 100:.1f}%"
                        )

                if self.loop:
                    self.loop.call_soon_threadsafe(update_ui)

            import inspect

            sig = inspect.signature(self.app_session.process_items_async)
            process_kwargs = {}
            if "progress_callback" in sig.parameters:
                process_kwargs["progress_callback"] = file_progress_cb

            async for (
                item,
                text,
                file_hash,
                was_skipped,
            ) in self.app_session.process_items_async(
                items_to_sort,
                lambda: getattr(self, "_cancel_analysis_flag", False),
                **process_kwargs,
            ):
                if self._cancel_analysis_flag or text == "[STATUS:CANCELLED]":
                    break

                if not was_skipped:
                    chunk = {item: {"text": text, "hash": file_hash}}
                    await asyncio.to_thread(self.app_session.partial_fit, chunk)

                self.completed_files += 1
                if self.total_files > 0:
                    self.progress_bar.set_value(self.completed_files / self.total_files)

                # Reset/hide file progress elements after processing an item
                if hasattr(self, "file_progress_bar"):
                    self.file_progress_bar.set_value(0)
                    self.file_progress_bar.set_visibility(False)
                if hasattr(self, "file_progress_label"):
                    self.file_progress_label.set_text("")
                    self.file_progress_label.set_visibility(False)

                if was_skipped:
                    msg = f"Processed {self.completed_files}/{self.total_files} files (skipped unchanged: {item})"
                    self.status_label.set_text(msg)
                    logger.info(msg)
                else:
                    msg = f"Processed {self.completed_files}/{self.total_files} files (extracted: {item})"
                    self.status_label.set_text(msg)
                    logger.info(msg)

                await asyncio.sleep(0.01)

            # Hide file progress elements when done
            if hasattr(self, "file_progress_bar"):
                self.file_progress_bar.set_visibility(False)
            if hasattr(self, "file_progress_label"):
                self.file_progress_label.set_visibility(False)

            if not self._cancel_analysis_flag:
                await asyncio.to_thread(self.load_locked_files_from_db)
                await asyncio.to_thread(self.load_ratings_from_db)
                self.plan = await asyncio.to_thread(
                    self.app_session.generate_sorting_plan
                )
                await self.verify_current_plan()
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
            self.update_ai_warning()
            self._rebuild_plan_async()

    def change_sorting_strategy(self, e):
        """Update sorting strategy."""
        strat = e.value
        self.sorting_strategy = strat
        self.settings.SORTING_STRATEGY = strat
        is_clinical = strat in ("clinical_tmf", "clinical_isf")
        if hasattr(self, "clinical_controls_row"):
            self.clinical_controls_row.set_visibility(is_clinical)
        if hasattr(self, "compliance_btn"):
            self.compliance_btn.set_visibility(is_clinical)

        if self.app_session and hasattr(self.app_session, "analyzer"):
            self.app_session.analyzer.strategy_name = strat
            from app.core.analyzer_strategies import clustering_registry

            strat_instance = clustering_registry.get_strategy(strat)
            if strat_instance and hasattr(strat_instance, "smart_renaming"):
                strat_instance.smart_renaming = getattr(
                    self.settings, "CLINICAL_SMART_RENAMING", False
                )
                strat_instance.generate_audit_report = getattr(
                    self.settings, "CLINICAL_GENERATE_AUDIT_REPORT", True
                )
                strat_instance.base_dir = self.base_dir
        self._rebuild_plan_async()

    def toggle_clinical_renaming(self, e):
        """Toggle smart clinical renaming."""
        self.clinical_smart_renaming = e.value
        self.settings.CLINICAL_SMART_RENAMING = e.value
        if self.app_session and hasattr(self.app_session, "analyzer"):
            from app.core.analyzer_strategies import clustering_registry

            strat = clustering_registry.get_strategy(self.settings.SORTING_STRATEGY)
            if strat and hasattr(strat, "smart_renaming"):
                strat.smart_renaming = e.value
        self._rebuild_plan_async()

    def toggle_clinical_audit_report(self, e):
        """Toggle clinical audit report generation."""
        self.clinical_generate_audit_report = e.value
        self.settings.CLINICAL_GENERATE_AUDIT_REPORT = e.value
        if self.app_session and hasattr(self.app_session, "analyzer"):
            from app.core.analyzer_strategies import clustering_registry

            strat = clustering_registry.get_strategy(self.settings.SORTING_STRATEGY)
            if strat and hasattr(strat, "generate_audit_report"):
                strat.generate_audit_report = e.value

    def show_compliance_checklist_dialog(self):
        """Show interactive ICH-GCP compliance audit checklist."""
        strat_name = getattr(self.settings, "SORTING_STRATEGY", "default")
        from app.core.analyzer_strategies import clustering_registry

        strat = clustering_registry.get_strategy(strat_name)
        comp_data = getattr(strat, "last_compliance_result", None) if strat else None

        with (
            ui.dialog() as dialog,
            ui.card().classes(
                get_dialog_card_classes("lg") + " p-6 max-h-[85vh] overflow-y-auto"
            ),
        ):
            dialog.props('aria-label="Compliance Audit Checklist Dialog"')
            with ui.row().classes("w-full justify-between items-center"):
                ui.label("ICH-GCP Regulatory Compliance Checklist").classes(
                    "text-h6 font-bold text-gray-800"
                )
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")

            if not comp_data:
                ui.label(
                    "No compliance audit data available yet. Please select a directory and run sorting first."
                ).classes("text-gray-500 my-4")
                ui.button("Close", on_click=dialog.close).props('color="grey"')
                dialog.open()
                return

            score = comp_data.get("compliance_score_percent", 0.0)
            status = comp_data.get("audit_readiness_status", "UNKNOWN")
            badge_color = (
                "positive" if score >= 90 else "warning" if score >= 60 else "negative"
            )

            with ui.row().classes(
                "w-full items-center gap-4 my-2 p-3 bg-gray-50 rounded border"
            ):
                ui.badge(f"{status} ({score}%)", color=badge_color).classes(
                    "text-sm p-2"
                )
                ui.label(
                    f"Essential Found: {comp_data.get('total_essential_found', 0)} / {comp_data.get('total_essential_required', 0)}"
                ).classes("font-semibold")
                ui.label(
                    f"Missing Gaps: {comp_data.get('total_essential_missing', 0)}"
                ).classes("text-red-500 font-semibold")

            missing_docs = comp_data.get("missing_essential_documents", [])
            if missing_docs:
                ui.label(
                    "Missing Regulatory Essential Documents (Action Required):"
                ).classes("text-sm font-bold text-red-600 mt-2")
                with ui.column().classes("w-full gap-1 pl-2"):
                    for m in missing_docs:
                        with ui.row().classes(
                            "items-center gap-2 text-xs text-red-700"
                        ):
                            ui.icon("warning", size="xs", color="red")
                            ui.label(
                                f"{m['title']} ({m['gcp_ref']}) - {m['importance']}"
                            )

            found_docs = comp_data.get("found_essential_documents", [])
            if found_docs:
                ui.label("Verified & Present Documents:").classes(
                    "text-sm font-bold text-green-700 mt-3"
                )
                with ui.column().classes("w-full gap-1 pl-2"):
                    for f in found_docs:
                        with ui.row().classes(
                            "items-center gap-2 text-xs text-green-800"
                        ):
                            ui.icon("check_circle", size="xs", color="green")
                            ui.label(
                                f"{f['title']} ({f['count']} file{'s' if f['count'] > 1 else ''})"
                            )

            with ui.row().classes("w-full justify-end mt-4 gap-2"):
                report_html = (
                    os.path.join(self.base_dir, "compliance_audit_report.html")
                    if self.base_dir
                    else ""
                )
                if report_html and os.path.exists(report_html):

                    def open_report():
                        import webbrowser

                        webbrowser.open(f"file://{report_html}")

                    ui.button("Open Full HTML Dossier", on_click=open_report).props(
                        'color="primary" outline'
                    )
                ui.button("Close", on_click=dialog.close).props('color="grey"')
        dialog.open()

    def show_cro_forensic_dialog(self):
        """Display the dedicated CRO multi-study forensic ingestion modal."""
        from app.ui.cro_forensic_view import CROForensicView

        view = CROForensicView(self.settings)
        view.show_dialog()

    def show_ml_warning_dialog(self, feature_name: str):
        """Show a clear, non-blocking warning dialogue explaining that the feature requires the full ML package."""
        with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("md")):
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
            with ui.row().classes("w-full justify-end flex-wrap gap-2"):
                ui.button("OK", on_click=dialog.close).classes(
                    "bg-blue-500 text-white"
                ).props('aria-label="Warning OK Button"')
        dialog.open()

    def _rebuild_plan_async(self):
        if not self.app_session or not self.base_dir:
            return

        if getattr(self, "_sorting_in_progress", False):
            return

        import threading

        # Cancel any previous task's cancellation token immediately
        if hasattr(self, "_current_recalc_token") and self._current_recalc_token:
            self._current_recalc_token.set()

        # Create isolated cancellation token for this run
        token = threading.Event()
        self._current_recalc_token = token

        if self._debounce_task:
            self._debounce_task.cancel()

        async def delayed_run(token):
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                token.set()
                return

            if token.is_set():
                return

            self.recalc_dialog.open()
            self.status_label.set_text("Rebuilding plan...")

            def check_cancel():
                return token.is_set()

            try:
                plan = await asyncio.to_thread(
                    self.app_session.analyzer.generate_sorting_plan,
                    self.base_dir,
                    self.settings,
                    self.locked_files,
                    check_cancel,
                )

                if token.is_set():
                    self.status_label.set_text("Recalculation cancelled.")
                    return

                self.plan = plan
                await self.verify_current_plan()
                if token.is_set():
                    return
                self.render_tree()
                self.status_label.set_text("Plan rebuilt.")
            except Exception as e:
                logger.error(f"Error rebuilding plan: {e}")
                self.status_label.set_text("Error rebuilding plan.")
            finally:
                if getattr(self, "_current_recalc_token", None) == token:
                    self.recalc_dialog.close()

        self._debounce_task = asyncio.create_task(delayed_run(token))

    def load_locked_files_from_db(self):
        """Load user-verified target paths as locks from the database."""
        if not self.app_session or not self.base_dir:
            return
        try:
            docs = self.app_session.db.get_all_documents(self.base_dir)
            for d in docs:
                if len(d) > 3 and d[3]:
                    self.locked_files[d[0]] = d[3]
        except Exception as e:
            logger.error(f"Error loading locked files: {e}")

    def load_ratings_from_db(self):
        """Load document ratings from the database into the in-memory cache."""
        if not self.app_session or not self.base_dir:
            return
        try:
            self._ratings_cache = self.app_session.db.get_all_document_ratings(
                self.base_dir
            )
        except Exception as e:
            logger.error(f"Error loading ratings from DB: {e}")

    def expand_all_nodes(self):
        """Expand all nodes in the tree view."""
        if hasattr(self, "tree_view"):
            try:
                self.tree_view.run_method("expandAll")
            except Exception:
                pass

    def collapse_all_nodes(self):
        """Collapse all nodes in the tree view."""
        if hasattr(self, "tree_view"):
            try:
                self.tree_view.run_method("collapseAll")
            except Exception:
                pass

    def show_new_folder_dialog(self):
        """Display dialog to create a new destination folder category in the plan."""
        with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("md")):
            ui.label("Create New Folder Category").classes(
                "text-lg font-bold text-slate-800"
            )
            name_input = ui.input(
                label="Folder Name", placeholder="e.g. Invoices, Contracts, Photos"
            ).classes("w-full mb-3")

            def on_confirm():
                folder_name = name_input.value.strip()
                if not folder_name:
                    ui.notify("Please enter a valid folder name.", type="warning")
                    return
                if folder_name not in self.plan:
                    self.plan[folder_name] = {}
                    self.render_tree()
                    dialog.close()
                    ui.notify(f"Folder '{folder_name}' created.", type="positive")
                else:
                    ui.notify(
                        f"Folder '{folder_name}' already exists in plan.",
                        type="warning",
                    )

            name_input.on("keydown.enter", on_confirm)

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button("Cancel", on_click=dialog.close).props('flat color="grey-7"')
                ui.button("Create Folder", on_click=on_confirm).classes(
                    "bg-blue-600 text-white"
                ).props("unelevated")
        dialog.open()

    def show_rename_folder_dialog(self, e):
        """Display dialog to rename an existing destination folder category."""
        folder_id = e.args.get("folder_id", "")
        if not folder_id:
            return
        old_name = folder_id.replace("\\", "/").split("/")[-1]
        with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("md")):
            ui.label(f"Rename Folder: {old_name}").classes(
                "text-lg font-bold text-slate-800"
            )
            name_input = ui.input(label="New Folder Name", value=old_name).classes(
                "w-full mb-3"
            )

            def on_rename():
                new_name = name_input.value.strip()
                if not new_name or new_name == old_name:
                    dialog.close()
                    return

                # If top-level folder
                if folder_id in self.plan:
                    self.plan[new_name] = self.plan.pop(folder_id)
                else:
                    # Nested folder
                    parts = folder_id.replace("\\", "/").split("/")
                    current = self.plan
                    for p in parts[:-1]:
                        if p in current and isinstance(current[p], dict):
                            current = current[p]
                    if parts[-1] in current:
                        current[new_name] = current.pop(parts[-1])

                # Update locks that pointed to this folder
                for f_key, target in list(self.locked_files.items()):
                    if target == folder_id or target == old_name:
                        self.locked_files[f_key] = new_name
                        if self.app_session:
                            self.app_session.db.set_user_verified_target_path(
                                self.base_dir, f_key, new_name
                            )

                self.render_tree()
                dialog.close()
                ui.notify(f"Renamed '{old_name}' to '{new_name}'.", type="positive")

            name_input.on("keydown.enter", on_rename)

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button("Cancel", on_click=dialog.close).props('flat color="grey-7"')
                ui.button("Rename", on_click=on_rename).classes(
                    "bg-blue-600 text-white"
                ).props("unelevated")
        dialog.open()

    def handle_node_toggle_lock(self, e):
        """Toggle the lock status of a file node."""
        file_id = e.args.get("file_id")
        if not file_id:
            return
        file_key = file_id.replace("\\", "/").split("/")[-1]
        is_currently_locked = (
            file_key in self.locked_files or file_id in self.locked_files
        )

        if is_currently_locked:
            self.locked_files.pop(file_key, None)
            self.locked_files.pop(file_id, None)
            if self.app_session:
                self.app_session.db.set_user_verified_target_path(
                    self.base_dir, file_key, None
                )
            ui.notify(f"Unlocked '{file_key}'", type="info")
        else:
            # Find the folder it is currently placed in
            parent_folder = None
            if "/" in file_id:
                parent_folder = file_id.rsplit("/", 1)[0]
            else:
                for folder, contents in self.plan.items():
                    if isinstance(contents, dict) and file_key in contents:
                        parent_folder = folder
                        break
            if parent_folder:
                self.locked_files[file_key] = parent_folder
                if self.app_session:
                    self.app_session.db.set_user_verified_target_path(
                        self.base_dir, file_key, parent_folder
                    )
                ui.notify(f"Locked '{file_key}' to '{parent_folder}'", type="positive")

        self.render_tree()

    def handle_node_drop(self, e):
        """Handle drag-and-drop of a file node onto a folder node."""
        try:
            source_id = e.args.get("source")
            target_folder = e.args.get("target")

            if not source_id or not target_folder:
                return

            # Safe folder path cleaning
            target_folder = target_folder.strip("/")

            # Clean leaf filename
            file_key = source_id.replace("\\", "/").split("/")[-1]

            # Perform the in-memory move
            file_info = find_and_remove_file(self.plan, source_id)
            if file_info is None:
                file_info = find_and_remove_file(self.plan, file_key)

            if file_info is not None:
                file_info["is_locked"] = True
                file_info["status"] = "Locked"
                file_info["routed_by"] = "manual"

                insert_file_into_plan(self.plan, target_folder, file_key, file_info)
                self.locked_files[file_key] = target_folder

                if self.app_session:
                    self.app_session.db.set_user_verified_target_path(
                        self.base_dir, file_key, target_folder
                    )

                self.render_tree()
                ui.notify(
                    f"Moved '{file_key}' to '{target_folder}' (Locked)",
                    type="positive",
                )
            else:
                logger.warning(f"Could not find file {source_id} in current plan.")
        except Exception as ex:
            logger.error(f"Error handling node drop: {ex}", exc_info=True)
            ui.notify(f"Failed to move file: {ex}", type="negative")

    def handle_node_rate(self, e):
        """Handle quality rating of a file node."""
        try:
            file_filepath = e.args.get("file_id")
            rating = e.args.get("rating")

            if not file_filepath or not rating:
                return

            current_rating = getattr(self, "_ratings_cache", {}).get(file_filepath)
            if current_rating == rating:
                rating_to_set = None
            else:
                rating_to_set = rating

            if hasattr(self, "_ratings_cache"):
                if rating_to_set:
                    self._ratings_cache[file_filepath] = rating_to_set
                else:
                    self._ratings_cache.pop(file_filepath, None)

            if self.app_session:
                self.app_session.db.set_document_rating(
                    self.base_dir, file_filepath, rating_to_set
                )

            self.render_tree()

            if rating_to_set:
                ui.notify(
                    f"Recorded {rating_to_set} rating for {os.path.basename(file_filepath)}",
                    type="positive",
                )
            else:
                ui.notify(f"Cleared rating for {os.path.basename(file_filepath)}")

        except Exception as ex:
            logger.error(f"Error handling node rate: {ex}", exc_info=True)
            ui.notify(f"Failed to record rating: {ex}", type="negative")

    def render_tree(self):
        """Render the tree view of the sorting plan and update folder/file badges."""
        if not self._ratings_cache and self.app_session and self.base_dir:
            self.load_ratings_from_db()
        if not self.locked_files and self.app_session and self.base_dir:
            self.load_locked_files_from_db()
        self.tree_nodes = []
        folder_count, file_count = self._flatten(self.plan, "", self.tree_nodes)
        if hasattr(self, "folder_count_badge"):
            self.folder_count_badge.set_text(
                f"{folder_count} folder{'s' if folder_count != 1 else ''}"
            )
        if hasattr(self, "file_count_badge"):
            self.file_count_badge.set_text(
                f"{file_count} file{'s' if file_count != 1 else ''}"
            )
        if hasattr(self, "tree_view"):
            self.tree_view._props["nodes"] = self.tree_nodes
            self.tree_view.update()

    def _flatten(self, node, current_path, nodes_list):
        folder_count = 0
        file_count = 0
        for k, v in sorted(
            node.items(),
            key=lambda x: (
                1 if (isinstance(x[1], dict) and x[1].get("__type__") == "file") else 0,
                x[0].lower(),
            ),
        ):
            node_id = f"{current_path}/{k}" if current_path else k
            if isinstance(v, dict) and v.get("__type__") != "file":
                folder_count += 1
                children = []
                nodes_list.append(
                    {
                        "id": node_id,
                        "text": k,
                        "children": children,
                        "icon": "folder",
                        "is_file": False,
                    }
                )
                sub_folders, sub_files = self._flatten(v, node_id, children)
                folder_count += sub_folders
                file_count += sub_files
            else:
                file_count += 1
                text = k
                icon = "insert_drive_file"
                is_locked = (
                    k in self.locked_files
                    or node_id in self.locked_files
                    or (isinstance(v, dict) and v.get("is_locked"))
                )
                if is_locked:
                    icon = "lock"

                badge = None
                badge_color = None
                if is_locked:
                    badge = "Locked"
                    badge_color = "amber-8"
                elif isinstance(v, dict):
                    routed_by = v.get("routed_by")
                    match_val = v.get("match")
                    if routed_by == "keyword":
                        badge = f"Rule: {match_val}" if match_val else "Keyword Rule"
                        badge_color = "blue-8"
                    elif routed_by == "pattern":
                        badge = f"Pattern: {match_val}" if match_val else "Pattern"
                        badge_color = "indigo-8"
                    elif routed_by == "historical":
                        badge = "Historical Match"
                        badge_color = "purple-8"
                    elif routed_by in ("ai", "semantic"):
                        badge = "AI Semantic"
                        badge_color = "emerald-8"

                if isinstance(v, dict):
                    status = v.get("status", "")
                    if status and not is_locked:
                        text += f" [{status}]"
                    if not is_locked and (
                        "error" in status.lower() or "locked" in status.lower()
                    ):
                        icon = "error"
                if k in self.plan_errors or node_id in self.plan_errors:
                    err_msg = self.plan_errors.get(node_id) or self.plan_errors.get(k)
                    text += f" (Error: {err_msg})"
                    icon = "error"

                rating = self._ratings_cache.get(node_id) or self._ratings_cache.get(k)
                nodes_list.append(
                    {
                        "id": node_id,
                        "text": text,
                        "icon": icon,
                        "is_file": True,
                        "filepath": node_id,
                        "is_locked": bool(is_locked),
                        "badge": badge,
                        "badge_color": badge_color,
                        "rating": rating,
                    }
                )
        return folder_count, file_count

    def execute_sort(self):
        """Execute the sorting plan with real-time two-phase progress and rollback availability."""
        if not self.app_session or not self.plan:
            return

        if getattr(self, "_sorting_in_progress", False):
            return

        self._sorting_in_progress = True

        # Immediately cancel any ongoing recalculation and its debounce task
        if self._debounce_task:
            try:
                self._debounce_task.cancel()
            except Exception:
                pass
        if hasattr(self, "_current_recalc_token") and self._current_recalc_token:
            self._current_recalc_token.set()
        try:
            self.recalc_dialog.close()
        except Exception:
            pass

        self.execute_btn.disable()
        if hasattr(self, "undo_btn"):
            self.undo_btn.set_visibility(False)
        self.status_label.set_text("Executing sort...")
        self.progress_bar.set_value(0)
        self.stop_watcher()

        def _split_plan_phases(plan):
            fast_plan = {}
            slow_plan = {}

            def _split_node(src_node, fast_node, slow_node):
                if not isinstance(src_node, dict) or src_node.get("__type__") in (
                    "file",
                    "directory",
                ):
                    return
                for k, v in src_node.items():
                    if isinstance(v, dict):
                        if v.get("__type__") == "file":
                            routed_by = v.get("routed_by")
                            if routed_by in ("keyword", "override", "learned"):
                                fast_node[k] = v
                            else:
                                slow_node[k] = v
                        elif v.get("__type__") == "directory":
                            fast_node[k] = v
                            slow_node[k] = v
                        else:
                            sub_fast = {}
                            sub_slow = {}
                            _split_node(v, sub_fast, sub_slow)
                            if sub_fast:
                                fast_node[k] = sub_fast
                            if sub_slow:
                                slow_node[k] = sub_slow
                    else:
                        fast_node[k] = v

            _split_node(plan, fast_plan, slow_plan)
            return fast_plan, slow_plan

        async def run():
            success = False
            try:
                # Derive fast-path and slow-path phases from the approved plan (self.plan)
                fast_path_plan, slow_path_plan = _split_plan_phases(self.plan)

                fast_path_summary = None
                if fast_path_plan:
                    # Phase 1: Fast-path deterministic moves
                    self.status_label.set_text(
                        "Phase 1/2: Executing fast-path rules..."
                    )
                    self.progress_bar.set_value(0.1)
                    fast_path_summary = await asyncio.to_thread(
                        self.app_session.execute_moves, fast_path_plan
                    )

                self.progress_bar.set_value(0.4)
                self.status_label.set_text(
                    "Phase 1 complete. Initiating AI classification..."
                )

                # Refresh DB cache states before initiating AI phase
                if hasattr(self.app_session, "db"):
                    self.app_session.db.invalidate_cache()
                await asyncio.to_thread(self.load_locked_files_from_db)
                await asyncio.to_thread(self.load_ratings_from_db)

                self.progress_bar.set_value(0.5)

                slow_path_summary = None
                if slow_path_plan:
                    self.status_label.set_text(
                        "Phase 2/2: Executing AI classification..."
                    )
                    slow_path_summary = await asyncio.to_thread(
                        self.app_session.execute_moves, slow_path_plan
                    )

                self.progress_bar.set_value(0.9)

                summary = {}
                for s in (fast_path_summary, slow_path_summary):
                    if s and isinstance(s, dict):
                        for k, v in s.items():
                            if isinstance(v, (int, float)):
                                summary[k] = summary.get(k, 0) + v
                            elif k not in summary:
                                summary[k] = v

                ui.notify(f"Sorted successfully: {summary}", type="positive")
                self.status_label.set_text(
                    "Sorting complete. You can click 'Undo Last Sort' to revert anytime."
                )
                self.progress_bar.set_value(1.0)
                if hasattr(self, "undo_btn"):
                    self.undo_btn.set_visibility(True)
                success = True
            except Exception as e:
                logger.error(f"Error executing sort: {e}")
                ui.notify(f"Error: {e}", type="negative")
                self.status_label.set_text("Sorting failed.")

                with (
                    ui.dialog() as error_dialog,
                    ui.card().classes(get_dialog_card_classes("md")),
                ):
                    ui.label("Move Transaction Error").classes("text-h6 text-red-500")
                    ui.label(f"The organization process failed: {e}").classes(
                        "text-body1"
                    )
                    ui.label(
                        "An automated rollback was successfully executed to restore files and index database."
                    ).classes("text-body2 text-gray-600")
                    with ui.row().classes("w-full justify-end mt-4 flex-wrap gap-2"):
                        ui.button("Close", on_click=error_dialog.close).props(
                            'color="primary" aria-label="Close Error Dialog"'
                        )
                error_dialog.open()
            finally:
                self._sorting_in_progress = False
                self.plan = {}
                self.render_tree()
                self.execute_btn.enable()
                self.start_watcher()
                if success and self.app_session:
                    try:
                        self.status_label.set_text(
                            "Running background classifier updates..."
                        )
                        await asyncio.to_thread(
                            run_incremental_training_in_background,
                            self.app_session,
                            self.base_dir,
                        )
                    except Exception as train_err:
                        logger.error(f"Error during incremental training: {train_err}")
                    self.app_session.close()
                    self.app_session = None
                    self.status_label.set_text("Sorting complete.")

        asyncio.create_task(run())

    def undo_last_sort(self):
        """Roll back the last sorting operation and restore all files."""
        if not self.base_dir:
            return

        async def run_undo():
            self.status_label.set_text("Rolling back files to previous locations...")
            self.progress_bar.set_value(0.5)
            try:
                from app.core.db import DocumentDB
                from app.core.session import auto_rollback_sync

                db = DocumentDB(self.base_dir)
                try:
                    await asyncio.to_thread(auto_rollback_sync, db, self.base_dir)
                finally:
                    db.close()

                if hasattr(self, "undo_btn"):
                    self.undo_btn.set_visibility(False)
                self.progress_bar.set_value(1.0)
                ui.notify(
                    "Rollback completed successfully! Files restored.", type="positive"
                )
                self.status_label.set_text("Rollback complete.")
                await asyncio.sleep(0.5)
                self.start_analysis()
            except Exception as e:
                logger.error(f"Rollback failed: {e}")
                ui.notify(f"Rollback failed: {e}", type="negative")
                self.status_label.set_text("Rollback failed.")

        asyncio.create_task(run_undo())

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

    async def verify_current_plan(self):
        """Run path integrity verification on the current plan and update warnings."""
        self.update_ai_warning()
        if not self.base_dir or not self.plan:
            if hasattr(self, "warnings_label"):
                self.warnings_label.set_text("")
                self.warnings_label.set_visibility(False)
            return

        from app.core.verifier import VerificationEngine

        integrity_result = await asyncio.to_thread(
            VerificationEngine.verify_plan_integrity, self.base_dir, self.plan
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

            for item in integrity_result.get("long_paths", []):
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

            warnings_text = "\n".join(integrity_result["warnings"])
            if hasattr(self, "warnings_label"):
                self.warnings_label.set_text(warnings_text)
                self.warnings_label.set_visibility(True)
        else:
            if hasattr(self, "warnings_label"):
                self.warnings_label.set_text("")
                self.warnings_label.set_visibility(False)

    def update_ai_warning(self):
        """Update the UI with any AI status warnings."""
        if not hasattr(self, "ai_warnings_label"):
            return

        async def _run():
            from app.core.verifier import check_ai_status

            is_healthy, warn_msg = await asyncio.to_thread(
                check_ai_status, self.settings
            )
            if not is_healthy:
                self.ai_warnings_label.set_text(
                    warn_msg or "AI models are corrupt or missing."
                )
                self.ai_warnings_label.set_visibility(True)
            else:
                self.ai_warnings_label.set_text("")
                self.ai_warnings_label.set_visibility(False)

        asyncio.create_task(_run())


def find_and_remove_file(node, file_key):
    """Recursively find and remove a file with key file_key in the plan node dictionary.

    Returns its value (the file info dict) or None if not found.
    """
    if not isinstance(node, dict):
        return None

    # If file_key is a relative path with slashes, we can traverse/pop it specifically
    if "/" in file_key or "\\" in file_key:
        parts = file_key.replace("\\", "/").split("/")
        current = node
        # We need to keep track of the path taken so we can clean up empty folders
        path_nodes = [(current, None)]  # list of tuples (node, key_used_to_get_here)
        for part in parts[:-1]:
            if part in current and isinstance(current[part], dict):
                next_node = current[part]
                path_nodes.append((next_node, part))
                current = next_node
            else:
                current = None
                break
        if current is not None:
            leaf_key = parts[-1]
            if (
                leaf_key in current
                and isinstance(current[leaf_key], dict)
                and current[leaf_key].get("__type__") == "file"
            ):
                val = current.pop(leaf_key)
                # Clean up empty parent directories up the chain
                for i in range(len(path_nodes) - 1, 0, -1):
                    p_node, p_key = path_nodes[i]
                    if not p_node:
                        parent_node, _ = path_nodes[i - 1]
                        parent_node.pop(p_key, None)
                return val

    # Fallback to the original logic
    if (
        file_key in node
        and isinstance(node[file_key], dict)
        and node[file_key].get("__type__") == "file"
    ):
        return node.pop(file_key)
    for k, v in list(node.items()):
        if isinstance(v, dict) and v.get("__type__") != "file":
            res = find_and_remove_file(v, file_key)
            if res is not None:
                if not v:
                    node.pop(k)
                return res
    return None


def insert_file_into_plan(plan, target_folder, file_key, file_info):
    """Insert a file into the plan under a target folder path."""
    parts = target_folder.replace("\\", "/").split("/")
    current = plan
    for part in parts:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[file_key] = file_info


def run_incremental_training_in_background(app_session, base_dir):
    """Background worker function that finds reassigned documents.

    Generates their vector embeddings and updates the document_vectors
    database table.
    """
    import logging

    try:
        logging.info(
            "Starting background incremental training for reassigned documents."
        )
        db = app_session.db
        analyzer = app_session.analyzer

        # 1. Fetch all documents for this base directory
        docs = db.get_all_documents(base_dir)
        if not docs:
            logging.info("No documents found for background incremental training.")
            return

        # 2. Filter for reassigned/verified documents
        reassigned_docs = []
        for d in docs:
            filepath = d[0]
            text = d[1]
            user_verified_target = d[3] if len(d) > 3 else None

            if user_verified_target and text:
                if text.startswith("[STATUS:"):
                    continue
                reassigned_docs.append((filepath, text))

        if not reassigned_docs:
            logging.info("No reassigned documents with text found to train on.")
            return

        # 3. Generate vectors for these documents
        vectors_to_upsert = []
        for filepath, text in reassigned_docs:
            try:
                vector = analyzer.embedding_manager.generate_embedding(text)
                if vector and analyzer.embedding_manager.validate_vector_dimension(
                    vector
                ):
                    vectors_to_upsert.append((filepath, vector))
            except Exception as e:
                logging.error(f"Error generating embedding for {filepath}: {e}")

        # 4. Upsert vectors into DB
        if vectors_to_upsert:
            db.upsert_document_vectors(
                base_dir,
                vectors_to_upsert,
                model_signature=analyzer.embedding_manager.signature,
            )
            logging.info(
                f"Successfully updated vectors for {len(vectors_to_upsert)} reassigned documents in the background."
            )
        else:
            logging.info("No new vectors generated for reassigned documents.")

    except Exception as e:
        logging.error(
            f"Error during background incremental training: {e}", exc_info=True
        )


def run_app(settings, directory=None, port=8080, show=True) -> None:
    """Run the NiceGUI application."""
    app_instance = AutoSorterApp(settings)
    if directory:
        if os.path.exists(directory):
            app_instance.base_dir = os.path.abspath(directory)
    app_instance.build_ui()
    ui.run(
        host="127.0.0.1",
        title="Smart AutoSorter AI Pro",
        port=port,
        reload=False,
        show=show,
    )
