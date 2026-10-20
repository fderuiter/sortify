"""Quarantine vault dialog."""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.modals.edit import DirectorySelectModal

logger = logging.getLogger(__name__)


class QuarantineVaultModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[None]):
    """Interactive Modal Screen for Compliance Quarantine Vault & Audit Log Review."""

    BINDINGS = [
        Binding("escape", "cancel", "Close quarantine vault", show=True),
        Binding("r", "release_selected", "Release file", show=True),
        Binding("d", "dismiss_selected", "Dismiss item", show=True),
        Binding("i", "reinspect_selected", "Re-inspect item", show=True),
        Binding("ctrl+r", "refresh_records", "Refresh vault", show=True),
    ]

    CSS = """
    QuarantineVaultModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .quarantine-modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $warning;
        width: 95%;
        max-width: 110;
        min-width: 60;
        height: 90%;
        min-height: 22;
        layout: vertical;
    }
    .narrow .quarantine-modal-box {
        padding: 0 1;
        width: 98%;
    }
    .quarantine-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 0;
    }
    .quarantine-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .quarantine-filter-row {
        height: 3;
        margin-bottom: 1;
        layout: horizontal;
    }
    #quarantine-search-input {
        width: 60%;
        margin-right: 1;
    }
    #quarantine-status-filter {
        width: 38%;
    }
    .quarantine-content-split {
        height: 1fr;
        layout: horizontal;
    }
    .narrow .quarantine-content-split {
        layout: vertical;
    }
    #quarantine-table-pane {
        width: 55%;
        height: 100%;
        border: solid $primary;
        margin-right: 1;
    }
    .narrow #quarantine-table-pane {
        width: 100%;
        height: 50%;
        margin-right: 0;
        margin-bottom: 1;
    }
    #quarantine-detail-pane {
        width: 45%;
        height: 100%;
        border: solid $secondary;
        padding: 1;
        overflow-y: auto;
    }
    .narrow #quarantine-detail-pane {
        width: 100%;
        height: 50%;
    }
    .quarantine-detail-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .quarantine-detail-line {
        margin-bottom: 1;
    }
    .quarantine-audit-header {
        text-style: bold;
        color: $primary;
        margin-top: 1;
        margin-bottom: 1;
    }
    .quarantine-button-row {
        margin-top: 1;
        height: 3;
        layout: horizontal;
        align: right middle;
    }
    .quarantine-button-row Button {
        margin-left: 1;
    }
    Button:focus, Input:focus, Select:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        db: Optional[Any] = None,
        base_dir: str = "",
        policies: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__()
        self.db = db
        self.base_dir = base_dir
        self.policies = policies or []
        self.records: List[Dict[str, Any]] = []
        self.filtered_records: List[Dict[str, Any]] = []
        self.selected_job_id: Optional[str] = None

    def compose(self) -> ComposeResult:
        """Compose quarantine vault modal widgets."""
        with Vertical(classes="quarantine-modal-box"):
            yield Heading(
                "Quarantine Vault & Compliance Audit Review [Ctrl+U]",
                level=1,
                classes="quarantine-title",
                id="modal-title",
            )
            yield Label(
                "Inspect staged files, view structured audit logs, release, or re-inspect compliance items:",
                classes="quarantine-subtitle",
            )

            with Horizontal(classes="quarantine-filter-row"):
                inp = Input(
                    placeholder="Search by job ID, filename, policy reason...",
                    id="quarantine-search-input",
                )
                inp.tooltip = (
                    "Filter quarantine records by job ID, filename, or policy reason"
                )
                yield inp

                sel = Select(
                    [
                        ("ALL Statuses", "ALL"),
                        ("STAGED", "STAGED"),
                        ("QUARANTINED", "QUARANTINED"),
                        ("IN_INSPECTION", "IN_INSPECTION"),
                        ("RELEASED", "RELEASED"),
                        ("DISMISSED", "DISMISSED"),
                        ("DEAD_LETTER_QUEUE", "DEAD_LETTER_QUEUE"),
                    ],
                    value="ALL",
                    id="quarantine-status-filter",
                    allow_blank=False,
                )
                sel.tooltip = "Filter quarantine records by status"
                yield sel

            with Horizontal(classes="quarantine-content-split"):
                dt = DataTable(id="quarantine-table")
                dt.tooltip = "Table of staged quarantine compliance records"
                dt.cursor_type = "row"
                with Vertical(id="quarantine-table-pane"):
                    yield dt

                with Vertical(id="quarantine-detail-pane"):
                    lbl = Heading(
                        "Select an item to view audit log & details",
                        level=2,
                        classes="quarantine-detail-header",
                        id="quarantine-detail-title",
                    )
                    lbl.tooltip = "Audit log and metadata details panel"
                    yield lbl
                    st = Static(
                        "No quarantine item selected.", id="quarantine-detail-content"
                    )
                    st.tooltip = "Structured audit log timeline view"
                    yield st

            with Horizontal(classes="quarantine-button-row"):
                btn_rel = Button(
                    "Release (r)", id="btn-quarantine-release", variant="success"
                )
                btn_rel.tooltip = (
                    "Release selected quarantined item to destination directory"
                )
                yield btn_rel

                btn_dis = Button(
                    "Dismiss (d)", id="btn-quarantine-dismiss", variant="warning"
                )
                btn_dis.tooltip = (
                    "Dismiss selected quarantine item without relocating file"
                )
                yield btn_dis

                btn_insp = Button(
                    "Re-inspect (i)", id="btn-quarantine-reinspect", variant="primary"
                )
                btn_insp.tooltip = (
                    "Trigger background forensic scan and policy evaluation"
                )
                yield btn_insp

                btn_ref = Button("Refresh", id="btn-quarantine-refresh")
                btn_ref.tooltip = "Refresh quarantine vault records from database"
                yield btn_ref

                btn_close = Button("Close (Esc)", id="btn-quarantine-close")
                btn_close.tooltip = "Close quarantine vault dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Initialize data table columns and trigger initial record fetch on mount."""
        self._update_layout(self.size.width)
        dt = self.query_one("#quarantine-table", DataTable)
        dt.add_columns("Job ID", "Filename", "Status", "Policy Action", "Created")
        self.announce("Opened Quarantine Vault modal.")
        self.action_refresh_records()

    def action_cancel(self) -> None:
        """Close quarantine vault modal on Escape key press."""
        self.dismiss(None)

    @on(Button.Pressed, "#btn-quarantine-close")
    def action_close_button(self) -> None:
        """Close quarantine vault modal via close button."""
        self.dismiss(None)

    def _get_db(self) -> Any:
        if self.db is not None:
            return self.db
        from app.core.db_worker import DBWorker
        from app.core.store import open_store

        return open_store().database(DBWorker())

    def _call_ui(self, func, *args, **kwargs) -> None:
        try:
            app_obj = self.app
        except Exception:
            app_obj = None

        if app_obj and hasattr(app_obj, "call_from_thread"):
            try:
                app_obj.call_from_thread(func, *args, **kwargs)
                return
            except Exception:
                pass
        try:
            func(*args, **kwargs)
        except Exception:
            pass

    @on(Button.Pressed, "#btn-quarantine-refresh")
    def action_refresh_records(self) -> None:
        """Refresh quarantine records from database."""
        self.fetch_records_async()

    @work(thread=True)
    def fetch_records_async(self) -> None:
        """Fetch quarantine records asynchronously on background worker thread."""
        try:
            db_inst = self._get_db()

            if self.base_dir:
                recs = db_inst.get_quarantine_records_by_base_dir(self.base_dir)
            else:
                recs = db_inst.get_all_quarantine_records()
            self._call_ui(self._on_records_loaded, recs)
        except Exception as e:
            logger.exception(f"Failed to fetch quarantine records: {e}")
            self._call_ui(self._on_records_loaded, [])
            self._call_ui(self.announce, f"Error loading quarantine records: {e}")

    def _on_records_loaded(self, records: List[Dict[str, Any]]) -> None:
        self.records = records
        self._apply_filters()
        self.announce(f"Loaded {len(records)} quarantine record(s).")

    @on(Input.Changed, "#quarantine-search-input")
    @on(Select.Changed, "#quarantine-status-filter")
    def _on_filter_changed(self) -> None:
        self._apply_filters()

    def _apply_filters(self) -> None:
        search_term = ""
        status_term = "ALL"
        try:
            search_term = (
                self.query_one("#quarantine-search-input", Input).value.strip().lower()
            )
            status_term = str(
                self.query_one("#quarantine-status-filter", Select).value or "ALL"
            )
        except Exception:
            pass

        filtered = []
        for r in self.records:
            st = r.get("status", "")
            if status_term != "ALL" and st != status_term:
                continue

            if search_term:
                job_id = (r.get("job_id") or "").lower()
                orig_file = (r.get("original_filepath") or "").lower()
                staged_file = (r.get("staged_filepath") or "").lower()
                action = (r.get("policy_action") or "").lower()
                if (
                    search_term not in job_id
                    and search_term not in orig_file
                    and search_term not in staged_file
                    and search_term not in action
                    and search_term not in st.lower()
                ):
                    continue
            filtered.append(r)

        self.filtered_records = filtered
        dt = self.query_one("#quarantine-table", DataTable)
        dt.clear()

        for r in filtered:
            job_id = r.get("job_id", "")
            orig_path = r.get("original_filepath", "")
            fname = os.path.basename(orig_path) if orig_path else job_id
            st = r.get("status", "")
            act = r.get("policy_action") or "-"
            created_ts = r.get("created_at", 0)
            created_str = (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(created_ts))
                if created_ts
                else "-"
            )
            dt.add_row(job_id, fname, st, act, created_str, key=job_id)

        if filtered:
            if not self.selected_job_id or not any(
                x.get("job_id") == self.selected_job_id for x in filtered
            ):
                self.selected_job_id = filtered[0].get("job_id")
            self._render_detail_pane(self.selected_job_id)
        else:
            self.selected_job_id = None
            self._render_detail_pane(None)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.selected_job_id = event.row_key.value
            self._render_detail_pane(self.selected_job_id)

    @on(DataTable.CellHighlighted)
    def _on_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        if event.coordinate and (dt := self.query_one("#quarantine-table", DataTable)):
            try:
                row_key = dt.coordinate_to_cell_key(event.coordinate).row_key
                if row_key and row_key.value:
                    self.selected_job_id = row_key.value
                    self._render_detail_pane(self.selected_job_id)
            except Exception:
                pass

    def _get_selected_record(self, job_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not job_id:
            return None
        for r in self.records:
            if r.get("job_id") == job_id:
                return r
        return None

    def _render_detail_pane(self, job_id: Optional[str]) -> None:
        try:
            title_lbl = self.query_one("#quarantine-detail-title", Label)
            content_st = self.query_one("#quarantine-detail-content", Static)
        except Exception:
            return

        record = self._get_selected_record(job_id)
        if not record:
            title_lbl.update("No item selected")
            content_st.update(
                "Select a row in the quarantine table to view its detailed structured audit log."
            )
            return

        title_lbl.update(f"Job Details: {record.get('job_id')}")

        lines = [
            f"[bold]Original Path:[/bold] {record.get('original_filepath', '-')}",
            f"[bold]Staged Path:[/bold] {record.get('staged_filepath', '-')}",
            f"[bold]Status:[/bold] {record.get('status', '-')}",
            f"[bold]Policy Action:[/bold] {record.get('policy_action') or '-'}",
            f"[bold]File Hash:[/bold] {record.get('file_hash') or '-'}",
            f"[bold]Base Directory:[/bold] {record.get('base_dir', '-')}",
        ]
        if err := record.get("error_message"):
            lines.append(f"[bold red]Error:[/bold red] {err}")

        lines.append("")
        lines.append("[bold cyan]Structured Audit Log Timeline:[/bold cyan]")
        audit_log = record.get("audit_log", [])
        if audit_log:
            for entry in audit_log:
                if isinstance(entry, dict):
                    ts = entry.get("timestamp", 0)
                    ts_str = (
                        time.strftime("%H:%M:%S", time.localtime(ts))
                        if ts
                        else "--:--:--"
                    )
                    st = entry.get("status", "")
                    det = entry.get("details", "")
                    lines.append(f"  [dim]{ts_str}[/dim] [bold]{st}[/bold]: {det}")
                else:
                    lines.append(f"  • {entry}")
        else:
            lines.append("  (No audit log entries recorded)")

        content_st.update("\n".join(lines))

    @on(Button.Pressed, "#btn-quarantine-release")
    def action_release_selected(self) -> None:
        """Prompt destination directory and release selected quarantine item."""
        record = self._get_selected_record(self.selected_job_id)
        if not record:
            self.announce("No quarantine item selected for release.")
            return

        if record.get("status") == "RELEASED":
            self.announce(
                f"Quarantine item '{record.get('job_id')}' is already released."
            )
            return

        staged_path = record.get("staged_filepath")
        if not staged_path or not os.path.exists(staged_path):
            self.announce(
                f"Cannot release item '{record.get('job_id')}': Staged file missing at {staged_path}."
            )
            return

        orig_rel = record.get("original_filepath", "released_file")
        base_dir = record.get("base_dir") or self.base_dir or "."

        def _dir_selected_callback(target_dir: Optional[str]) -> None:
            if not target_dir:
                self.announce("Release operation cancelled.")
                return
            if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
                from app.ui.notifications import notify

                err_msg = f"Target directory does not exist: {target_dir}"
                self.announce(err_msg, priority="assertive")
                notify(err_msg, type="error")
                return
            dest_file_path = os.path.join(target_dir, os.path.basename(orig_rel))
            self.execute_release_async(record, dest_file_path)

        self.app.push_screen(DirectorySelectModal(base_dir), _dir_selected_callback)

    @work(thread=True)
    def execute_release_async(self, record: dict, dest_file_path: str) -> None:
        """Asynchronously perform atomic quarantine relocation to destination path."""
        job_id = record["job_id"]
        staged_path = record["staged_filepath"]
        try:
            os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
            from app.core.resilient_file_ops import atomic_quarantine_relocation

            with atomic_quarantine_relocation(staged_path, dest_file_path):
                db_inst = self._get_db()
                db_inst.update_quarantine_status(
                    job_id=job_id,
                    status="RELEASED",
                    audit_entry={
                        "timestamp": time.time(),
                        "status": "RELEASED",
                        "details": f"Released from quarantine modal to {dest_file_path}",
                    },
                )
            self._call_ui(self._on_release_success, job_id, dest_file_path)
        except Exception as e:
            logger.error(f"Failed to release quarantine job {job_id}: {e}")
            self._call_ui(self.announce, f"Release failed for {job_id}: {e}")

    def _on_release_success(self, job_id: str, dest_file_path: str) -> None:
        self.announce(
            f"Quarantine item '{job_id}' released successfully to {dest_file_path}."
        )
        self.fetch_records_async()

    @on(Button.Pressed, "#btn-quarantine-dismiss")
    def action_dismiss_selected(self) -> None:
        """Dismiss the selected quarantine item without restoring file."""
        record = self._get_selected_record(self.selected_job_id)
        if not record:
            self.announce("No quarantine item selected for dismissal.")
            return

        if record.get("status") == "DISMISSED":
            self.announce(
                f"Quarantine item '{record.get('job_id')}' is already dismissed."
            )
            return

        self.execute_dismiss_async(record["job_id"])

    @work(thread=True)
    def execute_dismiss_async(self, job_id: str) -> None:
        """Asynchronously mark quarantine item as dismissed in database."""
        try:
            db_inst = self._get_db()
            db_inst.update_quarantine_status(
                job_id=job_id,
                status="DISMISSED",
                audit_entry={
                    "timestamp": time.time(),
                    "status": "DISMISSED",
                    "details": "Dismissed manually via quarantine vault modal review",
                },
            )
            self._call_ui(self._on_dismiss_success, job_id)
        except Exception as e:
            logger.error(f"Failed to dismiss quarantine job {job_id}: {e}")
            self._call_ui(self.announce, f"Dismiss failed for {job_id}: {e}")

    def _on_dismiss_success(self, job_id: str) -> None:
        self.announce(f"Quarantine item '{job_id}' marked as DISMISSED.")
        self.fetch_records_async()

    @on(Button.Pressed, "#btn-quarantine-reinspect")
    def action_reinspect_selected(self) -> None:
        """Trigger background forensic re-inspection scan for selected item."""
        record = self._get_selected_record(self.selected_job_id)
        if not record:
            self.announce("No quarantine item selected for re-inspection.")
            return

        job_id = record["job_id"]
        self.announce(
            f"Initiating background forensic re-inspection for item '{job_id}'..."
        )
        self.execute_reinspect_async(job_id)

    @work(thread=True)
    def execute_reinspect_async(self, job_id: str) -> None:
        """Asynchronously execute policy re-inspection on worker thread."""
        try:
            db_inst = self._get_db()

            from app.core.quarantine_interceptor import QuarantineInterceptorService

            service = QuarantineInterceptorService(db=db_inst, policies=self.policies)
            res = service.process_quarantine_job(job_id)
            self._call_ui(self._on_reinspect_success, job_id, res)
        except Exception as e:
            logger.error(f"Failed to re-inspect quarantine job {job_id}: {e}")
            self._call_ui(self.announce, f"Re-inspection failed for {job_id}: {e}")

    def _on_reinspect_success(self, job_id: str, res: dict) -> None:
        new_st = res.get("status", "UPDATED")
        self.announce(
            f"Re-inspection completed for '{job_id}'. Resulting status: {new_st}."
        )
        self.fetch_records_async()
