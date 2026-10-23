"""Session history, rollback confirmation and interrupted-session recovery dialogs."""

import time
from typing import Any, Dict, List, Optional

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.notifications import TROUBLESHOOTING_URL


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
        Binding("space", "toggle_current_row", "Toggle selection", show=False),
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
        width: 90%;
        max-width: 95;
        height: 85%;
        max-height: 85%;
    }
    .modal-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 0;
    }
    .controls-row {
        height: 3;
        margin-top: 1;
        margin-bottom: 1;
        layout: horizontal;
    }
    #rollback-search-input {
        width: 40%;
        margin-right: 1;
    }
    .bulk-btn {
        margin-right: 1;
    }
    #lbl-selection-count {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #rollback-items-table {
        height: 1fr;
        border: solid $primary;
        margin-bottom: 1;
    }
    .warning-box {
        margin-top: 1;
        margin-bottom: 1;
        padding: 1;
        background: $boost;
        border: dashed $warning;
        color: $text;
        height: auto;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, session_info: Dict[str, Any]):
        super().__init__()
        self.session_info = session_info
        self.all_items: List[Dict[str, Any]] = list(session_info.get("items") or [])
        self.selected_step_ids: set = set()
        self.filtered_items: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        """Compose confirmation modal widgets."""
        sid = self.session_info.get("session_id", "unknown")
        b_dir = self.session_info.get("base_dir") or "N/A"
        ts = self.session_info.get("timestamp")
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "N/A"

        with Vertical(classes="confirm-box"):
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

            with Horizontal(classes="controls-row"):
                yield Input(
                    placeholder="Search paths or status to filter...",
                    id="rollback-search-input",
                )
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

            yield Label("Selected for Rollback: 0 items", id="lbl-selection-count")

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
                btn_cancel = Button(
                    "Cancel", id="btn-cancel-rollback", variant="default"
                )
                yield btn_cancel
                btn_confirm = Button(
                    "Confirm Rollback", id="btn-confirm-rollback", variant="error"
                )
                yield btn_confirm

    def on_mount(self) -> None:
        """Initialize items, populate table and focus controls on mount."""
        sid = self.session_info.get("session_id")
        if not self.all_items and sid:
            try:
                from app.core.history import open_session_history

                with open_session_history() as history_mgr:
                    self.all_items = history_mgr.get_session_moves(sid)
            except Exception:
                self.all_items = []

        self.selected_step_ids = {item["step_id"] for item in self.all_items}

        dt = self.query_one("#rollback-items-table", DataTable)
        dt.cursor_type = "row"
        dt.add_columns(
            "Sel", "ID", "Original Source Path", "Target Sorted Path", "Size", "Status"
        )

        self._refresh_table()

        self.query_one("#btn-confirm-rollback", Button).focus()
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
                row_key, _ = dt.coordinate_to_cell_key((dt.cursor_row, 0))
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

    @on(Button.Pressed, "#btn-confirm-rollback")
    def action_confirm(self) -> None:
        """Confirm rollback action and dismiss modal with parameters."""
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
        selected_step_ids: Optional[List[Any]] = None,
    ) -> None:
        """Execute session rollback asynchronously in background worker thread."""
        try:
            from app.core.history import open_session_history

            kwargs: Dict[str, Any] = {"ignore_missing": ignore_missing}
            if selected_step_ids is not None:
                kwargs["selected_step_ids"] = selected_step_ids

            with open_session_history() as history_mgr:
                restored_count = history_mgr.rollback(session_id, **kwargs)

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
