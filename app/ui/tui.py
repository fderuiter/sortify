"""Textual full-screen terminal interface with interactive tree and modal dialogs."""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
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
from app.ui.modals.drop_zone import DropPlanConfirmModal as DropPlanConfirmModal
from app.ui.modals.drop_zone import DropZoneModal
from app.ui.modals.edit import (
    DirectorySelectModal,
    DuplicateReviewModal,
    NewFolderModal,
    ReassignModal,
    RenameModal,
)
from app.ui.modals.edit import (
    ManualPathFallbackModal as ManualPathFallbackModal,
)
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
from app.ui.notifications import TROUBLESHOOTING_URL
from app.ui.widgets.density_slider import DensitySlider as DensitySlider
from app.ui.widgets.labeled_switch import LabeledSwitch as LabeledSwitch
from app.ui.widgets.path_diff import (
    _diff_highlight_paths as _diff_highlight_paths,
)
from app.ui.widgets.path_diff import (
    format_folder_path_comparison,
    format_side_by_side_path_comparison,
)
from app.ui.widgets.vim_tree import VimTree

logger = logging.getLogger(__name__)

EXECUTE_NEEDS_SCAN_TOOLTIP = "Execution disabled: Scan a folder first [Ctrl+S]"


class SessionRecoveryModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[str]]
):
    """Modal dialog for recovering interrupted file sorting sessions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    SessionRecoveryModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 80;
        min-width: 30;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .narrow .modal-box {
        padding: 0 1;
        width: 95%;
    }
    .modal-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, session_info: Dict[str, Any]):
        super().__init__()
        self.session_info = session_info

    def compose(self) -> ComposeResult:
        """Compose session recovery modal children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Interrupted Session Detected",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(f"Session ID: {self.session_info.get('session_id', 'Unknown')}")
            yield Label(
                f"Target Directory: {self.session_info.get('base_dir', 'Unknown')}"
            )
            yield Label(f"Status: {self.session_info.get('status', 'Unknown')}")
            yield Heading("Select session recovery action:", level=2)

            with Horizontal(classes="button-row"):
                btn_clean = Button("Clean", id="btn-clean", variant="error")
                btn_clean.tooltip = "Discard session files and clean workspace"
                yield btn_clean

                btn_rollback = Button("Rollback", id="btn-rollback", variant="warning")
                btn_rollback.tooltip = (
                    "Rollback partial file moves from interrupted run"
                )
                yield btn_rollback

                btn_help = Button("Help", id="btn-help", variant="default")
                btn_help.tooltip = "Open session recovery troubleshooting guide"
                yield btn_help

                btn_resume = Button("Resume", id="btn-resume", variant="primary")
                btn_resume.tooltip = "Resume pending file moves for interrupted run"
                yield btn_resume

    def on_mount(self) -> None:
        """Focus resume button on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#btn-resume", Button).focus()
        self.announce("Opened interrupted session recovery dialog.")

    @on(Button.Pressed, "#btn-resume")
    def action_resume(self) -> None:
        """Confirm resume action."""
        self.announce("Confirmed session resume.")
        self.dismiss("resume")

    @on(Button.Pressed, "#btn-rollback")
    def action_rollback(self) -> None:
        """Confirm rollback action."""
        self.announce("Confirmed session rollback.")
        self.dismiss("rollback")

    @on(Button.Pressed, "#btn-clean")
    def action_clean(self) -> None:
        """Confirm clean action."""
        self.announce("Confirmed session clean.")
        self.dismiss("clean")

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open session recovery troubleshooting guide."""
        url = f"{TROUBLESHOOTING_URL}#session-recovery"
        from app.ui.notifications import notify

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel recovery dialog."""
        self.announce("Cancelled session recovery dialog.")
        self.dismiss(None)


class RollbackConfirmModal(A11yMixin, ModalScreen[Optional[Dict[str, Any]]]):
    """Confirmation modal dialog before executing session rollback."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel rollback dialog", show=True),
    ]

    CSS = """
    RollbackConfirmModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .confirm-box {
        padding: 1 2;
        background: $panel;
        border: thick $warning;
        width: 85%;
        max-width: 75;
        height: auto;
    }
    .modal-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    .warning-box {
        margin-top: 1;
        margin-bottom: 1;
        padding: 1;
        background: $boost;
        border: dashed $warning;
        color: $text;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    """

    def __init__(self, session_info: Dict[str, Any]):
        super().__init__()
        self.session_info = session_info

    def compose(self) -> ComposeResult:
        """Compose confirmation modal widgets."""
        sid = self.session_info.get("session_id", "unknown")
        b_dir = self.session_info.get("base_dir") or "N/A"
        step_cnt = (
            self.session_info.get("step_count")
            or self.session_info.get("file_count")
            or 0
        )
        ts = self.session_info.get("timestamp")
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "N/A"

        with Vertical(classes="confirm-box"):
            yield Heading(
                "Confirm Session Rollback",
                level=1,
                classes="modal-title",
                id="confirm-title",
            )
            yield Label(f"Session ID: {sid}", id="lbl-confirm-sid")
            yield Label(f"Base Directory: {b_dir}", id="lbl-confirm-dir")
            yield Label(
                f"Operations / Files to restore: {step_cnt}", id="lbl-confirm-ops"
            )
            yield Label(f"Session Timestamp: {ts_str}", id="lbl-confirm-ts")

            with Vertical(classes="warning-box"):
                yield Label(
                    "⚠️ Warning: Rolling back will restore files to their original locations prior to this session. Any subsequent manual edits or file relocations in target folders may be unwound or overwritten."
                )

            if self.session_info.get("step_count"):
                # Step-ledger sessions always skip files that are no longer
                # at their sorted location, so a force option would do nothing.
                yield Label(
                    "Files that are no longer at their sorted location are skipped.",
                    id="lbl-confirm-missing",
                )
            else:
                yield Checkbox(
                    "Roll back even if some sorted files are missing (they are skipped)",
                    value=False,
                    id="chk-ignore-missing",
                )

            with Horizontal(classes="button-row"):
                btn_cancel = Button(
                    "Cancel", id="btn-cancel-rollback", variant="default"
                )
                yield btn_cancel
                btn_confirm = Button(
                    "Confirm Rollback", id="btn-confirm-rollback", variant="error"
                )
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus confirm button and announce modal screen."""
        self.query_one("#btn-confirm-rollback", Button).focus()
        self.announce("Opened rollback confirmation modal dialog.")

    @on(Button.Pressed, "#btn-confirm-rollback")
    def action_confirm(self) -> None:
        """Confirm rollback action and dismiss modal with parameters."""
        checkboxes = self.query("#chk-ignore-missing").results(Checkbox)
        chk = next(iter(checkboxes), None)
        self.dismiss(
            {
                "confirmed": True,
                "session_id": self.session_info.get("session_id"),
                "ignore_missing": chk.value if chk else False,
            }
        )

    @on(Button.Pressed, "#btn-cancel-rollback")
    def action_cancel(self) -> None:
        """Cancel rollback confirmation dialog."""
        self.dismiss(None)


class RollbackErrorModal(A11yMixin, ModalScreen[None]):
    """Modal dialog displaying error messages encountered during session rollback."""

    BINDINGS = [
        Binding("escape", "close", "Close error dialog", show=True),
    ]

    CSS = """
    RollbackErrorModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .error-box {
        padding: 1 2;
        background: $panel;
        border: thick $error;
        width: 85%;
        max-width: 75;
        height: auto;
    }
    .modal-title {
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    .error-text {
        margin-top: 1;
        margin-bottom: 1;
        color: $text;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    """

    def __init__(self, title: str = "Rollback Failed", error_message: str = ""):
        super().__init__()
        self.error_title = title
        self.error_message = error_message

    def compose(self) -> ComposeResult:
        """Compose error modal widgets."""
        with Vertical(classes="error-box"):
            yield Heading(
                self.error_title, level=1, classes="modal-title", id="error-title"
            )
            yield Label(
                self.error_message, classes="error-text", id="lbl-error-message"
            )
            with Horizontal(classes="button-row"):
                yield Button("Close", id="btn-close-error", variant="primary")

    def on_mount(self) -> None:
        """Focus close button and announce error message."""
        self.query_one("#btn-close-error", Button).focus()
        self.announce(f"Error dialog: {self.error_title}")

    @on(Button.Pressed, "#btn-close-error")
    def action_close(self) -> None:
        """Dismiss error modal dialog."""
        self.dismiss(None)


class HistoryModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Dict[str, Any]]]
):
    """Modal dialog for viewing session history records, exporting audit logs, and rolling back sessions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    HistoryModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 90;
        min-width: 40;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .narrow .modal-box {
        padding: 0 1;
        width: 95%;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .details-box {
        margin-top: 1;
        margin-bottom: 1;
        padding: 1;
        background: $boost;
        border: solid $accent;
        color: $text;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        sessions: Optional[List[Dict[str, Any]]] = None,
        base_dir: str = "",
    ):
        super().__init__()
        if sessions is None:
            from app.core.session_catalog import list_sessions

            sessions = list_sessions()
        self.sessions = sessions or []
        self.base_dir = base_dir
        self._rollback_in_progress = False

    def compose(self) -> ComposeResult:
        """Compose history view, export, and rollback modal widgets."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Session History & Rollback Controls [Ctrl+Y]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(
                "Select a session row to export audit log or perform session rollback:",
                classes="modal-subtitle",
            )

            options = [("All Sessions (Aggregate Audit Log)", "all")]
            for s in self.sessions:
                sid = s.get("session_id", "unknown")
                b_dir = s.get("base_dir") or "N/A"
                status = s.get("status") or "completed"
                step_cnt = s.get("step_count") or s.get("file_count") or 0
                options.append(
                    (
                        f"{sid[:8]}... | {status.upper()} | {step_cnt} file(s) | {b_dir}",
                        sid,
                    )
                )

            if len(options) == 1 and not self.sessions:
                options = [("No historical sessions found", "none")]

            select_sess = Select(
                options,
                value=options[0][1] if options else "none",
                id="select-history-session",
                allow_blank=False,
            )
            select_sess.tooltip = (
                "Select historical session row for audit log export or session rollback"
            )
            yield select_sess

            yield Label("Session Details:", classes="modal-subtitle")
            yield Label(
                "Select a session above to view details.",
                id="lbl-session-details",
                classes="details-box",
            )

            yield Label("Output File Path (Audit Log Export):")
            inp_path = Input(
                value="./audit_log.csv",
                placeholder="Enter export path (e.g. ./audit_log.csv)...",
                id="input-history-path",
            )
            inp_path.tooltip = (
                "Enter target output path for exported session audit log file"
            )
            yield inp_path

            yield Label("Export Format:")
            fmt_select = Select(
                [("CSV Format (*.csv)", "csv"), ("JSON Format (*.json)", "json")],
                value="csv",
                id="select-history-format",
                allow_blank=False,
            )
            fmt_select.tooltip = "Select audit log output format"
            yield fmt_select

            yield Label("", id="lbl-rollback-status")

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel history dialog"
                yield btn_cancel

                btn_rollback = Button(
                    "Rollback Session",
                    id="btn-rollback-session",
                    variant="warning",
                    disabled=True,
                )
                btn_rollback.tooltip = "Revert file operations for selected session"
                yield btn_rollback

                btn_export = Button(
                    "Export Audit Log", id="btn-export", variant="primary"
                )
                btn_export.tooltip = (
                    "Export audit log file for selected session history row"
                )
                yield btn_export

    def on_mount(self) -> None:
        """Focus session select widget on mount and emit accessibility announcement."""
        self._update_layout(self.size.width)
        select_widget = self.query_one("#select-history-session", Select)
        select_widget.focus()
        self._update_selected_session_view(select_widget.value)
        self.announce("Opened session history export and rollback modal dialog.")

    @on(Select.Changed, "#select-history-session")
    def on_session_changed(self, event: Select.Changed) -> None:
        """Update session details label and button state when session selection changes."""
        self._update_selected_session_view(event.value)

    def _update_selected_session_view(self, session_val: Any) -> None:
        """Update session details label and enable/disable rollback button based on selected session."""
        try:
            lbl_details = self.query_one("#lbl-session-details", Label)
            btn_rollback = self.query_one("#btn-rollback-session", Button)
        except Exception:
            return

        if not session_val or session_val in ("none", "all"):
            lbl_details.update(
                "Aggregate log across all historical sessions selected."
                if session_val == "all"
                else "No session selected."
            )
            btn_rollback.disabled = True
            btn_rollback.tooltip = "Select an individual session to enable rollback"
            return

        target = None
        for s in self.sessions:
            if s.get("session_id") == session_val:
                target = s
                break

        if not target:
            lbl_details.update("Session information unavailable.")
            btn_rollback.disabled = True
            return

        sid = target.get("session_id", "N/A")
        status = target.get("status") or "completed"
        b_dir = target.get("base_dir") or "N/A"
        step_cnt = target.get("step_count") or target.get("file_count") or 0
        ts = target.get("timestamp")
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "N/A"

        details_str = (
            f"Session ID: {sid}\n"
            f"Status: {status.upper()} | Operations/Files: {step_cnt}\n"
            f"Timestamp: {ts_str}\n"
            f"Base Directory: {b_dir}"
        )
        lbl_details.update(details_str)

        if status.lower() == "rolled_back":
            btn_rollback.disabled = True
            btn_rollback.tooltip = "Session has already been rolled back"
        else:
            btn_rollback.disabled = False
            btn_rollback.tooltip = "Revert file operations for selected session"

    @on(Button.Pressed, "#btn-export")
    def action_export_audit(self) -> None:
        """Confirm session audit log export and dismiss modal screen."""
        session_val = self.query_one("#select-history-session", Select).value
        if not session_val or session_val == "none":
            self.announce("No session selected for audit log export.")
            return

        path_val = self.query_one("#input-history-path", Input).value.strip()
        if not path_val:
            self.announce("Output file path cannot be empty.")
            return

        format_val = self.query_one("#select-history-format", Select).value or "csv"

        self.dismiss(
            {
                "session_id": str(session_val),
                "output_path": path_val,
                "format": str(format_val),
            }
        )

    @on(Button.Pressed, "#btn-rollback-session")
    def action_rollback_session(self) -> None:
        """Prompt confirmation and initiate asynchronous session rollback."""
        session_val = self.query_one("#select-history-session", Select).value
        if not session_val or session_val in ("none", "all"):
            self.announce("Please select a specific session to rollback.")
            return

        target_sess = None
        for s in self.sessions:
            if s.get("session_id") == session_val:
                target_sess = s
                break

        if not target_sess or str(target_sess.get("status")).lower() == "rolled_back":
            self.announce("Selected session cannot be rolled back.")
            return

        def handle_confirm_result(result: Optional[Dict[str, Any]]) -> None:
            if not result or not result.get("confirmed"):
                return

            sid = result["session_id"]
            ignore_missing = result.get("ignore_missing", False)

            self.query_one("#btn-rollback-session", Button).disabled = True
            self.query_one("#btn-export", Button).disabled = True
            self.query_one("#btn-cancel", Button).disabled = True
            self._rollback_in_progress = True

            lbl_status = self.query_one("#lbl-rollback-status", Label)
            lbl_status.update(f"⏳ Rolling back session {sid[:8]}... Please wait.")
            self.announce(f"Executing rollback for session {sid[:8]}...")

            self.run_rollback_worker(sid, ignore_missing)

        self.app.push_screen(
            RollbackConfirmModal(session_info=target_sess),
            handle_confirm_result,
        )

    @work(thread=True)
    def run_rollback_worker(self, session_id: str, ignore_missing: bool) -> None:
        """Execute session rollback asynchronously in background worker thread."""
        try:
            from app.core.history import open_session_history

            with open_session_history() as history_mgr:
                restored_count = history_mgr.rollback(
                    session_id, ignore_missing=ignore_missing
                )

            app_obj = getattr(self, "app", None)
            if app_obj and hasattr(app_obj, "call_from_thread"):
                app_obj.call_from_thread(
                    self._on_rollback_success, session_id, restored_count
                )
            else:
                self._on_rollback_success(session_id, restored_count)
        except Exception as err:
            app_obj = getattr(self, "app", None)
            if app_obj and hasattr(app_obj, "call_from_thread"):
                app_obj.call_from_thread(self._on_rollback_failure, session_id, err)
            else:
                self._on_rollback_failure(session_id, err)

    def _on_rollback_success(self, session_id: str, restored_count: int) -> None:
        """Handle successful session rollback completion on main thread."""
        self._rollback_in_progress = False
        from app.core.session_catalog import list_sessions

        self.sessions = list_sessions()
        self._refresh_session_options(selected_sid=session_id)

        lbl_status = self.query_one("#lbl-rollback-status", Label)
        lbl_status.update(
            f"✅ Session successfully rolled back. ({restored_count} operations unwound)"
        )

        msg = f"Session {session_id[:8]}... successfully rolled back ({restored_count} file(s) restored)."
        self.announce(msg)
        try:
            from app.ui.notifications import notify

            notify(msg, type="info")
        except Exception:
            pass

        self.query_one("#btn-cancel", Button).disabled = False
        self.query_one("#btn-export", Button).disabled = False

    def _on_rollback_failure(self, session_id: str, err: Exception) -> None:
        """Handle session rollback failure on main thread."""
        self._rollback_in_progress = False
        lbl_status = self.query_one("#lbl-rollback-status", Label)
        lbl_status.update(f"❌ Rollback failed: {err}")

        msg = f"Rollback failed for session {session_id[:8]}...: {err}"
        self.announce(msg)
        try:
            from app.ui.notifications import notify

            notify(msg, type="error")
        except Exception:
            pass

        self.query_one("#btn-cancel", Button).disabled = False
        self.query_one("#btn-export", Button).disabled = False
        self.query_one("#btn-rollback-session", Button).disabled = False

        self.app.push_screen(
            RollbackErrorModal(title="Rollback Failed", error_message=str(err))
        )

    def _refresh_session_options(self, selected_sid: Optional[str] = None) -> None:
        """Re-populate session select dropdown widget options after state update."""
        select_sess = self.query_one("#select-history-session", Select)
        options = [("All Sessions (Aggregate Audit Log)", "all")]
        for s in self.sessions:
            sid = s.get("session_id", "unknown")
            b_dir = s.get("base_dir") or "N/A"
            status = s.get("status") or "completed"
            step_cnt = s.get("step_count") or s.get("file_count") or 0
            options.append(
                (f"{sid[:8]}... | {status.upper()} | {step_cnt} file(s) | {b_dir}", sid)
            )

        if len(options) == 1 and not self.sessions:
            options = [("No historical sessions found", "none")]

        select_sess.set_options(options)
        if selected_sid:
            select_sess.value = selected_sid
        self._update_selected_session_view(select_sess.value)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel history export modal, unless a rollback is still running."""
        if self._rollback_in_progress:
            self.announce("Rollback in progress. Please wait for it to finish.")
            return
        self.dismiss(None)


class AutoSorterTUI(ResponsiveLayoutMixin, A11yMixin, App):
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
                yield Heading(
                    "Side-by-Side Path Comparison", level=2, classes="meta-header"
                )
                path_comp = Static(
                    "Select a file or folder in the tree to compare source and target paths.",
                    id="path-comparison",
                )
                path_comp.tooltip = "Side-by-side split column view comparing source and destination target paths"
                yield path_comp
                yield Heading("Node Metadata Inspector", level=2, classes="meta-header")
                meta_widget = Static(
                    "Select a node in the tree to inspect details.", id="meta-details"
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
        import shutil

        from app.core.pipeline import SortPipeline
        from app.core.session import AppSession

        base_dir = session_info.get("base_dir") or self.base_dir
        session_id = session_info.get("session_id")
        rollback_success = False
        if base_dir and session_id:
            try:
                temp_session = AppSession(
                    self.settings, base_dir, session_id=session_id
                )
                try:
                    result = SortPipeline(temp_session).undo(
                        session_id, ignore_missing=True
                    )
                finally:
                    temp_session.close()
                rollback_success = result.ok
                if not result.ok:
                    logger.error(
                        f"Session rollback left files in place: {result.failed}"
                    )
            except Exception as e:
                logger.error(f"Error during session rollback: {e}")

        session_dir = session_info.get("session_dir")
        if rollback_success:
            if session_dir and os.path.exists(session_dir):
                shutil.rmtree(session_dir, ignore_errors=True)
            self.announce(f"Rolled back session '{session_id}'.")
        else:
            self.announce(
                f"Session recovery failed for '{session_id}': write/permission error. Session files and safety storage preserved."
            )

        if self.base_dir:
            self.action_scan_directory()

    def _do_recovery_clean(self, session_info: Dict[str, Any]) -> None:
        """Leave an abandoned session as it is and stop offering to recover it."""
        from app.core.session_catalog import mark_session_abandoned

        journal_path = session_info.get("journal_path")
        if journal_path:
            try:
                os.remove(journal_path)
            except OSError:
                logger.warning("Could not remove rollback journal %s", journal_path)
        elif session_info.get("session_id"):
            mark_session_abandoned(session_info["session_id"])
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

    def action_scan_directory(self) -> None:
        """Trigger directory scanning background worker."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        if not getattr(self, "source_dirs", None) and self.base_dir:
            self.source_dirs = [self.base_dir]
        if not getattr(self, "source_dirs", None) or not any(
            os.path.exists(d) for d in self.source_dirs
        ):
            self.action_select_dir()
            return
        if self._announce_if_busy("scan"):
            return
        self._start_scan()

    def _start_scan(self) -> None:
        """Announce and launch the scan worker without the busy guard."""
        self.announce(f"Scanning directories: {', '.join(self.source_dirs)} ...")
        self.run_scan_worker()

    def _running_pipeline_worker(self) -> Optional[str]:
        """Return the name of a running scan or execute worker, if any."""
        for worker in self.workers:
            if worker.is_running and worker.name in (
                "run_scan_worker",
                "run_execute_worker",
            ):
                return worker.name
        return None

    def _announce_if_busy(self, action: str) -> bool:
        """Refuse to start ``action`` while a scan or move is still running.

        Both workers share the default exclusive group, so starting one cancels
        the other and replaces the AppSession that a running move depends on.
        """
        running = self._running_pipeline_worker()
        if running is None:
            return False
        if running == "run_execute_worker":
            msg = f"Files are still being moved. Wait for it to finish before you {action}."
        else:
            msg = f"A scan is still running. Wait for it to finish before you {action}."
        self.announce(msg, priority="assertive")
        return True

    @work(exclusive=True, thread=True)
    def run_scan_worker(self) -> None:
        """Execute scan and plan generation in worker thread."""
        try:
            from app.core.session import AppSession

            if self.app_session:
                try:
                    self.app_session.close()
                except Exception:
                    pass

            self.app_session = AppSession(
                self.settings,
                self.base_dir,
                source_dirs=getattr(self, "source_dirs", None),
            )

            from app.core.scanner import get_files_from_sources

            s_dirs = getattr(self, "source_dirs", None) or (
                [self.base_dir] if self.base_dir else []
            )
            files = get_files_from_sources(s_dirs)

            from app.core.metadata import MetadataPass

            def scan_progress_cb(
                update_or_prog: Any, stage: Optional[str] = None, **kwargs: Any
            ) -> None:
                self.call_from_thread(
                    self._handle_progress_update, update_or_prog, stage, **kwargs
                )

            bypassed = MetadataPass.run(
                self.base_dir,
                files,
                self.settings,
                self.app_session.db,
                progress_callback=scan_progress_cb,
                cancel_check=lambda: False,
                source_dirs=s_dirs,
            )

            # Files no metadata rule matched still need their text extracted and
            # fitted, otherwise clustering sees no documents and the plan is empty.
            bypassed_set = {str(f).replace("\\", "/") for f in (bypassed or [])}
            items_to_extract = [
                f for f in files if str(f).replace("\\", "/") not in bypassed_set
            ]
            if items_to_extract:
                from app.core.extractor import build_corpus_generator

                total = len(items_to_extract)
                done = 0
                for chunk in build_corpus_generator(
                    base_dir=self.base_dir,
                    items_to_sort=items_to_extract,
                    progress_callback=lambda *args, **kwargs: None,
                    max_workers=getattr(self.settings, "MAX_WORKERS", 2),
                    db=self.app_session.db,
                    settings=self.settings,
                ):
                    self.app_session.partial_fit(chunk)
                    done += len(chunk)
                    scan_progress_cb(done / total, f"Extracting text ({done}/{total})")

            # Load locks & ratings
            try:
                docs = self.app_session.db.get_all_documents(
                    self.base_dir, source_dirs=s_dirs
                )
                for d in docs:
                    if len(d) > 3 and d[3]:
                        self.locked_files[d[0]] = d[3]
            except Exception:
                pass

            try:
                self._ratings_cache = self.app_session.db.get_all_document_ratings(
                    self.base_dir
                )
            except Exception:
                pass

            self.plan = self.app_session.generate_sorting_plan()
            self.call_from_thread(self.rebuild_tree)
            model_status = getattr(self.plan, "model_status", "HEALTHY")
            invalid_locks = getattr(self.plan, "invalid_locks", {}) or {}
            if not invalid_locks and isinstance(self.plan, dict):
                invalid_locks = self.plan.get("invalid_locks", {}) or {}
            num_invalid = len(invalid_locks)

            if model_status == "DEGRADED_FALLBACK" or (
                isinstance(self.plan, dict)
                and self.plan.get("model_status") == "DEGRADED_FALLBACK"
            ):
                reason = (
                    getattr(self.plan, "degradation_reason", None)
                    or self.plan.get("degradation_reason")
                    or "Model unavailable"
                )
                rec_action = (
                    getattr(self.plan, "recovery_action", None)
                    or self.plan.get("recovery_action")
                    or "Re-download model bundle"
                )
                msg = f"Scan complete (DEGRADED FALLBACK MODE: {reason}. Suggested action: {rec_action}). Analyzed {len(files)} files."
                self.call_from_thread(
                    self.notify,
                    f"Sorting plan generated in degraded fallback mode: {reason}",
                    title=f"Suggested recovery action: {rec_action}",
                    severity="warning",
                )
            else:
                msg = f"Scan complete. Analyzed {len(files)} files into proposed plan."

            if num_invalid > 0:
                lock_str = (
                    "1 bad lock path was skipped"
                    if num_invalid == 1
                    else f"{num_invalid} bad lock paths were skipped"
                )
                msg += f" ({lock_str})."
            self.call_from_thread(self.announce, msg)
        except Exception as e:
            logger.error(f"Error in run_scan_worker: {e}")
            self.call_from_thread(self.announce, f"Scan error: {e}")
        finally:
            self.call_from_thread(self.reset_progress_bar)

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
                )
                cnt = export_res.get("count", 0)
                msg = f"Audit log exported ({fmt.upper()}) to '{output_path}' ({cnt} records)."
                self.announce(msg)
                from app.ui.notifications import notify

                notify(msg, type="info")
            except Exception as e:
                logger.error(f"Error exporting audit log from TUI: {e}")
                self.announce(f"Export error: {e}")

        self.push_screen(
            HistoryModal(sessions=sessions, base_dir=self.base_dir),
            handle_history_modal_result,
        )

    @on(Button.Pressed, "#btn-execute")
    def on_btn_execute_pressed(self) -> None:
        """Handle Execute Plan button press."""
        self.action_execute_sort()

    def action_execute_sort(self) -> None:
        """Trigger sorting plan execution worker with pre-move duplicate cluster review."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        if not self.plan or not self.app_session:
            self.announce("No plan available to execute. Run [Ctrl+S] Scan first.")
            return
        if self._announce_if_busy("execute the plan"):
            return

        exec_plan = self._get_executable_plan()
        if not self._has_file_nodes(exec_plan):
            self.announce("No checked files in sorting plan to execute.")
            return

        if getattr(self, "has_critical_errors", False):
            self.announce(
                "Cannot execute plan: Critical validation errors exist (see Alert Banner)."
            )
            return

        base_dir = self.app_session.base_dir if self.app_session else "."
        from app.core.verifier import VerificationEngine

        duplicate_groups = VerificationEngine.get_duplicate_groups(base_dir, exec_plan)
        if duplicate_groups:

            def handle_duplicate_modal_result(
                selections: Optional[Dict[str, str]],
            ) -> None:
                if selections is None:
                    self.announce("Move execution cancelled during duplicate review.")
                    return

                VerificationEngine.apply_deduplication_selections(
                    base_dir, self.plan, selections
                )
                self.announce(
                    "Executing file moves according to plan with deduplication applied..."
                )
                self.run_execute_worker()

            self.push_screen(
                DuplicateReviewModal(duplicate_groups=duplicate_groups),
                handle_duplicate_modal_result,
            )
        else:
            self.announce("Executing file moves according to plan...")
            self.run_execute_worker()

    @work(group="validation", exclusive=True, thread=True)
    def run_validation_worker(self) -> None:
        """Asynchronously verify plan integrity in background worker thread."""
        try:
            from app.core.verifier import VerificationEngine

            exec_plan = self._get_executable_plan()
            if not self.plan or not self._has_file_nodes(exec_plan):
                self.call_from_thread(self._update_validation_state, {})
                return

            base_dir = (
                self.app_session.base_dir
                if self.app_session
                else (self.base_dir or ".")
            )
            result = VerificationEngine.verify_plan_integrity(base_dir, exec_plan)
            self.call_from_thread(self._update_validation_state, result)
        except Exception as e:
            logger.error(f"Error running validation worker: {e}")

    def _update_validation_state(self, result: Dict[str, Any]) -> None:
        """Update UI controls, alert banner, and tree highlights based on validation result."""
        self.verification_result = result or {}

        collisions = result.get("collisions", [])
        circular = result.get("circular_renames", [])
        invalid = result.get("invalid_renames", [])
        long_paths = result.get("long_paths", [])

        broken_links = result.get("broken_links", [])
        unconfirmed = result.get("unconfirmed_renames", [])

        has_critical = bool(collisions or circular or invalid or long_paths)
        has_warnings = bool(broken_links or unconfirmed)

        self.has_critical_errors = has_critical

        # Build path sets for tree node highlighting
        crit_paths = set()
        for c in collisions:
            if c.get("path"):
                crit_paths.add(os.path.normpath(c["path"]))
            if c.get("source"):
                crit_paths.add(os.path.normpath(c["source"]))
            for s in c.get("sources", []):
                crit_paths.add(os.path.normpath(s))
        for r in circular:
            if r.get("path"):
                crit_paths.add(os.path.normpath(r["path"]))
            for p in r.get("cycle", []):
                crit_paths.add(os.path.normpath(p))
        for inv in invalid:
            if inv.get("path"):
                crit_paths.add(os.path.normpath(inv["path"]))
            if inv.get("source"):
                crit_paths.add(os.path.normpath(inv["source"]))
        for lp in long_paths:
            if lp.get("path"):
                crit_paths.add(os.path.normpath(lp["path"]))
            if lp.get("source"):
                crit_paths.add(os.path.normpath(lp["source"]))

        warn_paths = set()
        for bl in broken_links:
            if bl.get("path"):
                warn_paths.add(os.path.normpath(bl["path"]))
        for unc in unconfirmed:
            if unc.get("path"):
                warn_paths.add(os.path.normpath(unc["path"]))
            if unc.get("source"):
                warn_paths.add(os.path.normpath(unc["source"]))

        self.critical_error_paths = crit_paths
        self.warning_paths = warn_paths

        # Update Execute Button
        try:
            btn_exec = self.query_one("#btn-execute", Button)
            exec_plan = self._get_executable_plan()
            has_files = self._has_file_nodes(exec_plan)

            if has_critical or not has_files:
                btn_exec.disabled = True
                if has_critical:
                    btn_exec.tooltip = "Execution disabled: Plan contains blocking critical validation errors"
                elif not self.plan:
                    btn_exec.tooltip = EXECUTE_NEEDS_SCAN_TOOLTIP
                else:
                    btn_exec.tooltip = (
                        "Execution disabled: No files selected to execute"
                    )
            else:
                btn_exec.disabled = False
                btn_exec.tooltip = "Execute file sorting plan [Ctrl+E]"
        except Exception:
            pass

        # Update Alert Banner
        try:
            alert_banner = self.query_one("#alert-banner", Static)
            if has_critical:
                alert_banner.display = True
                alert_banner.remove_class("alert-warning")
                alert_banner.add_class("alert-critical")

                msgs = []
                if collisions:
                    msgs.append("⛔ CRITICAL FILE COLLISION(S) DETECTED:")
                    for c in collisions:
                        dst_p = c.get("path", "")
                        if c.get("sources"):
                            for s_p in c["sources"]:
                                msgs.append(
                                    f"  • Source: {s_p} -> Destination: {dst_p}"
                                )
                        elif c.get("source"):
                            msgs.append(
                                f"  • Source: {c['source']} -> Destination: {dst_p}"
                            )
                        else:
                            msgs.append(
                                f"  • Destination: {dst_p} ({c.get('message', 'Collision')})"
                            )
                if circular:
                    msgs.append("⛔ CIRCULAR RENAMING DEPENDENCY DETECTED:")
                    for r in circular:
                        msgs.append(f"  • {r.get('message')}")
                if invalid:
                    msgs.append("⛔ INVALID FILENAME / STRUCTURE:")
                    for inv in invalid:
                        msgs.append(f"  • {inv.get('message')}")
                if long_paths:
                    msgs.append("⛔ EXCEEDS PATH LENGTH LIMIT:")
                    for lp in long_paths:
                        msgs.append(f"  • {lp.get('message')}")

                alert_str = "\n".join(msgs)
                alert_banner.update(alert_str)
                self.announce(f"Alert Banner: {alert_str}")
            elif has_warnings:
                alert_banner.display = True
                alert_banner.remove_class("alert-critical")
                alert_banner.add_class("alert-warning")

                msgs = ["⚠️ NON-FATAL WARNING(S) REPORTED (Execution unblocked):"]
                for bl in broken_links:
                    msgs.append(f"  • {bl.get('message')}")
                for unc in unconfirmed:
                    msgs.append(f"  • {unc.get('message')}")

                alert_str = "\n".join(msgs)
                alert_banner.update(alert_str)
            else:
                alert_banner.display = False
                alert_banner.update("")
        except Exception as e:
            logger.debug(f"Error updating alert banner: {e}")

        # Update node styling in tree view
        self._apply_tree_node_styles()

    def _apply_tree_node_styles(self) -> None:
        """Apply red/yellow styling to existing tree nodes based on active error/warning paths."""
        try:
            tree = self.query_one("#plan-tree", Tree)

            def _stylize_node(node: TreeNode) -> None:
                data = getattr(node, "data", None)
                if not data or not isinstance(data, dict):
                    for child in node.children:
                        _stylize_node(child)
                    return

                filepath = data.get("filepath", "")
                key = data.get("key", "")
                norm_fp = os.path.normpath(filepath) if filepath else ""
                norm_key = os.path.normpath(key) if key else ""

                is_crit = False
                is_warn = False

                if norm_fp and norm_fp in self.critical_error_paths:
                    is_crit = True
                elif norm_key and norm_key in self.critical_error_paths:
                    is_crit = True
                elif any(
                    p
                    and (
                        p == norm_fp
                        or p == norm_key
                        or p.endswith("/" + norm_key)
                        or p.endswith("\\" + norm_key)
                    )
                    for p in self.critical_error_paths
                ):
                    is_crit = True

                if not is_crit:
                    if norm_fp and norm_fp in self.warning_paths:
                        is_warn = True
                    elif norm_key and norm_key in self.warning_paths:
                        is_warn = True
                    elif any(
                        p
                        and (
                            p == norm_fp
                            or p == norm_key
                            or p.endswith("/" + norm_key)
                            or p.endswith("\\" + norm_key)
                        )
                        for p in self.warning_paths
                    ):
                        is_warn = True

                if isinstance(node.label, Text):
                    plain_txt = node.label.plain
                    new_txt = Text(plain_txt)
                    if is_crit:
                        new_txt.stylize("bold red")
                    elif is_warn:
                        new_txt.stylize("bold yellow")
                    node.label = new_txt

                for child in node.children:
                    _stylize_node(child)

            _stylize_node(tree.root)
        except Exception as e:
            logger.debug(f"Error applying tree node styles: {e}")

    @work(exclusive=True, thread=True)
    def run_execute_worker(self) -> None:
        """Execute moves in worker thread."""
        try:

            def move_progress_cb(
                update_or_prog: Any, stage: Optional[str] = None, **kwargs: Any
            ) -> None:
                self.call_from_thread(
                    self._handle_progress_update, update_or_prog, stage, **kwargs
                )

            exec_plan = self._get_executable_plan()
            summary = self.app_session.execute_moves(
                exec_plan,
                progress_callback=move_progress_cb,
            )
            msg = f"Execution completed successfully! Summary: {summary}"
            self.call_from_thread(self.announce, msg)

            audit_report = (
                summary.get("audit_report") if isinstance(summary, dict) else None
            )
            if not audit_report and exec_plan:
                from app.core.audit_reporter import generate_audit_report
                from app.core.verifier import VerificationEngine

                base_dir = self.app_session.base_dir if self.app_session else "."
                moves = VerificationEngine.get_moves(base_dir, exec_plan)
                records = [
                    {"source_path": src, "destination_path": dst}
                    for _, src, dst in moves
                ]
                audit_report = generate_audit_report(base_dir, records)

            if audit_report:
                self.call_from_thread(self.action_open_audit_report, audit_report)
            # Rescan last: the scan worker replaces the AppSession this worker uses.
            self.call_from_thread(self._start_scan)
        except Exception as e:
            logger.error(f"Error executing moves: {e}")
            self.call_from_thread(self.announce, f"Execution error: {e}")
        finally:
            self.call_from_thread(self.reset_progress_bar)

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

    # --- Tree Management & Event Handlers ---

    def _has_file_nodes(self, plan_dict: Dict[str, Any]) -> bool:
        """Check if plan structure contains any document file nodes."""
        if not plan_dict or not isinstance(plan_dict, dict):
            return False
        for k, v in plan_dict.items():
            if isinstance(v, dict):
                if v.get("__type__") == "file":
                    return True
                if self._has_file_nodes(v):
                    return True
            elif isinstance(v, list) and len(v) > 0:
                return True
            elif isinstance(v, str) and k != "action":
                return True
        return False

    def _get_current_search_query(self) -> str:
        """Return lowercased search query string if search input exists and contains text."""
        try:
            search_input = self.query_one("#tree-search-input", Input)
            return search_input.value.strip().lower()
        except Exception:
            return ""

    def _build_matching_node_ids(
        self, plan_dict: Dict[str, Any], query: str, current_folder: str = ""
    ) -> Set[int]:
        """Perform a single O(N) bottom-up pass returning a set of dictionary object IDs matching the query."""
        matching_ids: Set[int] = set()
        if not query or not isinstance(plan_dict, dict):
            return matching_ids

        def _walk(d: Dict[str, Any], folder: str) -> bool:
            has_match = False
            for k, v in d.items():
                if isinstance(v, dict):
                    if v.get("__type__") == "file":
                        filepath = str(
                            v.get(
                                "filepath",
                                (Path(self.base_dir) / folder / k).as_posix()
                                if self.base_dir
                                else (Path(folder) / k).as_posix(),
                            )
                        )
                        target_filename = str(v.get("target_filename", ""))
                        if (
                            query in k.lower()
                            or query in filepath.lower()
                            or query in target_filename.lower()
                        ):
                            matching_ids.add(id(v))
                            has_match = True
                    else:
                        sub_folder = (Path(folder) / k).as_posix() if folder else k
                        folder_direct_match = (
                            query in k.lower() or query in sub_folder.lower()
                        )
                        child_match = _walk(v, sub_folder)
                        if folder_direct_match or child_match:
                            matching_ids.add(id(v))
                            has_match = True
            return has_match

        _walk(plan_dict, current_folder)
        return matching_ids

    def render_tree(self, search_query: Optional[str] = None) -> None:
        """Render Textual Tree widget from in-memory plan structure without running validation worker."""
        try:
            tree = self.query_one("#plan-tree", Tree)
            tree.reset("Proposed Organization Plan")
            if search_query is None:
                search_query = self._get_current_search_query()

            query = search_query.strip().lower() if search_query else ""

            if not self._has_file_nodes(self.plan):
                tree.root.add_leaf(
                    "💡 Empty Workspace: No documents found in target directory",
                    data={"is_empty_state": True, "action": "info"},
                )
                tree.root.add_leaf(
                    "▶ Press [G] to Load Sample Dataset",
                    data={
                        "is_empty_state": True,
                        "action": "generate_sample_corpus",
                        "shortcut": "G",
                    },
                )
                tree.root.add_leaf(
                    "▶ Press [Ctrl+B] to Select Different Directory",
                    data={
                        "is_empty_state": True,
                        "action": "select_dir",
                        "shortcut": "Ctrl+B",
                    },
                )
            else:
                matching_ids = (
                    self._build_matching_node_ids(self.plan, query) if query else None
                )
                nodes_added = self._build_tree_nodes(
                    self.plan,
                    tree.root,
                    current_folder="",
                    query=query,
                    matching_ids=matching_ids,
                )
                if query and nodes_added == 0:
                    tree.root.add_leaf(
                        f"🔍 No matching tree nodes found for '{search_query}'",
                        data={"is_empty_state": True, "action": "info"},
                    )
            tree.root.expand_all()
            if tree.cursor_node and tree.cursor_node.data:
                self._update_inspector(tree.cursor_node, announce=False)
            elif getattr(self, "active_tree_node", None) and self.active_tree_node.data:
                self._update_inspector(self.active_tree_node, announce=False)
            else:
                self._update_inspector(None, announce=False)
        except Exception as e:
            logger.error(f"Error rendering tree: {e}")

    def rebuild_tree(
        self, search_query: Optional[str] = None, run_validation: bool = True
    ) -> None:
        """Rebuild Textual Tree widget from in-memory plan structure and optionally validate plan integrity."""
        self.render_tree(search_query=search_query)
        if run_validation:
            self.run_validation_worker()

    def _collect_file_nodes_from_dict(self, d: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Collect all file info dicts nested within a plan directory subtree."""
        files = []
        if not isinstance(d, dict):
            return files
        for val in d.values():
            if isinstance(val, dict):
                if val.get("__type__") == "file":
                    files.append(val)
                else:
                    files.extend(self._collect_file_nodes_from_dict(val))
        return files

    def _collect_visible_file_nodes_from_dict(
        self,
        node_dict: Dict[str, Any],
        current_folder: str = "",
        query: str = "",
        parent_folder_matches: bool = False,
        matching_ids: Optional[Set[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Collect file info dicts for nodes visible under the active search query filter."""
        files = []
        if not isinstance(node_dict, dict):
            return files

        if not query:
            return self._collect_file_nodes_from_dict(node_dict)

        if matching_ids is None and query:
            matching_ids = self._build_matching_node_ids(
                node_dict, query, current_folder=current_folder
            )

        for k, v in sorted(node_dict.items()):
            if isinstance(v, dict) and v.get("__type__") == "file":
                if parent_folder_matches or (
                    matching_ids is not None and id(v) in matching_ids
                ):
                    files.append(v)
            elif isinstance(v, dict):
                sub_folder = (
                    (Path(current_folder) / k).as_posix() if current_folder else k
                )
                folder_matches = query in k.lower() or query in sub_folder.lower()
                if (
                    parent_folder_matches
                    or folder_matches
                    or (matching_ids is not None and id(v) in matching_ids)
                ):
                    files.extend(
                        self._collect_visible_file_nodes_from_dict(
                            v,
                            current_folder=sub_folder,
                            query=query,
                            parent_folder_matches=parent_folder_matches
                            or folder_matches,
                            matching_ids=matching_ids,
                        )
                    )
        return files

    def _node_matches_query(
        self, key: str, val: Dict[str, Any], current_folder: str, query: str
    ) -> bool:
        """Check if a node (file or directory) or any of its descendants match the search query."""
        if not query:
            return True
        matching_ids = self._build_matching_node_ids(
            {key: val}, query, current_folder=current_folder
        )
        return id(val) in matching_ids

    def _build_tree_nodes(
        self,
        node_dict: Dict[str, Any],
        parent_item: TreeNode,
        current_folder: str,
        query: str = "",
        parent_folder_matches: bool = False,
        matching_ids: Optional[Set[int]] = None,
    ) -> int:
        """Recursively build tree nodes, filtering by search query if specified. Returns count of nodes added."""
        added_count = 0
        if query and matching_ids is None:
            matching_ids = self._build_matching_node_ids(
                node_dict, query, current_folder=current_folder
            )

        for k, v in sorted(
            node_dict.items(),
            key=lambda x: (
                1 if isinstance(x[1], dict) and x[1].get("__type__") == "file" else 0,
                x[0],
            ),
        ):
            if isinstance(v, dict) and v.get("__type__") == "file":
                file_key = k
                file_info = v
                filepath = file_info.get(
                    "filepath",
                    (Path(self.base_dir) / current_folder / file_key).as_posix()
                    if self.base_dir
                    else (Path(current_folder) / file_key).as_posix(),
                )
                target_filename = file_info.get("target_filename", "")

                if query and not parent_folder_matches:
                    file_matches = matching_ids is not None and id(v) in matching_ids
                    if not file_matches:
                        continue

                is_locked = (
                    file_key in self.locked_files
                    or filepath in self.locked_files
                    or file_info.get("is_locked")
                )
                rating = self._ratings_cache.get(filepath) or file_info.get("rating")

                invalid_locks = getattr(self.plan, "invalid_locks", {}) or {}
                if not invalid_locks and isinstance(self.plan, dict):
                    invalid_locks = self.plan.get("invalid_locks", {}) or {}

                is_invalid_lock = (
                    filepath in invalid_locks
                    or file_key in invalid_locks
                    or (bool(filepath) and filepath.replace("\\", "/") in invalid_locks)
                    or (bool(filepath) and os.path.normpath(filepath) in invalid_locks)
                )

                is_excluded = file_info.get("is_excluded", False)
                if "is_checked" not in file_info:
                    is_checked = not is_excluded
                else:
                    is_checked = file_info.get("is_checked", True)
                if is_excluded:
                    is_checked = False
                file_info["is_checked"] = is_checked
                file_info["is_excluded"] = is_excluded

                cb_str = "[x]" if is_checked else "[ ]"
                label_parts = [cb_str]
                if is_invalid_lock:
                    label_parts.append("[LOCK WARNING]")
                elif is_locked:
                    label_parts.append("[LOCKED]")
                if is_excluded:
                    label_parts.append("[EXCLUDED]")

                source_root = file_info.get("source_root")
                if source_root:
                    root_name = os.path.basename(source_root) or source_root
                    label_parts.append(f"[ROOT: {root_name}]")

                if rating == "positive":
                    label_parts.append("[+]")
                elif rating == "negative":
                    label_parts.append("[-]")

                routed_by = file_info.get("routed_by")
                if routed_by == "jev_classifier" or file_info.get("is_jev"):
                    label_parts.append("[JEV]")

                sens_rating = file_info.get("sensitivity_rating")
                if sens_rating:
                    label_parts.append(f"[SENS: {str(sens_rating).upper()}]")

                arch_prio = file_info.get("archival_priority")
                if arch_prio is not None:
                    arch_str = (
                        f"P{arch_prio}"
                        if isinstance(arch_prio, int)
                        or (
                            isinstance(arch_prio, str)
                            and not str(arch_prio).upper().startswith("P")
                        )
                        else str(arch_prio).upper()
                    )
                    label_parts.append(f"[ARCH: {arch_str}]")

                target_filename = file_info.get("target_filename") or file_key
                target_path = None
                if self.base_dir:
                    target_path = os.path.normpath(
                        os.path.join(self.base_dir, current_folder, target_filename)
                    )
                elif current_folder:
                    target_path = os.path.normpath(
                        os.path.join(current_folder, target_filename)
                    )

                is_collision = (
                    file_info.get("collision")
                    or file_info.get("has_collision")
                    or False
                )
                if (
                    not is_collision
                    and target_path
                    and filepath
                    and os.path.lexists(target_path)
                ):
                    try:
                        is_same = os.path.samefile(target_path, filepath)
                    except Exception:
                        is_same = os.path.normpath(
                            os.path.abspath(target_path)
                        ) == os.path.normpath(os.path.abspath(filepath))
                    if not is_same:
                        is_collision = True

                pol = str(getattr(self.settings, "CONFLICT_POLICY", "rename")).lower()
                if is_collision:
                    label_parts.append(f"[COLLISION: {pol.upper()}]")

                label_parts.append(file_key)
                if target_filename and target_filename != file_key:
                    label_parts.append(f"-> {target_filename}")

                norm_fp = os.path.normpath(filepath) if filepath else ""
                norm_key = os.path.normpath(file_key) if file_key else ""

                is_crit_node = (
                    norm_fp in self.critical_error_paths
                    or norm_key in self.critical_error_paths
                    or any(
                        p
                        and (
                            p == norm_fp
                            or p == norm_key
                            or p.endswith("/" + norm_key)
                            or p.endswith("\\" + norm_key)
                        )
                        for p in self.critical_error_paths
                    )
                )
                is_warn_node = (
                    norm_fp in self.warning_paths
                    or norm_key in self.warning_paths
                    or any(
                        p
                        and (
                            p == norm_fp
                            or p == norm_key
                            or p.endswith("/" + norm_key)
                            or p.endswith("\\" + norm_key)
                        )
                        for p in self.warning_paths
                    )
                )

                label_str = cb_str + " 📄 " + " ".join(label_parts[1:])
                text_obj = Text(label_str)
                if is_crit_node:
                    text_obj.stylize("bold red")
                elif is_warn_node:
                    text_obj.stylize("bold yellow")

                node_data = {
                    "is_file": True,
                    "key": file_key,
                    "filepath": filepath,
                    "folder": current_folder,
                    "info": file_info,
                    "is_locked": is_locked,
                    "rating": rating,
                    "is_checked": is_checked,
                    "is_excluded": is_excluded,
                    "is_collision": is_collision,
                    "conflict_policy": pol,
                }
                parent_item.add_leaf(text_obj, data=node_data)
                added_count += 1
            elif isinstance(v, dict):
                sub_folder = (
                    (Path(current_folder) / k).as_posix() if current_folder else k
                )
                folder_matches = (
                    (query in k.lower() or query in sub_folder.lower())
                    if query
                    else True
                )

                if query and not (
                    parent_folder_matches
                    or folder_matches
                    or (matching_ids is not None and id(v) in matching_ids)
                ):
                    continue

                node_data = {
                    "is_file": False,
                    "key": k,
                    "folder": sub_folder,
                    "info": v,
                }
                child_files = self._collect_file_nodes_from_dict(v)
                if child_files:
                    checked_count = sum(
                        1
                        for f in child_files
                        if f.get("is_checked", True) and not f.get("is_excluded", False)
                    )
                    if checked_count == len(child_files):
                        folder_cb = "[x] "
                    elif checked_count > 0:
                        folder_cb = "[-] "
                    else:
                        folder_cb = "[ ] "
                else:
                    folder_cb = ""

                child_tree_node = parent_item.add(
                    Text(f"{folder_cb}📁 {k}"), data=node_data, expand=True
                )
                child_added = self._build_tree_nodes(
                    v,
                    child_tree_node,
                    current_folder=sub_folder,
                    query=query,
                    parent_folder_matches=parent_folder_matches or folder_matches,
                    matching_ids=matching_ids,
                )
                added_count += 1 + child_added

        return added_count

    def _update_inspector(self, node_or_data: Any = None, announce: bool = True) -> str:
        """Update and return formatted metadata text for the inspector panel and path comparison pane."""
        if node_or_data is None:
            node_or_data = self.active_tree_node

        data = None
        if hasattr(node_or_data, "data"):
            data = getattr(node_or_data, "data", None)
        elif isinstance(node_or_data, dict):
            data = node_or_data

        path_comp_widget = None
        try:
            path_comp_widget = self.query_one("#path-comparison", Static)
        except Exception:
            pass

        if not data or not isinstance(data, dict):
            msg = (
                "Root folder of sorting plan."
                if getattr(self, "plan", None)
                else self._start_hint()
            )
            try:
                meta_widget = self.query_one("#meta-details", Static)
                meta_widget.update(msg)
            except Exception:
                pass
            if path_comp_widget:
                all_files = (
                    self._collect_file_nodes_from_dict(self.plan)
                    if hasattr(self, "plan") and self.plan
                    else []
                )
                if all_files:
                    path_comp_widget.update(
                        format_folder_path_comparison(all_files, self.base_dir)
                    )
                else:
                    path_comp_widget.update(
                        "Select a file or folder in the tree to compare source and target paths."
                    )
            return msg

        if data.get("is_empty_state"):
            action = data.get("action")
            if action == "generate_sample_corpus":
                msg = "Empty workspace node. Press [G] to create a sample dataset to try sorting on."
            elif action == "select_dir":
                msg = "Empty workspace node. Press [Ctrl+B] to select a different directory."
            else:
                msg = "Empty workspace node. No documents found in target directory."
            if announce:
                self.announce(msg)
            try:
                meta_widget = self.query_one("#meta-details", Static)
                meta_widget.update(msg)
            except Exception:
                pass
            if path_comp_widget:
                path_comp_widget.update("No documents available for path comparison.")
            return msg

        lines = []
        if data.get("is_file"):
            key = data.get("key", "")
            folder = data.get("folder") or "(Root)"
            locked = "Yes [LOCKED]" if data.get("is_locked") else "No"
            rating = data.get("rating") or "None"
            info = data.get("info") if isinstance(data.get("info"), dict) else {}

            src_filepath = data.get("filepath") or info.get("filepath") or key
            target_fn = info.get("target_filename") or key
            target_folder = folder if folder != "(Root)" else ""
            if self.base_dir and target_folder:
                target_filepath = os.path.join(self.base_dir, target_folder, target_fn)
            elif self.base_dir:
                target_filepath = os.path.join(self.base_dir, target_fn)
            else:
                target_filepath = (
                    os.path.join(target_folder, target_fn)
                    if target_folder
                    else target_fn
                )

            if path_comp_widget:
                table = format_side_by_side_path_comparison(
                    source_path=src_filepath,
                    target_path=target_filepath,
                    base_dir=self.base_dir,
                )
                path_comp_widget.update(table)

            lines.append(f"[bold accent]File:[/bold accent] {key}")
            lines.append(f"[bold]Path:[/bold] {data.get('filepath')}")
            lines.append(f"[bold]Target Folder:[/bold] {folder}")

            target_fn = info.get("target_filename")
            if target_fn:
                lines.append(f"[bold]Target Filename:[/bold] {target_fn}")

            lines.append(f"[bold]Locked:[/bold] {locked}")
            lines.append(f"[bold]ML Rating:[/bold] {rating}")

            invalid_locks = getattr(self.plan, "invalid_locks", {}) or {}
            if not invalid_locks and isinstance(self.plan, dict):
                invalid_locks = self.plan.get("invalid_locks", {}) or {}
            filepath_str = data.get("filepath", "")
            key_str = data.get("key", "")
            lock_err = (
                invalid_locks.get(filepath_str)
                or invalid_locks.get(key_str)
                or (
                    invalid_locks.get(filepath_str.replace("\\", "/"))
                    if filepath_str
                    else None
                )
                or (
                    invalid_locks.get(os.path.normpath(filepath_str))
                    if filepath_str
                    else None
                )
            )
            if lock_err:
                lines.append(f"[bold error]Lock Warning:[/bold error] {lock_err}")

            routing_src = info.get("routed_by")
            if routing_src:
                lines.append(f"[bold]Routing Source:[/bold] {routing_src}")

            cat = info.get("category") or info.get("jev_category")
            if cat:
                lines.append(f"[bold]Category:[/bold] {cat}")

            sens_rating = info.get("sensitivity_rating")
            if sens_rating is not None:
                lines.append(f"[bold]Sensitivity Rating:[/bold] {sens_rating}")

            sens_score = info.get("sensitivity_score")
            if sens_score is not None:
                try:
                    lines.append(
                        f"[bold]Sensitivity Score:[/bold] {float(sens_score):.2f}"
                    )
                except (ValueError, TypeError):
                    lines.append(f"[bold]Sensitivity Score:[/bold] {sens_score}")

            arch_prio = info.get("archival_priority")
            if arch_prio is not None:
                arch_str = (
                    f"P{arch_prio}"
                    if isinstance(arch_prio, int)
                    or (
                        isinstance(arch_prio, str)
                        and not str(arch_prio).upper().startswith("P")
                    )
                    else str(arch_prio).upper()
                )
                lines.append(f"[bold]Archival Priority:[/bold] {arch_str}")

            arch_score = info.get("archival_priority_score")
            if arch_score is not None:
                try:
                    lines.append(
                        f"[bold]Archival Priority Score:[/bold] {float(arch_score):.2f}"
                    )
                except (ValueError, TypeError):
                    lines.append(f"[bold]Archival Priority Score:[/bold] {arch_score}")

            conf = info.get("confidence")
            if conf is not None:
                try:
                    lines.append(f"[bold]Confidence:[/bold] {float(conf):.2%}")
                except (ValueError, TypeError):
                    lines.append(f"[bold]Confidence:[/bold] {conf}")

            if data.get("is_collision"):
                pol_str = str(
                    data.get("conflict_policy")
                    or getattr(self.settings, "CONFLICT_POLICY", "rename")
                ).upper()
                lines.append(
                    f"[bold warning]Collision Indicator:[/bold warning] [COLLISION: {pol_str}]"
                )

            if announce:
                self.announce(
                    f"Selected file '{key}' in folder '{folder}'. Locked: {locked}. Rating: {rating}."
                )
        else:
            key = data.get("key", "")
            folder = data.get("folder", "")
            folder_info = data.get("info", {})
            child_files = (
                self._collect_file_nodes_from_dict(folder_info)
                if isinstance(folder_info, dict)
                else []
            )

            if path_comp_widget:
                if child_files:
                    table = format_folder_path_comparison(
                        files_info=child_files,
                        base_dir=self.base_dir,
                    )
                    path_comp_widget.update(table)
                else:
                    path_comp_widget.update(
                        f"Folder category '{key}' contains no document items."
                    )

            lines.append(f"[bold accent]Folder Category:[/bold accent] {key}")
            lines.append(f"[bold]Relative Path:[/bold] {folder}")
            if announce:
                self.announce(f"Selected folder category '{key}' at path '{folder}'.")

        text = "\n".join(lines)
        try:
            meta_widget = self.query_one("#meta-details", Static)
            meta_widget.update(text)
        except Exception:
            pass

        return text

    @on(Tree.NodeHighlighted, "#plan-tree")
    @on(Tree.NodeSelected, "#plan-tree")
    def on_node_selected(
        self, event: Union[Tree.NodeSelected, Tree.NodeHighlighted]
    ) -> None:
        """Handle tree node selection or highlight to update metadata pane and screen reader announcement."""
        self.active_tree_node = event.node
        self._update_inspector(event.node)
        if isinstance(event, Tree.NodeSelected) and event.node and event.node.data:
            if event.node.data.get("is_empty_state"):
                action = event.node.data.get("action")
                if action == "generate_sample_corpus":
                    self.action_generate_sample_corpus()
                elif action == "select_dir":
                    self.action_select_dir()

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

    def action_focus_search(self) -> None:
        """Focus the real-time tree filter search input bar."""
        try:
            inp = self.query_one("#tree-search-input", Input)
            inp.focus()
            self.announce("Focused tree search input bar.")
        except Exception:
            pass

    # --- Checkbox Selection & Bulk Action Handlers ---

    def register_visual_selected_node(self, node: TreeNode) -> None:
        """Register node into visual range selection list when visual selection is active."""
        if not self.visual_select_active or not node:
            return
        if node not in self.visual_selected_nodes:
            self.visual_selected_nodes.append(node)

    def action_visual_range_select(self) -> None:
        """Toggle visual range selection mode [V]."""
        if self._is_text_control_focused():
            return
        self.visual_select_active = not self.visual_select_active
        if self.visual_select_active:
            self.visual_selected_nodes = []
            node = self._get_active_node()
            if node:
                self.visual_selected_nodes.append(node)
            self.announce(
                "Visual range selection mode enabled. Move cursor [j/k] to extend range."
            )
        else:
            self.announce("Visual range selection mode disabled.")

    def action_toggle_selection(self) -> None:
        """Toggle checkbox selection for active node, folder group, or visual range [Space]."""
        if self._is_text_control_focused():
            return

        nodes_to_toggle = []
        if self.visual_select_active and self.visual_selected_nodes:
            nodes_to_toggle = list(self.visual_selected_nodes)
            self.visual_select_active = False
            self.visual_selected_nodes = []
        else:
            active_node = self._get_active_node()
            if active_node:
                nodes_to_toggle = [active_node]

        if not nodes_to_toggle:
            self.announce("Select a tree node to toggle checkbox [Space].")
            return

        file_infos_to_toggle: List[Dict[str, Any]] = []
        for n in nodes_to_toggle:
            data = getattr(n, "data", None)
            if not data or not isinstance(data, dict):
                continue
            if data.get("is_file"):
                file_infos_to_toggle.append(data["info"])
            else:
                info_dict = data.get("info", {})
                file_infos_to_toggle.extend(
                    self._collect_file_nodes_from_dict(info_dict)
                )

        if not file_infos_to_toggle:
            self.announce("No file nodes found to toggle.")
            return

        all_checked = all(
            fi.get("is_checked", True) and not fi.get("is_excluded", False)
            for fi in file_infos_to_toggle
        )
        new_checked = not all_checked

        for fi in file_infos_to_toggle:
            fi["is_checked"] = new_checked
            if new_checked:
                fi["is_excluded"] = False

        self.rebuild_tree()
        status_str = "checked [x]" if new_checked else "unchecked [ ]"
        self.announce(
            f"Toggled {len(file_infos_to_toggle)} document item(s) to {status_str}."
        )

    def _get_target_file_infos(self) -> List[Dict[str, Any]]:
        """Return checked file info dicts, or active/focused node file infos if none checked."""
        all_files = self._collect_file_nodes_from_dict(self.plan)
        checked_files = [
            f
            for f in all_files
            if f.get("is_checked", True) and not f.get("is_excluded", False)
        ]
        if checked_files:
            return checked_files

        active_node = self._get_active_node()
        if not active_node:
            return all_files

        data = getattr(active_node, "data", None)
        if not data or not isinstance(data, dict):
            return all_files

        if data.get("is_file"):
            return [data["info"]]
        else:
            folder_files = self._collect_file_nodes_from_dict(data.get("info", {}))
            return folder_files if folder_files else all_files

    def action_exclude_selected(self) -> None:
        """Bulk exclude checked items or active node [X]."""
        if self._is_text_control_focused():
            return

        target_files = self._get_target_file_infos()
        if not target_files:
            self.announce("No file items available to exclude.")
            return

        for fi in target_files:
            fi["is_checked"] = False
            fi["is_excluded"] = True

        self.rebuild_tree()
        self.announce(f"Excluded {len(target_files)} file(s) from sorting plan.")

    def action_include_selected(self) -> None:
        """Bulk include selected items or active node [I]."""
        if self._is_text_control_focused():
            return

        target_files = self._get_target_file_infos()
        if not target_files:
            self.announce("No file items available to include.")
            return

        for fi in target_files:
            fi["is_checked"] = True
            fi["is_excluded"] = False

        self.rebuild_tree()
        self.announce(f"Included {len(target_files)} file(s) in sorting plan.")

    def action_reassign_selected(self) -> None:
        """Bulk reassign checked file items or active node to target category [R]."""
        if self._is_text_control_focused():
            return

        target_files = self._get_target_file_infos()
        if not target_files:
            self.announce("No file items available to reassign.")
            return

        existing_folders = [
            k
            for k, v in self.plan.items()
            if isinstance(v, dict) and v.get("__type__") != "file"
        ]

        def on_reassigned(target_folder: Optional[str]) -> None:
            if not target_folder:
                return

            reassigned_count = 0
            for fi in target_files:
                file_key = None
                old_folder = fi.get("folder", "")

                for f_name, f_content in list(self.plan.items()):
                    if isinstance(f_content, dict):
                        for k, v in list(f_content.items()):
                            if v is fi:
                                file_key = k
                                old_folder = f_name
                                break
                    if file_key:
                        break

                if not file_key:
                    file_key = fi.get("target_filename") or fi.get("key")

                if file_key and old_folder in self.plan:
                    if file_key in self.plan[old_folder]:
                        self.plan[old_folder].pop(file_key, None)
                    if target_folder not in self.plan:
                        self.plan[target_folder] = {}
                    self.plan[target_folder][file_key] = fi
                    fi["folder"] = target_folder
                    fi["is_locked"] = True
                    self.locked_files[file_key] = target_folder
                    if self.app_session:
                        self.app_session.db.set_user_verified_target_path(
                            self.base_dir, file_key, target_folder
                        )
                    reassigned_count += 1

            self.rebuild_tree()
            self.announce(
                f"Reassigned {reassigned_count} file(s) to folder '{target_folder}'."
            )

        self.push_screen(
            ReassignModal(existing_folders, count=len(target_files)), on_reassigned
        )

    def action_toggle_select_all(self) -> None:
        """Toggle Select All / Deselect All file nodes in plan [A]."""
        if self._is_text_control_focused():
            return

        all_files = self._collect_file_nodes_from_dict(self.plan)
        if not all_files:
            self.announce("No file items available to select/deselect.")
            return

        query = self._get_current_search_query()
        if query:
            target_files = self._collect_visible_file_nodes_from_dict(
                self.plan, query=query
            )
        else:
            target_files = all_files

        if not target_files:
            self.announce(
                f"No visible file items matching '{query}' to select/deselect."
            )
            return

        all_checked = all(
            f.get("is_checked", True) and not f.get("is_excluded", False)
            for f in target_files
        )
        new_state = not all_checked

        for f in target_files:
            f["is_checked"] = new_state
            if new_state:
                f["is_excluded"] = False

        self.rebuild_tree(run_validation=True)

        if query:
            hidden_count = len(all_files) - len(target_files)
            status_str = "Selected" if new_state else "Deselected"
            if hidden_count > 0:
                self.announce(
                    f"{status_str} {len(target_files)} visible file item(s) ({hidden_count} hidden files unchanged)."
                )
            else:
                self.announce(f"{status_str} {len(target_files)} visible file item(s).")
        else:
            status_str = "Selected all" if new_state else "Deselected all"
            self.announce(f"{status_str} {len(all_files)} file item(s).")

    def _get_executable_plan(
        self, plan_dict: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Return filtered plan dictionary containing only checked, non-excluded file nodes."""
        if plan_dict is None:
            plan_dict = self.plan

        result: Dict[str, Any] = {}
        for k, v in plan_dict.items():
            if isinstance(v, dict) and v.get("__type__") == "file":
                is_checked = v.get("is_checked", True)
                is_excluded = v.get("is_excluded", False)
                if is_checked and not is_excluded:
                    result[k] = v
            elif isinstance(v, dict):
                sub = self._get_executable_plan(v)
                if sub:
                    result[k] = sub
            else:
                result[k] = v
        return result

    # --- Keyboard Action Hotkeys ---

    def action_toggle_lock(self) -> None:
        """Toggle node lock state [Ctrl+L]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        node = self._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            self.announce("Select a file node to toggle lock [Ctrl+L].")
            return

        data = node.data
        file_key = data["key"]
        filepath = data["filepath"]
        folder = data["folder"]

        if data.get("is_locked"):
            self.locked_files.pop(file_key, None)
            self.locked_files.pop(filepath, None)
            data["is_locked"] = False
            if self.app_session:
                self.app_session.db.set_user_verified_target_path(
                    self.base_dir, file_key, None
                )
            msg = f"Unlocked file '{file_key}'"
        else:
            self.locked_files[file_key] = folder
            data["is_locked"] = True
            if self.app_session:
                self.app_session.db.set_user_verified_target_path(
                    self.base_dir, file_key, folder
                )
            msg = f"Locked file '{file_key}' to folder '{folder}'"

        self.announce(msg)
        self.rebuild_tree()

    def action_rename_node(self) -> None:
        """Rename file or folder category node [Ctrl+R]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        node = self._get_active_node()
        if not node or not node.data:
            self.announce("Select a file or folder node to rename [Ctrl+R].")
            return

        data = node.data
        is_file = data.get("is_file")
        old_name = data.get("key")

        if is_file:
            stem, ext = os.path.splitext(old_name)
            folder_cat = data.get("folder", "")
            modal = RenameModal(
                title=f"Rename File: {old_name}",
                current_name=stem,
                extension=ext,
                category=folder_cat,
            )

            def on_renamed(new_formatted: Optional[str]) -> None:
                if not new_formatted:
                    return
                if ext and not new_formatted.endswith(ext):
                    new_filename = f"{new_formatted}{ext}"
                else:
                    new_filename = new_formatted

                if new_filename == old_name:
                    return
                data["info"]["target_filename"] = new_filename
                data["info"]["is_locked"] = True
                data["is_locked"] = True
                self.locked_files[old_name] = data["folder"]
                if self.app_session:
                    self.app_session.db.set_user_verified_target_path(
                        self.base_dir, old_name, data["folder"]
                    )
                self.rebuild_tree()
                self.announce(f"Renamed target file to '{new_filename}' [Locked]")

            self.push_screen(modal, on_renamed)
        else:
            modal = RenameModal(
                title=f"Rename Folder: {old_name}",
                current_name=old_name,
                category=old_name,
            )

            def on_folder_renamed(new_name: Optional[str]) -> None:
                if not new_name or new_name == old_name:
                    return
                if old_name in self.plan:
                    self.plan[new_name] = self.plan.pop(old_name)
                    self.rebuild_tree()
                    self.announce(
                        f"Renamed folder category '{old_name}' -> '{new_name}'"
                    )

            self.push_screen(modal, on_folder_renamed)

    def action_new_folder(self) -> None:
        """Create a new folder category node [Ctrl+N]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        def on_created(folder_name: Optional[str]) -> None:
            if not folder_name:
                return
            if folder_name not in self.plan:
                self.plan[folder_name] = {}
                self.rebuild_tree()
                self.announce(f"Created new folder category: '{folder_name}'")
            else:
                self.announce(f"Folder category '{folder_name}' already exists.")

        self.push_screen(NewFolderModal(), on_created)

    def action_rate_positive(self) -> None:
        """Set positive ML rating feedback [+] for selected node."""
        if self._is_text_control_focused():
            return
        self._set_rating_for_selected("positive")

    def action_rate_negative(self) -> None:
        """Set negative ML rating feedback [-] for selected node."""
        if self._is_text_control_focused():
            return
        self._set_rating_for_selected("negative")

    async def action_quit(self) -> None:
        """Quit application [Ctrl+Q], from any focused widget."""
        await super().action_quit()

    def _set_rating_for_selected(self, rating: str) -> None:
        node = self._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            self.announce("Select a file node to rate [+][-].")
            return

        data = node.data
        filepath = data["filepath"]
        current = self._ratings_cache.get(filepath)

        rating_to_set = None if current == rating else rating
        if rating_to_set:
            self._ratings_cache[filepath] = rating_to_set
        else:
            self._ratings_cache.pop(filepath, None)

        if self.app_session:
            self.app_session.db.set_document_rating(
                self.base_dir, filepath, rating_to_set
            )

        data["rating"] = rating_to_set
        self.rebuild_tree()
        msg = f"Set rating '{rating_to_set or 'cleared'}' for '{data['key']}'"
        self.announce(msg)


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
