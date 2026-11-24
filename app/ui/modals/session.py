"""Session history, rollback confirmation and interrupted-session recovery dialogs."""

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static

from app.config import get_app_dir
from app.core.path_utils import scrub_user_home_paths
from app.core.resilient_file_ops import _set_posix_mode
from app.core.text_utils import sanitize_secret_patterns
from app.log_filter import scrub_diagnostic_text
from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen
from app.ui.notifications import TROUBLESHOOTING_URL, notify

logger = logging.getLogger(__name__)


class SessionRecoveryModal(ResponsiveModalScreen[Optional[str]]):
    """Modal dialog for recovering interrupted file sorting sessions."""

    HELP_URL = f"{TROUBLESHOOTING_URL}#session-recovery"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-title {
        color: $error;
    }
    .button-row {
        layout: grid;
        grid-size: 4;
        grid-columns: 1fr 1fr 1fr 1fr;
        grid-gutter: 0 1;
    }
    .compact .button-row, .narrow .button-row {
        grid-size: 2;
        grid-columns: 1fr 1fr;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    .compact .button-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    Button:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, session_info: dict[str, Any]):
        super().__init__()
        self.session_info = session_info

    def compose(self) -> ComposeResult:
        """Compose session recovery modal children."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Interrupted Session Detected",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    f"Session ID: {self.session_info.get('session_id', 'Unknown')}"
                )
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
        super().on_mount()
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

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel recovery dialog."""
        self.announce("Cancelled session recovery dialog.")
        self.dismiss(None)


class RecoveryResult(str):
    """Result returned when session recovery modal dismisses with an action."""

    action: str
    session: Optional[dict[str, Any]]
    sessions: list[dict[str, Any]]

    def __new__(
        cls,
        action: str,
        session: Optional[dict[str, Any]] = None,
        sessions: Optional[list[dict[str, Any]]] = None,
    ):
        """Instantiate recovery result string with action and session context."""
        obj = super().__new__(cls, action)
        obj.action = action
        obj.session = session
        obj.sessions = sessions or []
        return obj


class MultiSessionRecoveryModal(ResponsiveModalScreen[Optional[Any]]):
    """Modal dialog for recovering multiple interrupted file sorting sessions."""

    HELP_URL = f"{TROUBLESHOOTING_URL}#multi-session-recovery"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-title {
        color: $error;
        margin-bottom: 0;
    }
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .compact .modal-subtitle {
        margin-bottom: 0;
    }
    #sessions-table {
        height: auto;
        min-height: 6;
        max-height: 10;
        border: solid $primary;
        margin-bottom: 1;
    }
    .compact #sessions-table {
        margin-bottom: 0;
    }
    #lbl-selected-session {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .compact #lbl-selected-session {
        margin-bottom: 0;
    }
    .actions-heading {
        margin-top: 1;
        margin-bottom: 0;
    }
    .compact .actions-heading {
        margin-top: 0;
    }
    .compact .button-row {
        margin-top: 0;
    }
    .batch-button-row {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-gutter: 0 1;
    }
    .item-button-row {
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    .compact .button-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    Button:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        sessions: Optional[list[dict[str, Any]]] = None,
        settings: Optional[Any] = None,
    ):
        super().__init__()
        if sessions is None:
            sessions = []
        elif isinstance(sessions, dict):
            sessions = [sessions]
        self.sessions: list[dict[str, Any]] = list(sessions)
        self.settings = settings
        self.selected_index: int = 0

    def compose(self) -> ComposeResult:
        """Compose multi-session recovery modal widgets."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Multiple Interrupted Sessions Detected",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    "Sortify detected multiple interrupted sessions. Review or perform batch recovery:",
                    classes="modal-subtitle",
                    id="modal-subtitle",
                )

                yield DataTable(id="sessions-table")

                yield Label(
                    "Selected: None",
                    id="lbl-selected-session",
                )

            with Vertical(classes="modal-footer"):
                yield Heading("Batch Actions:", level=2, classes="actions-heading")
                with Horizontal(classes="button-row batch-button-row"):
                    btn_rollback_all = Button(
                        "Rollback All", id="btn-rollback-all", variant="warning"
                    )
                    btn_rollback_all.tooltip = (
                        "Rollback uncommitted operations across all abandoned sessions"
                    )
                    yield btn_rollback_all

                    btn_clean_all = Button(
                        "Clean All", id="btn-clean-all", variant="error"
                    )
                    btn_clean_all.tooltip = (
                        "Discard all abandoned sessions and clean records"
                    )
                    yield btn_clean_all

                yield Heading(
                    "Item Actions (Selected Session):",
                    level=2,
                    classes="actions-heading",
                )
                with Horizontal(classes="button-row item-button-row"):
                    btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                    btn_cancel.tooltip = "Cancel recovery dialog"
                    yield btn_cancel

                    btn_help = Button("Help", id="btn-help", variant="default")
                    btn_help.tooltip = "Open session recovery troubleshooting guide"
                    yield btn_help

                    btn_clean = Button("Clean", id="btn-clean", variant="error")
                    btn_clean.tooltip = (
                        "Discard selected session files and clean record"
                    )
                    yield btn_clean

                    btn_rollback = Button(
                        "Rollback", id="btn-rollback", variant="warning"
                    )
                    btn_rollback.tooltip = (
                        "Rollback partial file moves for selected session"
                    )
                    yield btn_rollback

                    btn_resume = Button("Resume", id="btn-resume", variant="primary")
                    btn_resume.tooltip = (
                        "Resume pending file moves for selected session"
                    )
                    yield btn_resume

    def on_mount(self) -> None:
        """Initialize table columns, populate rows, and announce session count."""
        super().on_mount()
        table = self.query_one("#sessions-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Session ID", "Target Directory", "Step Count", "Status")
        self._refresh_table()
        count = len(self.sessions)
        self.announce(
            f"Opened multi-session recovery dialog. Detected {count} abandoned sessions."
        )
        try:
            self.query_one("#btn-resume", Button).focus()
        except Exception:
            pass

    def _refresh_table(self) -> None:
        """Refresh DataTable rows from self.sessions."""
        try:
            table = self.query_one("#sessions-table", DataTable)
        except Exception:
            return
        table.clear()
        for idx, sess in enumerate(self.sessions):
            sid = str(sess.get("session_id", "Unknown"))
            b_dir = scrub_user_home_paths(str(sess.get("base_dir") or "Unknown"))
            steps = str(sess.get("step_count", 0))
            status = str(sess.get("status", "Unknown"))
            table.add_row(sid, b_dir, steps, status, key=str(idx))
        if self.sessions:
            if self.selected_index >= len(self.sessions):
                self.selected_index = max(0, len(self.sessions) - 1)
            try:
                table.move_cursor(row=self.selected_index)
            except Exception:
                pass
        self._update_selected_session_display()

    def get_selected_session(self) -> Optional[dict[str, Any]]:
        """Return the currently selected session dictionary."""
        if not self.sessions:
            return None
        try:
            table = self.query_one("#sessions-table", DataTable)
            row_idx = table.cursor_row
            if row_idx is not None and 0 <= row_idx < len(self.sessions):
                self.selected_index = row_idx
        except Exception:
            pass
        if 0 <= self.selected_index < len(self.sessions):
            return self.sessions[self.selected_index]
        return self.sessions[0]

    def _update_selected_session_display(self) -> None:
        """Update selected session details label."""
        try:
            lbl = self.query_one("#lbl-selected-session", Label)
        except Exception:
            return
        sess = self.get_selected_session()
        if not sess:
            lbl.update("Selected: No sessions remaining.")
            return
        sid = sess.get("session_id", "Unknown")
        b_dir = scrub_user_home_paths(str(sess.get("base_dir") or "Unknown"))
        steps = sess.get("step_count", 0)
        status = sess.get("status", "Unknown")
        lbl.update(
            f"Selected: {sid} | Target: {b_dir} | Steps: {steps} | Status: {status}"
        )

    @on(DataTable.RowHighlighted, "#sessions-table")
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Handle row cursor movement in sessions table."""
        if 0 <= event.cursor_row < len(self.sessions):
            self.selected_index = event.cursor_row
            self._update_selected_session_display()
            sid = self.sessions[self.selected_index].get("session_id", "Unknown")
            self.announce(f"Selected session '{sid}'.")

    @on(DataTable.RowSelected, "#sessions-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in sessions table."""
        if 0 <= event.cursor_row < len(self.sessions):
            self.selected_index = event.cursor_row
            self._update_selected_session_display()

    @on(Button.Pressed, "#btn-clean-all")
    def action_clean_all(self) -> None:
        """Confirm clean all action."""
        self.announce("Confirmed cleanup of all abandoned sessions.")
        self.dismiss(RecoveryResult("clean_all", sessions=list(self.sessions)))

    @on(Button.Pressed, "#btn-rollback-all")
    def action_rollback_all(self) -> None:
        """Confirm rollback all action."""
        self.announce("Confirmed rollback of all abandoned sessions.")
        self.dismiss(RecoveryResult("rollback_all", sessions=list(self.sessions)))

    @on(Button.Pressed, "#btn-resume")
    def action_resume(self) -> None:
        """Confirm resume action for the selected session."""
        selected = self.get_selected_session()
        if not selected:
            return
        sid = selected.get("session_id", "Unknown")
        self.announce(f"Confirmed resume for session '{sid}'.")
        self.dismiss(
            RecoveryResult("resume", session=selected, sessions=list(self.sessions))
        )

    @on(Button.Pressed, "#btn-clean")
    def action_clean(self) -> None:
        """Clean currently selected session."""
        selected = self.get_selected_session()
        if not selected:
            return
        sid = selected.get("session_id", "Unknown")
        app_clean = getattr(self.app, "_do_recovery_clean", None)
        if callable(app_clean):
            try:
                app_clean(selected)
            except Exception as e:
                logger.error(f"Error cleaning session {sid}: {e}")
        else:
            from app.core.session_catalog import forget_abandoned_session

            try:
                forget_abandoned_session(selected)
            except Exception as e:
                logger.error(f"Error cleaning session {sid}: {e}")

        self.announce(f"Cleaned session files for '{sid}'.")
        self.sessions = [s for s in self.sessions if s.get("session_id") != sid]
        self._refresh_table()
        if not self.sessions:
            self.dismiss(RecoveryResult("clean", session=selected, sessions=[]))

    @on(Button.Pressed, "#btn-rollback")
    def action_rollback(self) -> None:
        """Rollback currently selected session."""
        selected = self.get_selected_session()
        if not selected:
            return
        sid = selected.get("session_id", "Unknown")
        app_rollback = getattr(self.app, "_do_recovery_rollback", None)
        if callable(app_rollback):
            outcome = app_rollback(selected)
            succeeded = outcome is not False
        else:
            from app.core.session_catalog import roll_back_abandoned_session

            settings = getattr(self, "settings", None) or getattr(
                self.app, "settings", None
            )
            succeeded = (
                bool(roll_back_abandoned_session(settings, selected))
                if settings
                else False
            )
            action_scan = getattr(self.app, "action_scan_directory", None)
            if callable(action_scan):
                try:
                    action_scan()
                except Exception:
                    pass

        if not succeeded:
            self.announce(
                f"Session recovery failed for '{sid}': write/permission error. Session files and safety storage preserved."
            )
            return

        self.announce(f"Rolled back session '{sid}'.")
        self.sessions = [s for s in self.sessions if s.get("session_id") != sid]
        self._refresh_table()
        if not self.sessions:
            self.dismiss(RecoveryResult("rollback", session=selected, sessions=[]))

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel recovery dialog."""
        self.announce("Cancelled multi-session recovery dialog.")
        self.dismiss(None)


class RollbackConfirmModal(ResponsiveModalScreen[Optional[dict[str, Any]]]):
    """Two-stage interactive confirmation modal dialog before executing session rollback."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel rollback dialog", show=True),
        Binding("space", "toggle_current_row", "Toggle selection", show=False),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    RollbackConfirmModal {
        background: rgba(0, 0, 0, 0.7);
    }
    .confirm-box {
        border: thick $warning;
        width: 92%;
        max-width: 98;
        height: 90%;
        max-height: 92%;
    }
    .compact .confirm-box, .narrow .confirm-box {
        width: 98%;
        height: 94%;
        max-height: 96%;
    }
    #stage-1-container, #stage-2-container {
        width: 100%;
        height: 100%;
        overflow-y: hidden;
    }
    #stage-2-container {
        display: none;
    }
    .modal-body {
        height: 1fr;
    }
    .modal-title {
        color: $warning;
        margin-bottom: 0;
    }
    .error-banner {
        color: $error;
        background: $boost;
        border: solid $error;
        padding: 0 1;
        margin-top: 1;
        margin-bottom: 1;
        text-style: bold;
        display: none;
    }
    .controls-row {
        width: 100%;
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
        layout: horizontal;
    }
    .compact .controls-row, .narrow .controls-row {
        layout: vertical;
        margin-top: 0;
        margin-bottom: 0;
    }
    #rollback-search-input {
        width: 1fr;
        margin-right: 1;
    }
    .compact #rollback-search-input, .narrow #rollback-search-input {
        width: 100%;
        margin-right: 0;
    }
    .bulk-actions-row {
        width: auto;
        height: auto;
        layout: horizontal;
    }
    .compact .bulk-actions-row, .narrow .bulk-actions-row {
        width: 100%;
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
    }
    .bulk-btn {
        min-width: 12;
        margin-right: 1;
    }
    .compact .bulk-btn, .narrow .bulk-btn {
        width: 100%;
        min-width: 0;
        margin-right: 0;
        height: 1;
        min-height: 1;
        border: none;
    }
    #lbl-selection-count {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .compact #lbl-selection-count {
        margin-bottom: 0;
    }
    #rollback-items-table, #dryrun-diff-table {
        width: 100%;
        height: auto;
        min-height: 7;
        max-height: 14;
        border: solid $primary;
        margin-bottom: 1;
    }
    .compact #rollback-items-table, .compact #dryrun-diff-table {
        margin-bottom: 0;
    }
    .summary-bar {
        width: 100%;
        height: auto;
        margin-top: 1;
        margin-bottom: 1;
        background: $surface;
        border: solid $accent;
        padding: 0 1;
        layout: horizontal;
    }
    .compact .summary-bar, .narrow .summary-bar {
        margin-top: 0;
        margin-bottom: 0;
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-gutter: 0 1;
    }
    .summary-stat {
        width: 1fr;
        margin-right: 1;
        text-style: bold;
    }
    .summary-stat.clean {
        color: $success;
    }
    .summary-stat.collision {
        color: $error;
    }
    .summary-stat.missing {
        color: $warning;
    }
    .warning-box {
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 1;
        background: $boost;
        border: dashed $warning;
        color: $text;
        height: auto;
    }
    .compact .warning-box {
        margin-top: 0;
        margin-bottom: 0;
    }
    .button-row {
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
    }
    .compact .button-row {
        margin-top: 0;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    .compact .button-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    Button:focus, Input:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, session_info: dict[str, Any]):
        super().__init__()
        self.session_info = session_info
        self.all_items: list[dict[str, Any]] = list(session_info.get("items") or [])
        self.selected_step_ids: set = set()
        self.filtered_items: list[dict[str, Any]] = []
        self.history_error: Optional[str] = None
        self.current_stage: int = 1
        self.preview_data: Optional[dict[str, Any]] = None

    def compose(self) -> ComposeResult:
        """Compose confirmation modal widgets across Stage 1 and Stage 2."""
        sid = self.session_info.get("session_id", "unknown")
        b_dir = self.session_info.get("base_dir") or "N/A"
        ts = self.session_info.get("timestamp")
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "N/A"

        with ModalBox(classes="confirm-box modal-box"):
            # Stage 1 Container: Step Selection
            with Vertical(id="stage-1-container"):
                yield Heading(
                    "Confirm Session Rollback",
                    level=1,
                    classes="modal-title",
                    id="confirm-title",
                )
                yield Label(
                    f"Session ID: {sid} | Base Dir: {b_dir} | Date: {ts_str}",
                    id="lbl-confirm-sid",
                )
                yield Static("", id="rollback-error-banner", classes="error-banner")

                with Vertical(classes="modal-body"):
                    with Horizontal(classes="controls-row"):
                        yield Input(
                            placeholder="Search paths or status to filter...",
                            id="rollback-search-input",
                        )
                        with Horizontal(classes="bulk-actions-row"):
                            yield Button(
                                "Select All",
                                id="btn-select-all",
                                classes="bulk-btn",
                                variant="primary",
                            )
                            yield Button(
                                "Deselect All",
                                id="btn-deselect-all",
                                classes="bulk-btn",
                                variant="default",
                            )
                            yield Button(
                                "Invert Selection",
                                id="btn-invert-selection",
                                classes="bulk-btn",
                                variant="default",
                            )

                    yield Label(
                        "Selected for Rollback: 0 items", id="lbl-selection-count"
                    )

                    yield DataTable(id="rollback-items-table")

                    with Vertical(classes="warning-box"):
                        yield Label(
                            "⚠️ Warning: Rolling back will restore checked files to their original locations. Unchecked files will remain in target folders."
                        )

                    if self.session_info.get("step_count"):
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
                    yield Button("Cancel", id="btn-cancel-rollback", variant="default")
                    yield Button(
                        "Preview Dry-Run",
                        id="btn-preview-dryrun",
                        variant="primary",
                    )
                    yield Button(
                        "Confirm Rollback",
                        id="btn-confirm-rollback",
                        variant="error",
                    )

            # Stage 2 Container: Dry-Run Diff Review
            with Vertical(id="stage-2-container"):
                yield Heading(
                    "Dry-Run Diff Review",
                    level=1,
                    classes="modal-title",
                    id="dryrun-title",
                )
                yield Label(
                    f"Session ID: {sid} | Base Dir: {b_dir}",
                    id="lbl-dryrun-sid",
                )

                with Vertical(classes="modal-body"):
                    with Horizontal(classes="summary-bar", id="dryrun-summary-bar"):
                        yield Label(
                            "Total Operations: 0",
                            id="lbl-summary-total",
                            classes="summary-stat",
                        )
                        yield Label(
                            "Clean: 0",
                            id="lbl-summary-clean",
                            classes="summary-stat clean",
                        )
                        yield Label(
                            "Collisions: 0",
                            id="lbl-summary-collisions",
                            classes="summary-stat collision",
                        )
                        yield Label(
                            "Missing Targets: 0",
                            id="lbl-summary-missing",
                            classes="summary-stat missing",
                        )

                    yield DataTable(id="dryrun-diff-table")

                    with Vertical(classes="warning-box", id="dryrun-warning-box"):
                        yield Label(
                            "Review target restoration paths above before executing rollback.",
                            id="lbl-dryrun-warning",
                        )

                with Horizontal(classes="button-row"):
                    yield Button(
                        "Back to Selection",
                        id="btn-back-to-selection",
                        variant="default",
                    )
                    yield Button(
                        "Cancel", id="btn-cancel-rollback-2", variant="default"
                    )
                    yield Button(
                        "Execute Rollback",
                        id="btn-execute-rollback",
                        variant="error",
                    )

    def on_mount(self) -> None:
        """Initialize items, populate tables and focus controls on mount."""
        super().on_mount()
        sid = self.session_info.get("session_id")
        if not self.all_items and sid:
            try:
                from app.core.session_catalog import get_session_moves

                self.all_items = get_session_moves(sid)
            except Exception as exc:
                logger.exception(
                    "Failed to load session history for rollback modal: %s", exc
                )
                self.all_items = []
                raw_msg = str(exc) or "Failed to load session history."
                sanitized_msg = sanitize_secret_patterns(scrub_user_home_paths(raw_msg))
                self.history_error = f"Error loading session history: {sanitized_msg}"

        if self.history_error:
            try:
                banner = self.query_one("#rollback-error-banner", Static)
                banner.update(f"⚠️ {self.history_error}")
                banner.display = True
            except Exception:
                pass

            try:
                btn_confirm = self.query_one("#btn-confirm-rollback", Button)
                btn_confirm.disabled = True
                btn_confirm.tooltip = (
                    "Cannot confirm rollback because session history failed to load."
                )
            except Exception:
                pass

            notify(self.history_error, type="error")
            self.announce(self.history_error, priority="assertive")

        self.selected_step_ids = {item["step_id"] for item in self.all_items}

        dt1 = self.query_one("#rollback-items-table", DataTable)
        dt1.cursor_type = "row"
        dt1.add_columns(
            "Sel", "ID", "Original Source Path", "Target Sorted Path", "Size", "Status"
        )

        dt2 = self.query_one("#dryrun-diff-table", DataTable)
        dt2.cursor_type = "row"
        dt2.add_columns(
            "ID",
            "Original Source Path",
            "Target Sorted Path",
            "Restoration Target Path",
            "Status",
        )

        self._refresh_table()

        if self.history_error:
            try:
                self.query_one("#btn-cancel-rollback", Button).focus()
            except Exception:
                pass
        else:
            try:
                self.query_one("#btn-confirm-rollback", Button).focus()
            except Exception:
                pass
        self.announce("Opened rollback confirmation modal dialog.")

    def _refresh_table(self) -> None:
        dt = self.query_one("#rollback-items-table", DataTable)
        dt.clear()

        query = ""
        try:
            inp = self.query_one("#rollback-search-input", Input)
            query = inp.value.strip().lower()
        except Exception:
            pass

        self.filtered_items = []
        for item in self.all_items:
            src = str(item.get("source_path", ""))
            dst = str(item.get("target_path", ""))
            stat = str(item.get("status", ""))
            if not query or (
                query in src.lower() or query in dst.lower() or query in stat.lower()
            ):
                self.filtered_items.append(item)

        for item in self.filtered_items:
            sid = item["step_id"]
            chk = "[X]" if sid in self.selected_step_ids else "[ ]"
            sz = item.get("size")
            if sz is not None and isinstance(sz, (int, float)):
                if sz < 1024:
                    sz_str = f"{int(sz)} B"
                elif sz < 1024 * 1024:
                    sz_str = f"{sz / 1024:.1f} KB"
                else:
                    sz_str = f"{sz / (1024 * 1024):.1f} MB"
            else:
                sz_str = "N/A"

            dt.add_row(
                chk,
                str(sid),
                item.get("source_path", ""),
                item.get("target_path", ""),
                sz_str,
                item.get("status", "completed"),
                key=str(sid),
            )

        self._update_count_badge()

    def _update_count_badge(self) -> None:
        total = len(self.all_items)
        selected = len(self.selected_step_ids)
        try:
            lbl = self.query_one("#lbl-selection-count", Label)
            lbl.update(f"Selected for Rollback: {selected} / {total} items")
        except Exception:
            pass

    def _compute_dryrun_preview(self) -> dict[str, Any]:
        """Compute dry-run simulation preview via session_catalog."""
        from app.core.session_catalog import preview_session_rollback

        sid = self.session_info.get("session_id")
        chk = self.query("#chk-ignore-missing").results(Checkbox)
        chk_box = next(iter(chk), None)
        ignore_missing = chk_box.value if chk_box else False
        selected_ids = list(self.selected_step_ids) if self.selected_step_ids else []

        return preview_session_rollback(
            sid,
            selected_step_ids=selected_ids,
            ignore_missing=ignore_missing,
            fallback_items=self.all_items,
        )

    def _show_stage_2(self) -> None:
        """Compute dry-run preview and switch UI view to Stage 2."""
        preview = self._compute_dryrun_preview()
        self.preview_data = preview

        summ = preview["summary"]
        self.query_one("#lbl-summary-total", Label).update(
            f"Total Operations: {summ['total']}"
        )
        self.query_one("#lbl-summary-clean", Label).update(f"Clean: {summ['clean']}")
        self.query_one("#lbl-summary-collisions", Label).update(
            f"Collisions: {summ['collision']}"
        )
        self.query_one("#lbl-summary-missing", Label).update(
            f"Missing Targets: {summ['missing_target']}"
        )

        dt2 = self.query_one("#dryrun-diff-table", DataTable)
        dt2.clear()

        for step in preview["steps"]:
            sid_str = str(step["step_id"])
            src = str(step["source_path"])
            dst = str(step["target_path"])
            rest = str(step["restoration_path"])
            st = str(step["status"])

            if st == "COLLISION":
                st_badge = f"[bold red]{st}[/bold red]"
            elif st == "MISSING_TARGET":
                st_badge = f"[bold yellow]{st}[/bold yellow]"
            elif st == "IDENTICAL_EXISTS":
                st_badge = f"[bold blue]{st}[/bold blue]"
            else:
                st_badge = f"[bold green]{st}[/bold green]"

            dt2.add_row(
                sid_str,
                src,
                dst,
                rest,
                st_badge,
                key=sid_str,
            )

        warn_lbl = self.query_one("#lbl-dryrun-warning", Label)
        if summ["collision"] > 0:
            warn_lbl.update(
                f"⚠️ Collision Warning: {summ['collision']} file(s) occupy original locations and will be restored with suffixed names."
            )
        elif summ["missing_target"] > 0:
            warn_lbl.update(
                f"⚠️ Missing Targets: {summ['missing_target']} file(s) are missing from disk and will be skipped."
            )
        else:
            warn_lbl.update(
                "✅ Pre-flight dry-run simulation clean. All selected steps ready for restoration."
            )

        self.query_one("#stage-1-container").styles.display = "none"
        self.query_one("#stage-2-container").styles.display = "block"
        self.current_stage = 2
        self.query_one("#btn-execute-rollback", Button).focus()
        self.announce("Opened Stage 2 Dry-Run Diff Review.")

    def _show_stage_1(self) -> None:
        """Switch UI view back to Stage 1 Step Selection."""
        self.query_one("#stage-2-container").styles.display = "none"
        self.query_one("#stage-1-container").styles.display = "block"
        self.current_stage = 1
        self.query_one("#btn-preview-dryrun", Button).focus()
        self.announce("Returned to Stage 1 Step Selection.")

    @on(Button.Pressed, "#btn-preview-dryrun")
    def action_preview_dryrun(self) -> None:
        """Trigger dry-run simulation and transition to Stage 2."""
        self._show_stage_2()

    @on(Button.Pressed, "#btn-back-to-selection")
    def action_back_to_selection(self) -> None:
        """Return from Stage 2 to Stage 1."""
        self._show_stage_1()

    @on(Input.Changed, "#rollback-search-input")
    def _on_search_changed(self, event: Input.Changed) -> None:
        self._refresh_table()

    @on(Button.Pressed, "#btn-select-all")
    def action_select_all(self) -> None:
        """Select all items in filtered table view."""
        for item in getattr(self, "filtered_items", self.all_items):
            self.selected_step_ids.add(item["step_id"])
        self._refresh_table()
        self.announce("Selected all items for rollback.")

    @on(Button.Pressed, "#btn-deselect-all")
    def action_deselect_all(self) -> None:
        """Deselect all items in filtered table view."""
        for item in getattr(self, "filtered_items", self.all_items):
            self.selected_step_ids.discard(item["step_id"])
        self._refresh_table()
        self.announce("Deselected all items for rollback.")

    @on(Button.Pressed, "#btn-invert-selection")
    def action_invert_selection(self) -> None:
        """Invert selection state of all items in filtered table view."""
        for item in getattr(self, "filtered_items", self.all_items):
            sid = item["step_id"]
            if sid in self.selected_step_ids:
                self.selected_step_ids.remove(sid)
            else:
                self.selected_step_ids.add(sid)
        self._refresh_table()
        self.announce("Inverted item selection.")

    @on(DataTable.RowSelected, "#rollback-items-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        row_key = event.row_key.value
        if not row_key:
            return
        self._toggle_item_by_key(str(row_key))

    def action_toggle_current_row(self) -> None:
        """Toggle selection state for the currently focused table row."""
        dt = self.query_one("#rollback-items-table", DataTable)
        if dt.has_focus and dt.cursor_row is not None:
            try:
                row_key, _ = dt.coordinate_to_cell_key(Coordinate(dt.cursor_row, 0))
                if row_key and row_key.value:
                    self._toggle_item_by_key(str(row_key.value))
            except Exception:
                pass

    def _toggle_item_by_key(self, key_str: str) -> None:
        target_id = None
        for item in self.all_items:
            if str(item["step_id"]) == key_str:
                target_id = item["step_id"]
                break

        if target_id is not None:
            if target_id in self.selected_step_ids:
                self.selected_step_ids.remove(target_id)
            else:
                self.selected_step_ids.add(target_id)
            self._refresh_table()

    @on(Button.Pressed, "#btn-execute-rollback")
    @on(Button.Pressed, "#btn-confirm-rollback")
    def action_confirm(self) -> None:
        """Confirm rollback action and dismiss modal with parameters."""
        try:
            btn = self.query_one("#btn-confirm-rollback", Button)
            if btn.disabled or self.history_error is not None:
                return
        except Exception:
            if self.history_error is not None:
                return

        checkboxes = self.query("#chk-ignore-missing").results(Checkbox)
        chk = next(iter(checkboxes), None)

        selected_ids = None
        if self.all_items and len(self.selected_step_ids) < len(self.all_items):
            selected_ids = list(self.selected_step_ids)

        self.dismiss(
            {
                "confirmed": True,
                "session_id": self.session_info.get("session_id"),
                "ignore_missing": chk.value if chk else False,
                "selected_step_ids": selected_ids,
            }
        )

    @on(Button.Pressed, "#btn-cancel-rollback")
    @on(Button.Pressed, "#btn-cancel-rollback-2")
    def action_cancel(self) -> None:
        """Cancel rollback confirmation dialog."""
        self.dismiss(None)


class RollbackErrorModal(ResponsiveModalScreen[None]):
    """Modal dialog displaying error messages encountered during session rollback."""

    HELP_URL = f"{TROUBLESHOOTING_URL}#session-recovery"

    BINDINGS = [
        Binding("escape", "close", "Close error dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    RollbackErrorModal {
        background: rgba(0, 0, 0, 0.7);
    }
    .error-box {
        border: thick $error;
    }
    .narrow .error-box {
        max-height: 96%;
    }
    .modal-title {
        color: $error;
    }
    .error-text {
        margin-top: 1;
        margin-bottom: 1;
        color: $text;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        title: str = "Rollback Failed",
        error_message: str = "",
        settings: Any = None,
    ):
        super().__init__()
        self.error_title = title
        self.error_message = error_message
        self.settings = settings
        self.help_url = f"{TROUBLESHOOTING_URL}#session-recovery"
        self.last_exported_diagnostics_path: Optional[str] = None

    def compose(self) -> ComposeResult:
        """Compose error modal widgets."""
        with ModalBox(classes="error-box modal-box"):
            yield Heading(
                self.error_title, level=1, classes="modal-title", id="error-title"
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    self.error_message, classes="error-text", id="lbl-error-message"
                )
            with Horizontal(classes="button-row"):
                btn_help = Button("Help", id="btn-help", variant="default")
                btn_help.tooltip = "Open session recovery troubleshooting guide"
                yield btn_help

                btn_export = Button(
                    "Export Diagnostics",
                    id="btn-export-diagnostics",
                    variant="warning",
                )
                btn_export.tooltip = "Export scrubbed diagnostic log report"
                yield btn_export

                btn_close = Button("Close", id="btn-close-error", variant="primary")
                btn_close.tooltip = "Close rollback error dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Focus close button and announce error message."""
        super().on_mount()
        self.query_one("#btn-close-error", Button).focus()
        self.announce(f"Error dialog: {self.error_title}")

    def _build_scrubbed_diagnostics_payload(self, settings_obj: Any) -> str:
        """Build a redacted diagnostic report from error context and ``settings.LOG_FILE``."""
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        clean_title = scrub_diagnostic_text(str(self.error_title or "Rollback Failed"))
        clean_error = scrub_diagnostic_text(str(self.error_message or ""))

        lines = [
            "Sortify Rollback Diagnostic Report",
            f"Generated: {ts_iso}",
            f"Title: {clean_title}",
            f"Error: {clean_error}",
            "",
            "--- Application Log Excerpt ---",
        ]

        log_file_val = (
            getattr(settings_obj, "LOG_FILE", None)
            if settings_obj is not None
            else None
        )
        log_loaded = False
        if log_file_val:
            try:
                log_path = Path(str(log_file_val))
                if log_path.is_file():
                    raw_lines = log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    for raw_line in raw_lines[-500:]:
                        if "enc:" in raw_line:
                            continue
                        lines.append(scrub_diagnostic_text(raw_line))
                    log_loaded = True
            except OSError:
                log_loaded = False

        if not log_loaded:
            lines.append("Application log file unavailable or absent on disk.")

        return "\n".join(lines) + "\n"

    def _write_secure_diagnostic_file(self, payload: str, app_obj: Any) -> str:
        """Write diagnostic payload with ``0o600`` permissions across fallback directories."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        filename = f"sortify-diagnostics-{stamp}.log"
        candidate_dirs: list[tuple[Path, bool]] = []

        base_dir = getattr(app_obj, "base_dir", None) if app_obj is not None else None
        if base_dir:
            base_path = Path(str(base_dir))
            if base_path.is_dir():
                candidate_dirs.append((base_path, False))

        try:
            candidate_dirs.append((Path(get_app_dir()), True))
        except Exception:
            pass

        for target_dir, allow_create in candidate_dirs:
            try:
                if allow_create:
                    target_dir.mkdir(parents=True, exist_ok=True)
                out_path = target_dir / filename
                fd = os.open(
                    str(out_path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                _set_posix_mode(str(out_path), 0o600)
                return str(out_path)
            except OSError:
                continue

        fd, tmp_path = tempfile.mkstemp(prefix="sortify-diagnostics-", suffix=".log")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        _set_posix_mode(tmp_path, 0o600)
        return tmp_path

    @on(Button.Pressed, "#btn-export-diagnostics")
    def action_export_diagnostics(self) -> str:
        """Export scrubbed diagnostic log report with ``0o600`` permissions."""
        app_obj = self._resolve_app_instance()
        settings_obj = getattr(self, "settings", None) or getattr(
            app_obj, "settings", None
        )
        payload = self._build_scrubbed_diagnostics_payload(settings_obj)
        out_path = self._write_secure_diagnostic_file(payload, app_obj)
        self.last_exported_diagnostics_path = out_path
        notify(f"Exported scrubbed diagnostics: {out_path}", type="positive")
        self.announce(f"Exported scrubbed diagnostics: {out_path}")
        return out_path

    @on(Button.Pressed, "#btn-close-error")
    def action_close(self) -> None:
        """Dismiss error modal dialog."""
        self.dismiss(None)


class HistoryModal(ResponsiveModalScreen[Optional[dict[str, Any]]]):
    """Modal dialog for viewing session history records, exporting audit logs, and rolling back sessions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        max-width: 90;
        min-width: 40;
        max-height: 92%;
    }
    .compact .modal-title {
        margin-bottom: 0;
    }
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .compact .modal-subtitle {
        margin-bottom: 0;
    }
    .details-box {
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 1;
        background: $boost;
        border: solid $accent;
        color: $text;
    }
    .button-row {
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    .compact .button-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        sessions: Optional[list[dict[str, Any]]] = None,
        base_dir: str = "",
    ):
        super().__init__()
        if sessions is None:
            from app.core.session_catalog import list_sessions

            sessions = list_sessions()
        self.sessions = sessions or []
        self.base_dir = base_dir
        self._rollback_in_progress = False
        self._rollback_pipeline: Any = None

    def _build_session_select_options(self) -> list[tuple[str, str]]:
        """Build Select widget options for available historical sessions."""
        if not self.sessions:
            return [("No historical sessions found", "none")]
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
        return options

    def compose(self) -> ComposeResult:
        """Compose history view, export, and rollback modal widgets."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Session History & Rollback Controls [Ctrl+Y]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    "Select a session row to export audit log or perform session rollback:",
                    classes="modal-subtitle",
                )

                options = self._build_session_select_options()

                select_sess = Select(
                    options,
                    value=options[0][1] if options else "none",
                    id="select-history-session",
                    allow_blank=False,
                )
                select_sess.tooltip = "Select historical session row for audit log export or session rollback"
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
        super().on_mount()
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

        def handle_confirm_result(result: Optional[dict[str, Any]]) -> None:
            if not result or not result.get("confirmed"):
                return

            sid = result["session_id"]
            ignore_missing = result.get("ignore_missing", False)
            selected_step_ids = result.get("selected_step_ids")

            self.query_one("#btn-rollback-session", Button).disabled = True
            self.query_one("#btn-export", Button).disabled = True
            self.query_one("#btn-cancel", Button).disabled = True
            self._rollback_in_progress = True

            lbl_status = self.query_one("#lbl-rollback-status", Label)
            lbl_status.update(f"⏳ Rolling back session {sid[:8]}... Please wait.")
            self.announce(f"Executing rollback for session {sid[:8]}...")

            self.run_rollback_worker(sid, ignore_missing, selected_step_ids)

        self.app.push_screen(
            RollbackConfirmModal(session_info=target_sess),
            handle_confirm_result,
        )

    @work(thread=True)
    def run_rollback_worker(
        self,
        session_id: str,
        ignore_missing: bool,
        selected_step_ids: Optional[list[Any]] = None,
    ) -> None:
        """Execute session rollback asynchronously in background worker thread."""
        app_obj = getattr(self, "app", None)
        try:
            from app.core.pipeline import SortPipeline

            app_session = getattr(app_obj, "app_session", None)
            settings = getattr(app_obj, "settings", None)
            pipeline = getattr(app_obj, "pipeline", None)
            if pipeline is None or pipeline.state.is_active:
                pipeline = SortPipeline(app_session, settings=settings)
                if app_obj is not None and getattr(app_obj, "pipeline", None) is None:
                    app_obj.pipeline = pipeline
                    state_hook = getattr(
                        app_obj, "_handle_pipeline_state_transition", None
                    )
                    tel_hook = getattr(app_obj, "_handle_pipeline_telemetry", None)
                    if callable(state_hook):
                        pipeline.add_state_change_hook(state_hook)
                    if callable(tel_hook):
                        pipeline.add_telemetry_hook(tel_hook)
            self._rollback_pipeline = pipeline

            undo_result = pipeline.undo(
                session_id,
                ignore_missing=ignore_missing,
                selected_step_ids=selected_step_ids,
            )
            if "error" in undo_result.summary:
                raise RuntimeError(str(undo_result.summary["error"]))
            restored_count = int(
                undo_result.summary.get("restored", len(undo_result.moved))
            )

            if app_obj and hasattr(app_obj, "call_from_thread"):
                app_obj.call_from_thread(
                    self._on_rollback_success, session_id, restored_count
                )
            else:
                self._on_rollback_success(session_id, restored_count)
        except Exception as err:
            if app_obj and hasattr(app_obj, "call_from_thread"):
                app_obj.call_from_thread(self._on_rollback_failure, session_id, err)
            else:
                self._on_rollback_failure(session_id, err)
        finally:
            self._rollback_pipeline = None

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
            RollbackErrorModal(
                title="Rollback Failed",
                error_message=str(err),
                settings=getattr(self.app, "settings", None),
            )
        )

    def _refresh_session_options(self, selected_sid: Optional[str] = None) -> None:
        """Re-populate session select dropdown widget options after state update."""
        select_sess = self.query_one("#select-history-session", Select)
        options = self._build_session_select_options()

        select_sess.set_options(options)
        if selected_sid:
            select_sess.value = selected_sid
        self._update_selected_session_view(select_sess.value)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel history export modal, unless a rollback is still running."""
        from app.core.pipeline import PipelineLifecycleState

        rb_pipe = getattr(self, "_rollback_pipeline", None)
        if self._rollback_in_progress or (
            rb_pipe is not None and rb_pipe.state == PipelineLifecycleState.ROLLING_BACK
        ):
            self.announce("Rollback in progress. Please wait for it to finish.")
            return
        self.dismiss(None)
