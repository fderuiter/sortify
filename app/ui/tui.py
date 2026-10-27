"""Textual full-screen terminal interface with interactive tree and modal dialogs."""

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Log,
    ProgressBar,
    Select,
    Static,
    Tree,
)
from textual.widgets import Switch as Switch
from textual.widgets.tree import TreeNode

from app.ui.a11y import A11yMixin
from app.ui.a11y import A11yViolation as A11yViolation
from app.ui.a11y import inspect_tui_component as inspect_tui_component
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.mixins.pipeline import EXECUTE_NEEDS_SCAN_TOOLTIP, SortPipelineMixin
from app.ui.mixins.selection import SelectionActionsMixin
from app.ui.mixins.tree_view import TreeViewMixin
from app.ui.modals.drop_zone import DropPlanConfirmModal as DropPlanConfirmModal
from app.ui.modals.drop_zone import DropZoneModal
from app.ui.modals.edit import (
    DirectorySelectModal,
)
from app.ui.modals.edit import DuplicateReviewModal as DuplicateReviewModal
from app.ui.modals.edit import (
    ManualPathFallbackModal as ManualPathFallbackModal,
)
from app.ui.modals.edit import NewFolderModal as NewFolderModal
from app.ui.modals.edit import ReassignModal as ReassignModal
from app.ui.modals.edit import RenameModal as RenameModal
from app.ui.modals.help import ShortcutCheatSheetModal
from app.ui.modals.models import ModelManagerModal
from app.ui.modals.quarantine import QuarantineVaultModal
from app.ui.modals.reports import (
    AuditReportModal,
    ExportReportModal,
    ExtractionPreviewModal,
)
from app.ui.modals.rules import RuleImportModal as RuleImportModal
from app.ui.modals.rules import TemplateGalleryModal as TemplateGalleryModal
from app.ui.modals.session import (
    HistoryModal,
    SessionRecoveryModal,
)
from app.ui.modals.session import RollbackConfirmModal as RollbackConfirmModal
from app.ui.modals.session import RollbackErrorModal as RollbackErrorModal
from app.ui.modals.settings import ConsentRevocationModal as ConsentRevocationModal
from app.ui.modals.settings import ScrubArtifactsModal as ScrubArtifactsModal
from app.ui.modals.settings import (
    SettingsModal,
)
from app.ui.modals.settings import (
    _get_model_cache_footprint_mb as _get_model_cache_footprint_mb,
)
from app.ui.modals.settings import _get_vector_count as _get_vector_count
from app.ui.modals.wizard import WizardModal
from app.ui.widgets.density_slider import DensitySlider as DensitySlider
from app.ui.widgets.labeled_switch import LabeledSwitch as LabeledSwitch
from app.ui.widgets.path_diff import (
    _diff_highlight_paths as _diff_highlight_paths,
)
from app.ui.widgets.path_diff import (
    format_folder_path_comparison as format_folder_path_comparison,
)
from app.ui.widgets.path_diff import (
    format_side_by_side_path_comparison as format_side_by_side_path_comparison,
)
from app.ui.widgets.vim_tree import VimTree

logger = logging.getLogger(__name__)


class AutoSorterTUI(
    ResponsiveLayoutMixin,
    A11yMixin,
    SortPipelineMixin,
    SelectionActionsMixin,
    TreeViewMixin,
    App,
):
    """Textual full-screen interactive TUI application for Sortify AI Pro."""

    TITLE = "Sortify AI Pro - Terminal UI"
    SUB_TITLE = "Interactive Tree & Modal Controls"

    BINDINGS = [
        Binding("space", "toggle_selection", "Toggle Checkbox", show=True),
        Binding("x", "exclude_selected", "Exclude Checked", show=False),
        Binding("i", "include_selected", "Include Checked", show=False),
        Binding("r", "reassign_selected", "Reassign Checked", show=False),
        Binding("v", "visual_range_select", "Range Select", show=False),
        Binding("a", "toggle_select_all", "Select/Deselect All", show=False),
        Binding("ctrl+l", "toggle_lock", "Lock/Unlock", show=False),
        Binding("ctrl+r", "rename_node", "Rename Node", show=False),
        Binding("ctrl+n", "new_folder", "New Folder", show=False),
        Binding("plus", "rate_positive", "Rating (+)", show=False),
        Binding("minus", "rate_negative", "Rating (-)", show=False),
        Binding("ctrl+o", "open_settings", "Settings", show=False),
        Binding("ctrl+k", "open_model_manager", "Model Manager", show=False),
        Binding("ctrl+w", "open_wizard", "Wizard", show=False),
        Binding("ctrl+c", "open_cro_forensic", "CRO Ingest", show=False),
        Binding("ctrl+d", "open_dropzone", "DropZone", show=False),
        Binding("ctrl+s", "scan_directory", "Scan", show=True),
        Binding("ctrl+y", "open_history", "History View", show=False),
        Binding("ctrl+u", "open_quarantine_vault", "Quarantine Vault", show=False),
        Binding("ctrl+e", "execute_sort", "Execute Plan", show=True),
        Binding("ctrl+a", "open_audit_report", "Audit Report", show=False),
        Binding("ctrl+x", "open_extraction_preview", "Extraction Preview", show=False),
        Binding("ctrl+b", "select_dir", "Browse Dir", show=True),
        Binding("ctrl+f", "focus_search", "Search Tree", show=True),
        Binding("g", "generate_sample_corpus", "Sample Dataset", show=False),
        Binding("G", "generate_sample_corpus", "Sample Dataset", show=False),
        Binding("shift+g", "generate_sample_corpus", "Sample Dataset", show=False),
        Binding("question_mark", "open_cheat_sheet", "Help", show=True),
        Binding("f1", "open_cheat_sheet", "Help (F1)", show=False),
        Binding("ctrl+q", "quit", "Quit", show=True),
    ]

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #main-dual-pane {
        layout: horizontal;
        height: 1fr;
        width: 100%;
    }
    #main-dual-pane.narrow {
        layout: vertical;
    }
    #left-tree-pane {
        width: 50%;
        height: 100%;
        border: solid $primary;
        padding: 0;
    }
    #main-dual-pane.narrow #left-tree-pane {
        width: 100%;
        height: 1fr;
    }
    #tree-search-input {
        width: 100%;
        height: auto;
        border: none;
        border-bottom: solid $primary;
        margin: 0;
    }
    #plan-tree {
        width: 100%;
        height: 1fr;
        border: none;
        padding: 0;
    }
    #right-meta-pane {
        width: 50%;
        height: 100%;
        border: solid $secondary;
        padding: 1 2;
    }
    #main-dual-pane.narrow #right-meta-pane {
        width: 100%;
        height: 1fr;
    }
    .tui-log-area {
        height: 1fr;
        max-height: 10;
        min-height: 3;
        margin-top: 1;
        border: solid $secondary;
    }
    #main-progress-bar {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0;
    }
    #status-bar {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        padding: 0 1;
    }
    .meta-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .meta-line {
        margin-bottom: 1;
    }
    Tree:focus, Static:focus, Log:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    #alert-banner {
        width: 100%;
        height: auto;
        min-height: 1;
        max-height: 6;
        padding: 0 1;
        margin: 0;
        display: none;
    }
    #alert-banner.alert-critical {
        background: $error-darken-2;
        color: $text;
        border: solid $error;
        text-style: bold;
        display: block;
    }
    #alert-banner.alert-warning {
        background: $warning-darken-2;
        color: $text;
        border: solid $warning;
        display: block;
    }
    #btn-execute {
        margin-top: 1;
        width: 100%;
    }
    #onboarding-cta-card {
        border: solid $accent;
        padding: 1 2;
        margin-bottom: 1;
        background: $surface;
        height: auto;
    }
    #onboarding-guide-text {
        margin-bottom: 1;
    }
    #onboarding-workspace-state {
        margin-bottom: 1;
        color: $accent;
        text-style: bold;
    }
    #onboarding-cta-buttons {
        layout: vertical;
        height: auto;
    }
    #onboarding-cta-buttons Button {
        width: 100%;
        margin-bottom: 1;
    }
    #inspector-container {
        height: auto;
    }
    """

    def __init__(
        self,
        settings,
        base_dir: Optional[str] = None,
        source_dirs: Optional[List[str]] = None,
        skip_wizard: bool = False,
        non_interactive: bool = False,
        db: Optional[Any] = None,
    ):
        super().__init__()
        self.settings = settings
        self.db = db
        if source_dirs:
            self.source_dirs = [
                d if str(d).startswith("/") else os.path.abspath(d)
                for d in source_dirs
                if d
            ]
            self.base_dir = self.source_dirs[0] if self.source_dirs else ""
        elif base_dir and str(base_dir).startswith("/"):
            self.base_dir = str(base_dir)
            self.source_dirs = [self.base_dir]
        elif base_dir:
            self.base_dir = os.path.abspath(base_dir)
            self.source_dirs = [self.base_dir]
        else:
            self.base_dir = ""
            self.source_dirs = []
        self.skip_wizard = skip_wizard
        self.non_interactive = non_interactive
        self.plan: Dict[str, Any] = {}
        self.locked_files: Dict[str, str] = {}
        self._ratings_cache: Dict[str, str] = {}
        self.app_session = None
        self.active_tree_node = None
        self.visual_select_active: bool = False
        self.visual_selected_nodes: list = []
        self._wizard_dismissed_this_session: bool = False
        self.verification_result: Dict[str, Any] = {}
        self.has_critical_errors: bool = False
        self.critical_error_paths: set = set()
        self.warning_paths: set = set()

    def compose(self) -> ComposeResult:
        """Compose main dual-pane TUI layout."""
        yield Header(show_clock=True)
        with Horizontal(id="main-dual-pane"):
            with Vertical(id="left-tree-pane"):
                search_input = Input(
                    placeholder="Search tree nodes...",
                    id="tree-search-input",
                )
                search_input.tooltip = (
                    "Real-time search input field to filter preview tree nodes"
                )
                yield search_input
                tree = VimTree("Proposed Organization Plan", id="plan-tree")
                tree.tooltip = (
                    "Interactive tree view of proposed file organization plan"
                )
                yield tree
            with Vertical(id="right-meta-pane"):
                with Vertical(id="onboarding-cta-card"):
                    yield Heading(
                        "Workspace Setup & Quick Actions",
                        level=2,
                        classes="meta-header",
                    )
                    yield Static(
                        "Welcome to Sortify AI Pro. Select a workspace directory, run a scan, or load a sample dataset to get started.",
                        id="onboarding-guide-text",
                    )
                    initial_workspace_state = (
                        f"Active Workspace: {self.base_dir}"
                        if self.base_dir
                        else "Active Workspace: No directory selected"
                    )
                    yield Static(
                        initial_workspace_state, id="onboarding-workspace-state"
                    )
                    with Vertical(id="onboarding-cta-buttons"):
                        btn_select_dir = Button(
                            "Choose Directory [Ctrl+B]",
                            id="btn-cta-select-dir",
                            variant="primary",
                        )
                        btn_select_dir.tooltip = "Open directory selection dialog to choose workspace folder [Ctrl+B]"
                        yield btn_select_dir

                        btn_scan_dir = Button(
                            "Scan Directory [Ctrl+S]",
                            id="btn-cta-scan-dir",
                            variant="success",
                        )
                        btn_scan_dir.tooltip = "Initiate workspace scan to generate organization plan [Ctrl+S]"
                        yield btn_scan_dir

                        btn_sample_data = Button(
                            "Load Sample Dataset [G]",
                            id="btn-cta-sample-data",
                            variant="default",
                        )
                        btn_sample_data.tooltip = (
                            "Generate interactive sample dataset in workspace [G]"
                        )
                        yield btn_sample_data
                with Vertical(id="inspector-container"):
                    yield Heading(
                        "Side-by-Side Path Comparison", level=2, classes="meta-header"
                    )
                    path_comp = Static(
                        "Select a file or folder in the tree to compare source and target paths.",
                        id="path-comparison",
                    )
                    path_comp.tooltip = "Side-by-side split column view comparing source and destination target paths"
                    yield path_comp
                    yield Heading(
                        "Node Metadata Inspector", level=2, classes="meta-header"
                    )
                    meta_widget = Static(
                        "Select a node in the tree to inspect details.",
                        id="meta-details",
                    )
                    meta_widget.tooltip = "Metadata inspector panel displaying attributes of selected file or folder"
                    yield meta_widget
                yield Heading("Live Execution Log", level=2, classes="meta-header")
                tui_log = Log(id="tui-log", classes="tui-log-area")
                tui_log.tooltip = "Live operation execution log output feed"
                yield tui_log
                btn_exec = Button(
                    "Execute Plan [Ctrl+E]",
                    id="btn-execute",
                    variant="primary",
                    disabled=True,
                )
                btn_exec.tooltip = EXECUTE_NEEDS_SCAN_TOOLTIP
                yield btn_exec
        alert_banner = Static("", id="alert-banner")
        alert_banner.tooltip = (
            "Validation alert banner displaying blocking errors or warnings"
        )
        yield alert_banner
        pb = ProgressBar(total=100, show_percentage=True, id="main-progress-bar")
        pb.tooltip = "Active operation background progress indicator"
        pb.display = False
        yield pb
        initial_status = f"{self._get_ai_status_badge()} Ready. Press [Ctrl+S] to Scan or [Ctrl+B] to select Directory."
        sb = Static(
            initial_status,
            id="status-bar",
        )
        sb.tooltip = "Application status and screen reader announcement bar"
        yield sb
        yield Footer()

    def announce(
        self,
        message: str,
        priority: str = "polite",
        help_url: Optional[str] = None,
    ) -> str:
        """Emit screen reader announcement and log to live execution log feed."""
        res = super().announce(message, priority=priority, help_url=help_url)
        try:
            log_w = self.query_one("#tui-log", Log)
            display_msg = f"{res} [Help: {help_url}]" if help_url else res
            log_w.write_line(display_msg)
        except Exception:
            pass
        return res

    def _handle_progress_update(
        self,
        update_or_prog: Any = 0.0,
        stage: Optional[str] = None,
        unit_count: Optional[int] = None,
        unit_type: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Handle progress updates dispatched from worker threads to main UI thread."""
        try:
            pb = self.query_one("#main-progress-bar", ProgressBar)
            pb.display = True

            progress_val = 0.0
            stage_text = stage
            count_val = unit_count

            if hasattr(update_or_prog, "progress"):
                progress_val = getattr(update_or_prog, "progress", 0.0)
                if stage_text is None:
                    stage_text = getattr(update_or_prog, "stage", None)
                if count_val is None:
                    count_val = getattr(update_or_prog, "unit_count", None)
                if unit_type is None:
                    unit_type = getattr(update_or_prog, "unit_type", None)
            elif isinstance(update_or_prog, (int, float)):
                progress_val = float(update_or_prog)
            elif isinstance(update_or_prog, str):
                stage_text = update_or_prog

            # Convert 0.0-1.0 ratio to percentage (0.0-100.0) if needed
            if 0.0 <= progress_val <= 1.0:
                pct = progress_val * 100.0
            else:
                pct = progress_val

            pct = min(100.0, max(0.0, pct))
            pb.progress = pct

            if stage_text:
                status_msg = f"[{pct:.0f}%] {stage_text}"
                if count_val is not None:
                    u_type = unit_type or "items"
                    status_msg = f"[{pct:.0f}%] ({count_val} {u_type}) {stage_text}"
                sb = self.query_one("#status-bar", Static)
                sb.update(f"{self._get_ai_status_badge()} {status_msg}")
                try:
                    log_w = self.query_one("#tui-log", Log)
                    log_w.write_line(status_msg)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Error handling progress update: {e}")

    def reset_progress_bar(self) -> None:
        """Reset progress bar to idle state and hide component."""
        try:
            pb = self.query_one("#main-progress-bar", ProgressBar)
            pb.progress = 0.0
            pb.total = 100
            pb.display = False
        except Exception:
            pass

    def _update_layout(self, width: int) -> None:
        """Update container CSS classes based on viewport width breakpoint."""
        try:
            dual_pane = self.query_one("#main-dual-pane")
            if width < 100:
                dual_pane.add_class("narrow")
            else:
                dual_pane.remove_class("narrow")
        except Exception:
            pass

    def _start_hint(self) -> str:
        """Tell a user with no plan yet which key starts the workflow."""
        if self.base_dir:
            return f"No plan yet. Press Ctrl+S to scan {self.base_dir}, or Ctrl+B to choose another folder."
        return "No plan yet. Press Ctrl+B to choose a folder to sort."

    def on_mount(self) -> None:
        """Mount event handler."""
        self._update_layout(self.size.width)
        try:
            from app.ui.notifications import NotificationManager

            NotificationManager.get_instance().register_tui(self)
        except Exception:
            pass
        try:
            from app.config import AppSettings

            AppSettings.add_observer("AI_CONSENT_GRANTED", self._on_ai_consent_changed)
        except Exception:
            pass
        try:
            tree = self.query_one("#plan-tree", Tree)
            tree.show_root = True
            tree.root.expand()
            tree.focus()
        except Exception:
            pass
        self.announce(f"Ready. {self._start_hint()}")
        self._update_onboarding_cta_visibility()
        self.check_abandoned_sessions()

        env_non_interactive = os.environ.get("NON_INTERACTIVE")
        is_non_interactive_env = bool(
            env_non_interactive
            and env_non_interactive.strip().lower() in ("1", "true", "yes", "on")
        )
        env_consent = os.environ.get("SORTIFY_AI_CONSENT")
        if env_consent is not None:
            env_consent_clean = env_consent.strip().lower()
            if env_consent_clean in ("1", "true", "yes", "on"):
                self.settings.AI_CONSENT_GRANTED = True
            elif env_consent_clean in ("0", "false", "no", "off"):
                self.settings.AI_CONSENT_GRANTED = False

        is_bypass = (
            self.skip_wizard
            or self.non_interactive
            or getattr(self.settings, "_skip_wizard", False)
            or getattr(self.settings, "_non_interactive", False)
            or is_non_interactive_env
        )

        if getattr(self.settings, "AI_CONSENT_GRANTED", None) is None:
            if is_bypass:
                self.settings.AI_CONSENT_GRANTED = False
                if hasattr(self.settings, "_save"):
                    try:
                        self.settings._save()
                    except Exception:
                        pass
            elif not getattr(self, "_wizard_dismissed_this_session", False):
                self.call_after_refresh(self.action_open_wizard)

    def on_unmount(self) -> None:
        """Lifecycle hook called when application is unmounted."""
        try:
            from app.ui.notifications import NotificationManager

            NotificationManager.get_instance().unregister_tui(self)
        except Exception:
            pass
        try:
            from app.config import AppSettings

            AppSettings.remove_observer(
                "AI_CONSENT_GRANTED", self._on_ai_consent_changed
            )
        except Exception:
            pass

    @work
    async def check_abandoned_sessions(self) -> None:
        """Scan for abandoned sessions on startup and prompt for recovery if detected."""
        try:
            import asyncio

            from app.core.session_catalog import find_abandoned_sessions

            abandoned = await asyncio.to_thread(find_abandoned_sessions)
            if not abandoned:
                return

            session_info = abandoned[0]

            def on_recovery_choice(choice: Optional[str]) -> None:
                if not choice:
                    return
                if choice == "resume":
                    self._do_recovery_resume(session_info)
                elif choice == "rollback":
                    self._do_recovery_rollback(session_info)
                elif choice == "clean":
                    self._do_recovery_clean(session_info)

            self.push_screen(SessionRecoveryModal(session_info), on_recovery_choice)
        except Exception as e:
            logger.error(f"Error checking abandoned sessions: {e}")

    def _do_recovery_resume(self, session_info: Dict[str, Any]) -> None:
        """Resume interrupted sorting operation."""
        import json

        from app.core.session import AppSession

        base_dir = session_info.get("base_dir") or self.base_dir
        if base_dir:
            self.base_dir = base_dir

        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info.get("session_id")
        )

        plan_path = session_info.get("plan_path")
        if plan_path and os.path.exists(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f:
                    self.plan = json.load(f)
            except Exception:
                self.plan = self.app_session.generate_sorting_plan()
        else:
            self.plan = self.app_session.generate_sorting_plan()

        self.rebuild_tree()
        self.announce(
            f"Resuming session '{session_info.get('session_id')}'. Executing pending moves..."
        )
        self.action_execute_sort()

    def _do_recovery_rollback(self, session_info: Dict[str, Any]) -> None:
        """Rollback interrupted sorting operation."""
        from app.core.session_catalog import roll_back_abandoned_session

        session_id = session_info.get("session_id")
        info = {
            **session_info,
            "base_dir": session_info.get("base_dir") or self.base_dir,
        }
        if roll_back_abandoned_session(self.settings, info):
            self.announce(f"Rolled back session '{session_id}'.")
        else:
            self.announce(
                f"Session recovery failed for '{session_id}': write/permission error. Session files and safety storage preserved."
            )

        if self.base_dir:
            self.action_scan_directory()

    def _do_recovery_clean(self, session_info: Dict[str, Any]) -> None:
        """Leave an abandoned session as it is and stop offering to recover it."""
        from app.core.session_catalog import forget_abandoned_session

        forget_abandoned_session(session_info)
        self.announce(f"Cleaned session files for '{session_info.get('session_id')}'.")

    def _get_ai_status_badge(self) -> str:
        """Return visual AI consent status badge string."""
        consent = getattr(self.settings, "AI_CONSENT_GRANTED", None)
        return "[AI: Active]" if consent is True else "[AI: Disabled]"

    def _on_ai_consent_changed(self, value: Any = None) -> None:
        """Handle AI_CONSENT_GRANTED setting change notification."""
        self.update_status()
        consent_val = (
            value
            if value is not None
            else getattr(self.settings, "AI_CONSENT_GRANTED", None)
        )
        if consent_val is False or consent_val is None:
            try:
                from app.core.shared_registry import SharedModelRegistry

                SharedModelRegistry.get_instance().unload_all_models()
            except Exception:
                pass
            self.announce(
                "AI processing disabled. In-memory AI models unloaded instantly.",
                priority="high",
            )
        elif consent_val is True:
            self.announce("AI processing enabled.", priority="high")

    def update_status(self, text: Optional[str] = None) -> None:
        """Update status bar label with current message and AI consent badge."""
        try:
            if text is not None:
                clean_text = str(text)
                for badge_str in ("[AI: Active]", "[AI: Disabled]"):
                    if clean_text.startswith(badge_str):
                        clean_text = clean_text[len(badge_str) :].lstrip()
                self._current_status_text = clean_text
            elif not hasattr(self, "_current_status_text"):
                self._current_status_text = (
                    "Ready. Press [Ctrl+S] to Scan or [Ctrl+B] to select Directory."
                )

            badge = self._get_ai_status_badge()
            full_text = (
                f"{badge} {self._current_status_text}"
                if getattr(self, "_current_status_text", "")
                else badge
            )
            sb = self.query_one("#status-bar", Static)
            sb.update(full_text)
        except Exception:
            pass

    def _update_onboarding_cta_visibility(self) -> None:
        """Update visibility of onboarding CTA card vs metadata inspectors based on workspace state."""
        try:
            cta_card = self.query_one("#onboarding-cta-card")
            inspector = self.query_one("#inspector-container")
            ws_state = self.query_one("#onboarding-workspace-state", Static)

            base_dir = getattr(self, "base_dir", "")
            base_dir_text = base_dir if base_dir else "No directory selected"
            ws_state.update(f"Active Workspace: {base_dir_text}")

            has_plan_files = self._has_file_nodes(getattr(self, "plan", {}))
            active_node = getattr(self, "active_tree_node", None)
            has_selected_real_node = False
            if active_node and getattr(active_node, "data", None):
                data = active_node.data
                if isinstance(data, dict) and not data.get("is_empty_state"):
                    has_selected_real_node = True

            show_cta = not has_plan_files and not has_selected_real_node
            cta_card.display = show_cta
            inspector.display = not show_cta
        except Exception as e:
            logger.debug(f"Error updating onboarding CTA visibility: {e}")

    def _get_active_node(self) -> Optional[TreeNode]:
        try:
            tree = self.query_one("#plan-tree", Tree)
            if tree.cursor_node and tree.cursor_node.data:
                return tree.cursor_node
            if getattr(self, "active_tree_node", None) and self.active_tree_node.data:
                return self.active_tree_node
        except Exception:
            pass
        return None

    def _is_text_control_focused(self, allow_tree_search: bool = False) -> bool:
        """Check if a text input or selection control currently holds focus."""
        focused = self.focused
        if focused is None or not isinstance(focused, (Input, Select)):
            return False
        if allow_tree_search and getattr(focused, "id", None) == "tree-search-input":
            return False
        return True

    # --- Actions ---

    def action_select_dir(self) -> None:
        """Open directory selection modal."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        def on_selected(path: Optional[Union[str, List[str]]]) -> None:
            if not path:
                return
            paths = [path] if isinstance(path, str) else list(path)
            valid_paths = [os.path.abspath(p) for p in paths if p and os.path.exists(p)]
            if valid_paths:
                self.source_dirs = valid_paths
                self.base_dir = valid_paths[0]
                msg = f"Selected directory: {', '.join(self.source_dirs)}"
                self.announce(msg)
                self.action_scan_directory()
            else:
                from app.ui.notifications import notify

                err_msg = f"Target directory does not exist: {path}"
                self.announce(err_msg, priority="assertive")
                notify(err_msg, type="error")

        self.push_screen(
            DirectorySelectModal(
                current_dir=self.base_dir,
                source_dirs=getattr(self, "source_dirs", None),
            ),
            on_selected,
        )

    def action_generate_sample_corpus(self) -> None:
        """Generate sample document corpus in active workspace directory [G]."""
        if self._is_text_control_focused():
            return

        target_dir = self._sample_dataset_dir(self.base_dir or os.getcwd())

        from app.core.sample_corpus import generate_sample_corpus

        try:
            try:
                generate_sample_corpus(target_dir, overwrite=False)
                self.announce(f"Generated sample dataset in {target_dir}.")
            except FileExistsError:
                self.announce(f"Using the existing sample dataset in {target_dir}.")
        except Exception as exc:
            logger.error(f"Error generating sample dataset: {exc}")
            self.announce(f"Failed to generate sample dataset: {exc}")
            return
        self.base_dir = target_dir
        self.source_dirs = [target_dir]
        self.action_scan_directory()

    SAMPLE_DATASET_DIRNAME = "sortify-sample-dataset"

    def _sample_dataset_dir(self, folder: str) -> str:
        """Pick where [G] writes sample files without mixing them into user files.

        An empty folder (or the sample folder itself) is used as is; any other
        folder gets a dedicated ``sortify-sample-dataset`` subfolder.
        """
        if os.path.basename(os.path.normpath(folder)) == self.SAMPLE_DATASET_DIRNAME:
            return folder
        try:
            with os.scandir(folder) as entries:
                is_empty = not any(entries)
        except FileNotFoundError:
            is_empty = True
        return folder if is_empty else os.path.join(folder, self.SAMPLE_DATASET_DIRNAME)

    def action_open_settings(self) -> None:
        """Open settings modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        def on_saved(res: Optional[Dict[str, Any]]) -> None:
            if res:
                if "PROTECTED_PATHS" in res:
                    try:
                        self.settings.PROTECTED_PATHS = res["PROTECTED_PATHS"]
                    except Exception:
                        pass
                if "IGNORED_EXTENSIONS" in res:
                    self.settings.IGNORED_EXTENSIONS = res["IGNORED_EXTENSIONS"]
                if "MAX_WORKERS" in res:
                    try:
                        self.settings.MAX_WORKERS = res["MAX_WORKERS"]
                    except Exception:
                        pass
                if "MAX_FOLDERS" in res:
                    try:
                        self.settings.MAX_FOLDERS = res["MAX_FOLDERS"]
                    except Exception:
                        pass
                if "SORTING_STRATEGY" in res:
                    self.settings.SORTING_STRATEGY = res["SORTING_STRATEGY"]
                if "CLINICAL_SMART_RENAMING" in res:
                    self.settings.CLINICAL_SMART_RENAMING = res[
                        "CLINICAL_SMART_RENAMING"
                    ]
                if "CONTEXTUAL_RENAMING" in res:
                    self.settings.CONTEXTUAL_RENAMING = res["CONTEXTUAL_RENAMING"]
                if "AI_CONSENT_GRANTED" in res:
                    self.settings.AI_CONSENT_GRANTED = res["AI_CONSENT_GRANTED"]
                if "SPEAK_ANNOUNCEMENTS" in res:
                    self.settings.SPEAK_ANNOUNCEMENTS = res["SPEAK_ANNOUNCEMENTS"]
                if "PDF_TEXT_DENSITY_THRESHOLD" in res:
                    self.settings.PDF_TEXT_DENSITY_THRESHOLD = res[
                        "PDF_TEXT_DENSITY_THRESHOLD"
                    ]
                if "CONFLICT_POLICY" in res:
                    self.settings.CONFLICT_POLICY = res["CONFLICT_POLICY"]
                if hasattr(self.settings, "_save"):
                    self.settings._save()
                self.update_status()
                self.announce("Settings updated and saved.")
                if self.base_dir:
                    self.action_scan_directory()

        self.push_screen(SettingsModal(self.settings), on_saved)

    def action_open_extraction_preview(self) -> None:
        """Open visual extraction preview modal for active or selected PDF document [Ctrl+X]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        pdf_path: Optional[str] = None

        active_node = self._get_active_node()
        if active_node and active_node.data:
            data = active_node.data
            if data.get("is_file"):
                fp = (
                    data.get("filepath")
                    or data.get("info", {}).get("src_path")
                    or data.get("info", {}).get("path")
                )
                if fp and str(fp).lower().endswith(".pdf"):
                    pdf_path = str(fp)

        if not pdf_path:
            target_files = self._get_target_file_infos()
            for tf in target_files:
                fp = tf.get("src_path") or tf.get("path") or tf.get("filepath")
                if fp and str(fp).lower().endswith(".pdf"):
                    pdf_path = str(fp)
                    break

        if not pdf_path:
            self.announce(
                "Please select a PDF document to preview extraction strategies [Ctrl+X]."
            )
            return

        def on_preview_closed(saved_val: Optional[int]) -> None:
            if saved_val is not None:
                self.announce(f"Updated extraction density threshold to {saved_val}.")

        self.push_screen(
            ExtractionPreviewModal(pdf_path, self.settings), on_preview_closed
        )

    def action_open_model_manager(self) -> None:
        """Open model manager modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        self.push_screen(ModelManagerModal(self.settings))

    def action_open_wizard(self) -> None:
        """Open model onboarding wizard modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        self.push_screen(WizardModal(self.settings))

    def action_open_cro_forensic(self) -> None:
        """Open CRO multi-study forensic ingestion modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        from app.core.plugin_registry import PluginRegistry

        reg = PluginRegistry.get_instance()
        reg.load_plugins_from_settings(self.settings)
        view_cls = reg.get_tui_view("cro_forensic")
        if view_cls:
            self.push_screen(view_cls(self.settings, base_dir=self.base_dir))
        else:
            self.announce("CRO Forensic ingestion plugin is not enabled.")

    def action_open_cheat_sheet(self) -> None:
        """Open keyboard shortcut cheat sheet modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        self.push_screen(ShortcutCheatSheetModal())

    def action_open_dropzone(self) -> None:
        """Open floating dropzone overlay modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        def on_drop_done(res: Optional[Dict[str, Any]]) -> None:
            if res:
                cnt = res.get("processed_count", 0)
                self.announce(f"DropZone triage finished: {cnt} item(s) organized.")
                if self.base_dir and os.path.exists(self.base_dir):
                    self.action_scan_directory()

        self.push_screen(
            DropZoneModal(settings=self.settings, base_dir=self.base_dir),
            on_drop_done,
        )

    def action_export_simulation_report(self) -> None:
        """Trigger dry-run simulation report export modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        if not self.plan or not self.base_dir:
            self.announce("No plan available to export. Run [Ctrl+S] Scan first.")
            return

        default_out = str(Path(self.base_dir) / "simulation_report.html")

        def handle_export_modal_result(result: Optional[Tuple[str, str]]) -> None:
            if not result:
                return
            export_path, report_format = result
            try:
                from app.core.simulation_exporter import SimulationExporter

                exporter = SimulationExporter(self.plan, self.base_dir)
                if report_format == "json":
                    exporter.export_json(export_path)
                else:
                    exporter.export_html(export_path)

                self.announce(f"Simulation report exported to '{export_path}'.")
            except Exception as e:
                logger.error(f"Error exporting simulation report: {e}")
                self.announce(f"Export error: {e}")

        self.push_screen(
            ExportReportModal(default_path=default_out, default_format="html"),
            handle_export_modal_result,
        )

    def action_export_report(self) -> None:
        """Alias for action_export_simulation_report."""
        self.action_export_simulation_report()

    def action_open_history(self) -> None:
        """Trigger session history view and audit log export modal screen."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        from app.core.session_catalog import list_sessions

        sessions = list_sessions()

        def handle_history_modal_result(result: Optional[Dict[str, Any]]) -> None:
            if not result:
                return
            session_id = result["session_id"]
            output_path = result["output_path"]
            fmt = result["format"]

            try:
                from app.core.db_worker import DBWorker
                from app.core.store import open_store

                store = open_store()
                worker = DBWorker()
                db = store.database(worker)
                history_mgr = store.history(db, store.cache(worker))

                export_res = history_mgr.export_audit_log(
                    output_path=output_path,
                    session_id=session_id
                    if session_id not in ("none", "all")
                    else None,
                    format=fmt,
                    base_dir=self.base_dir,
                )
                cnt = export_res.get("count", 0)
                out_p = export_res.get("output_path", output_path)
                msg = (
                    f"Audit log exported ({fmt.upper()}) to '{out_p}' ({cnt} records)."
                )
                self.announce(msg)
                from app.ui.notifications import notify

                notify(msg, type="info")
            except Exception as e:
                logger.error(f"Error exporting audit log from TUI: {e}")
                self.announce(f"Export error: {e}")
                from app.ui.notifications import notify

                notify(f"Export error: {e}", type="error")

        self.push_screen(
            HistoryModal(sessions=sessions, base_dir=self.base_dir),
            handle_history_modal_result,
        )

    @on(Button.Pressed, "#btn-execute")
    def on_btn_execute_pressed(self) -> None:
        """Handle Execute Plan button press."""
        self.action_execute_sort()

    def action_open_audit_report(
        self, audit_report: Optional[Dict[str, Any]] = None
    ) -> None:
        """Open the Audit Report Modal Screen."""
        if not audit_report and self.app_session and self.plan:
            from app.core.audit_reporter import generate_audit_report
            from app.core.verifier import VerificationEngine

            base_dir = self.app_session.base_dir or "."
            moves = VerificationEngine.get_moves(base_dir, self.plan)
            records = [
                {"source_path": src, "destination_path": dst} for _, src, dst in moves
            ]
            audit_report = generate_audit_report(base_dir, records)

        if audit_report:
            self.push_screen(AuditReportModal(audit_report))
        else:
            self.announce(
                "No audit report available. Execute a sorting operation first."
            )

    def action_open_quarantine_vault(self) -> None:
        """Open the Quarantine Vault Modal Screen."""
        db = self.app_session.db if self.app_session else None
        base_dir = self.base_dir
        policies = (
            getattr(self.settings, "POLICIES", [])
            if hasattr(self, "settings") and self.settings
            else []
        )
        self.push_screen(
            QuarantineVaultModal(db=db, base_dir=base_dir, policies=policies)
        )

    @on(Button.Pressed, "#btn-cta-select-dir")
    def on_btn_cta_select_dir_pressed(self) -> None:
        """Handle Choose Directory CTA button press."""
        self.action_select_dir()

    @on(Button.Pressed, "#btn-cta-scan-dir")
    def on_btn_cta_scan_dir_pressed(self) -> None:
        """Handle Scan Directory CTA button press."""
        self.action_scan_directory()

    @on(Button.Pressed, "#btn-cta-sample-data")
    def on_btn_cta_sample_data_pressed(self) -> None:
        """Handle Load Sample Dataset CTA button press."""
        self.action_generate_sample_corpus()

    # --- Tree Management & Event Handlers ---

    @on(Tree.NodeHighlighted, "#plan-tree")
    @on(Tree.NodeSelected, "#plan-tree")
    def on_node_selected(
        self, event: Union[Tree.NodeSelected, Tree.NodeHighlighted]
    ) -> None:
        """Handle tree node selection or highlight to update metadata pane and screen reader announcement."""
        self.active_tree_node = event.node
        if event.node and getattr(event.node, "data", None):
            cdata = event.node.data
            if isinstance(cdata, dict):
                key = cdata.get("filepath") or cdata.get("folder") or cdata.get("key")
                if key:
                    self._last_cursor_key = key
        self._update_inspector(event.node)
        self._update_onboarding_cta_visibility()
        if isinstance(event, Tree.NodeSelected) and event.node and event.node.data:
            if event.node.data.get("is_empty_state"):
                action = event.node.data.get("action")
                if action == "generate_sample_corpus":
                    self.action_generate_sample_corpus()
                elif action == "select_dir":
                    self.action_select_dir()

    @on(Tree.NodeExpanded, "#plan-tree")
    @on(Tree.NodeCollapsed, "#plan-tree")
    def on_tree_node_expansion_changed(
        self, event: Union[Tree.NodeExpanded, Tree.NodeCollapsed]
    ) -> None:
        """Handle tree node expand/collapse to keep expansion state memory synchronized."""
        search_query = self._get_current_search_query()
        if not search_query and event.node and getattr(event.node, "data", None):
            cdata = event.node.data
            if isinstance(cdata, dict) and not cdata.get("is_file"):
                key = cdata.get("filepath") or cdata.get("folder") or cdata.get("key")
                if key:
                    if (
                        not hasattr(self, "_tree_expansion_memory")
                        or self._tree_expansion_memory is None
                    ):
                        self._tree_expansion_memory = {}
                    self._tree_expansion_memory[key] = isinstance(
                        event, Tree.NodeExpanded
                    )

    @on(Input.Changed, "#tree-search-input")
    def on_tree_search_changed(self, event: Input.Changed) -> None:
        """Handle real-time search input field change to dynamically filter preview tree nodes."""
        val = event.value
        timer = getattr(self, "_tree_filter_timer", None)
        if timer is not None:
            timer.stop()
        self._tree_filter_timer = self.set_timer(
            0.15, lambda: self.render_tree(search_query=val)
        )

    def on_key(self, event: events.Key) -> None:
        """Handle global application key events."""
        if event.key in ("escape", "esc"):
            try:
                inp = self.query_one("#tree-search-input", Input)
                if inp.has_focus:
                    inp.value = ""
                    timer = getattr(self, "_tree_filter_timer", None)
                    if timer is not None:
                        timer.stop()
                    self.render_tree(search_query="")
                    tree = self.query_one("#plan-tree", Tree)
                    tree.focus()
                    event.stop()
                    event.prevent_default()
            except Exception:
                pass

    def action_focus_search(self) -> None:
        """Focus the real-time tree filter search input bar."""
        try:
            inp = self.query_one("#tree-search-input", Input)
            inp.focus()
            inp.select_all()
            self.announce("Focused tree search input bar.")
        except Exception:
            pass

    # --- Checkbox Selection & Bulk Action Handlers ---

    # --- Keyboard Action Hotkeys ---

    async def action_quit(self) -> None:
        """Quit application [Ctrl+Q], from any focused widget."""
        await super().action_quit()


def run_tui(
    settings,
    base_dir: Optional[str] = None,
    skip_wizard: bool = False,
    non_interactive: bool = False,
) -> None:
    """Run the Textual full-screen terminal interface."""
    import shutil

    from app.core.path_utils import is_packaged

    if sys.platform == "win32" and is_packaged():
        if (
            sys.stdin is None
            or not hasattr(sys.stdin, "isatty")
            or not sys.stdin.isatty()
        ):
            from app.frozen_runtime import attach_console

            attach_console()

    if (
        sys.stdin is None
        or not hasattr(sys.stdin, "isatty")
        or not sys.stdin.isatty()
        or (hasattr(sys.stdout, "isatty") and not sys.stdout.isatty())
    ):
        if not os.environ.get("FORCE_TUI"):
            print(
                "Error: Textual TUI requires an interactive TTY terminal environment.",
                file=sys.stderr,
            )
            sys.exit(1)

    cols, lines = shutil.get_terminal_size((80, 24))
    if (cols < 80 or lines < 24) and not os.environ.get("IGNORE_TERMINAL_SIZE"):
        print(
            f"Error: Terminal dimensions ({cols}x{lines}) are below minimum requirement (80x24). "
            "Please resize your terminal window.",
            file=sys.stderr,
        )
        sys.exit(1)

    _start_multiprocessing_resource_tracker()

    app = AutoSorterTUI(
        settings=settings,
        base_dir=base_dir,
        skip_wizard=skip_wizard,
        non_interactive=non_interactive,
    )
    app.run()


def _start_multiprocessing_resource_tracker() -> None:
    """Start multiprocessing's resource tracker while stderr is still a real fd.

    The tracker starts lazily with the first spawned process and passes
    ``sys.stderr.fileno()`` to its child. Textual replaces ``sys.stderr``
    while running, so starting it later (when clustering spawns its worker)
    fails with ``ValueError: bad value(s) in fds_to_keep``.
    """
    if sys.platform == "win32":
        return
    try:
        from multiprocessing import resource_tracker

        resource_tracker.ensure_running()
    except Exception as e:
        logger.warning(f"Could not start multiprocessing resource tracker: {e}")


def __getattr__(name: str) -> Any:
    """Lazy module attribute getter for dynamic extension component resolution."""
    if name == "CROForensicModal":
        from app.plugins.clinical_compliance.tui_views import CROForensicModal

        return CROForensicModal
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
