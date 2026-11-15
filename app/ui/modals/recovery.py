"""Focused recovery dialogs, re-exported by session for compatibility."""

import logging
from typing import Any, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label

from app.core.path_utils import scrub_user_home_paths
from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.notifications import TROUBLESHOOTING_URL, notify

logger = logging.getLogger(__name__)


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


class MultiSessionRecoveryModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Any]]
):
    """Modal dialog for recovering multiple interrupted file sorting sessions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    MultiSessionRecoveryModal {
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
        margin-bottom: 0;
    }
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    #sessions-table {
        height: 6;
        min-height: 4;
        border: solid $primary;
        margin-bottom: 1;
    }
    #lbl-selected-session {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .actions-heading {
        margin-top: 1;
        margin-bottom: 0;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
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
        with Vertical(classes="modal-box"):
            yield Heading(
                "Multiple Interrupted Sessions Detected",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
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

            yield Heading("Batch Actions:", level=2, classes="actions-heading")
            with Horizontal(classes="button-row"):
                btn_rollback_all = Button(
                    "Rollback All", id="btn-rollback-all", variant="warning"
                )
                btn_rollback_all.tooltip = (
                    "Rollback uncommitted operations across all abandoned sessions"
                )
                yield btn_rollback_all

                btn_clean_all = Button("Clean All", id="btn-clean-all", variant="error")
                btn_clean_all.tooltip = (
                    "Discard all abandoned sessions and clean records"
                )
                yield btn_clean_all

            yield Heading(
                "Item Actions (Selected Session):", level=2, classes="actions-heading"
            )
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel recovery dialog"
                yield btn_cancel

                btn_help = Button("Help", id="btn-help", variant="default")
                btn_help.tooltip = "Open session recovery troubleshooting guide"
                yield btn_help

                btn_clean = Button("Clean", id="btn-clean", variant="error")
                btn_clean.tooltip = "Discard selected session files and clean record"
                yield btn_clean

                btn_rollback = Button("Rollback", id="btn-rollback", variant="warning")
                btn_rollback.tooltip = (
                    "Rollback partial file moves for selected session"
                )
                yield btn_rollback

                btn_resume = Button("Resume", id="btn-resume", variant="primary")
                btn_resume.tooltip = "Resume pending file moves for selected session"
                yield btn_resume

    def on_mount(self) -> None:
        """Initialize table columns, populate rows, and announce session count."""
        self._update_layout(self.size.width)
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
        from app.core.session_catalog import roll_back_abandoned_session

        settings = getattr(self, "settings", None) or getattr(
            self.app, "settings", None
        )
        if settings:
            roll_back_abandoned_session(settings, selected)
        action_scan = getattr(self.app, "action_scan_directory", None)
        if callable(action_scan):
            try:
                action_scan()
            except Exception:
                pass

        self.announce(f"Rolled back session '{sid}'.")
        self.sessions = [s for s in self.sessions if s.get("session_id") != sid]
        self._refresh_table()
        if not self.sessions:
            self.dismiss(RecoveryResult("rollback", session=selected, sessions=[]))

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open session recovery troubleshooting guide."""
        url = f"{TROUBLESHOOTING_URL}#multi-session-recovery"

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel recovery dialog."""
        self.announce("Cancelled multi-session recovery dialog.")
        self.dismiss(None)
