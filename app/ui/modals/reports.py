"""Report dialogs: extraction preview, report export and the audit report."""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.widgets.density_slider import DensitySlider

logger = logging.getLogger(__name__)


class ExtractionPreviewModal(A11yMixin, ModalScreen[Optional[int]]):
    """Modal dialog for interactive visual inspection of PDF page extraction decisions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ExtractionPreviewModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .preview-modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 84;
        min-width: 40;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .file-path-label {
        color: $text-muted;
        margin-bottom: 1;
    }
    .preview-table {
        height: 10;
        margin-top: 1;
        margin-bottom: 1;
        border: solid $primary-lighten-2;
    }
    .threshold-row {
        height: 3;
        align: left middle;
    }
    .narrow-input {
        width: 12;
        margin-right: 1;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    """

    def __init__(self, file_path: str, settings: Any) -> None:
        super().__init__()
        self.file_path = file_path
        self.settings = settings
        self.initial_threshold = getattr(settings, "PDF_TEXT_DENSITY_THRESHOLD", 50)
        self.current_threshold = self.initial_threshold
        self.page_data: list[dict] = []

    def compose(self) -> ComposeResult:
        """Compose modal dialog children widgets."""
        with Vertical(classes="preview-modal-box"):
            yield Heading(
                "PDF Extraction Strategy Preview",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(
                f"File: {os.path.basename(self.file_path)}",
                classes="file-path-label",
            )

            yield Label("Density Threshold (chars/page):", classes="field-label")
            with Horizontal(classes="threshold-row"):
                yield Input(
                    value=str(self.current_threshold),
                    id="preview-input-threshold",
                    classes="narrow-input",
                )
                yield DensitySlider(
                    value=self.current_threshold,
                    min_value=0,
                    max_value=500,
                    step=5,
                    id="preview-slider-threshold",
                )

            yield DataTable(id="preview-table", classes="preview-table")
            yield Label("", id="preview-summary-label")

            with Horizontal(classes="button-row"):
                btn_cancel = Button(
                    "Cancel", id="btn-preview-cancel", variant="default"
                )
                btn_cancel.tooltip = "Close preview dialog without saving settings"
                yield btn_cancel
                btn_save = Button(
                    "Save & Apply", id="btn-preview-save", variant="primary"
                )
                btn_save.tooltip = "Save density threshold to settings and apply"
                yield btn_save

    def on_mount(self) -> None:
        """Handle modal mount event and populate initial preview data."""
        self.announce("Opened PDF extraction strategy preview modal.")
        table = self.query_one("#preview-table", DataTable)
        table.add_columns("Page", "Embedded Chars", "Vector Paths", "Predicted Status")

        from app.core.extractor_strategies import PdfExtractor

        extractor = PdfExtractor()
        # Light structural analysis without EasyOCR model passes
        self.page_data = extractor.preview_pdf_density(
            self.file_path, density_threshold=self.current_threshold
        )
        self._update_table()

    def _update_table(self) -> None:
        """Re-evaluate page extraction decisions and update table display."""
        table = self.query_one("#preview-table", DataTable)
        table.clear()
        std_count = 0
        rast_count = 0

        for page in self.page_data:
            char_count = page["char_count"]
            has_vector = page["has_vector"]
            if char_count >= self.current_threshold and not has_vector:
                status_text = Text("STANDARD", style="bold green")
                std_count += 1
            else:
                status_text = Text("RASTERIZED", style="bold yellow")
                rast_count += 1

            vector_str = "Yes" if has_vector else "No"
            table.add_row(
                f"Page {page['page_index']}",
                str(char_count),
                vector_str,
                status_text,
            )

        summary_label = self.query_one("#preview-summary-label", Label)
        summary_label.update(
            f"Total Pages: {len(self.page_data)} | Standard: {std_count} | Rasterized (OCR): {rast_count}"
        )

    @on(DensitySlider.Changed, "#preview-slider-threshold")
    def _on_slider_changed(self, event: DensitySlider.Changed) -> None:
        """Handle slider value changes by updating input and re-evaluating preview table."""
        if self.current_threshold != event.value:
            self.current_threshold = event.value
            try:
                inp = self.query_one("#preview-input-threshold", Input)
                if inp.value != str(event.value):
                    inp.value = str(event.value)
            except Exception:
                pass
            self._update_table()

    @on(Input.Changed, "#preview-input-threshold")
    def _on_input_changed(self, event: Input.Changed) -> None:
        """Handle input text changes by updating slider and re-evaluating preview table."""
        try:
            val = int(event.value.strip())
            if val >= 0 and self.current_threshold != val:
                self.current_threshold = val
                try:
                    slider = self.query_one("#preview-slider-threshold", DensitySlider)
                    if (
                        slider.value != val
                        and slider.min_value <= val <= slider.max_value
                    ):
                        slider.value = val
                except Exception:
                    pass
                self._update_table()
        except Exception:
            pass

    @on(Button.Pressed, "#btn-preview-save")
    def action_save_threshold(self) -> None:
        """Save updated density threshold to settings and dismiss modal."""
        if hasattr(self.settings, "PDF_TEXT_DENSITY_THRESHOLD"):
            self.settings.PDF_TEXT_DENSITY_THRESHOLD = self.current_threshold
        if hasattr(self.settings, "_save"):
            self.settings._save()
        self.announce(f"Saved PDF text density threshold: {self.current_threshold}")
        self.dismiss(self.current_threshold)

    @on(Button.Pressed, "#btn-preview-cancel")
    def action_cancel(self) -> None:
        """Cancel preview dialog and dismiss modal."""
        self.announce("Cancelled PDF extraction preview.")
        self.dismiss(None)


class ExportReportModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Tuple[str, str]]]
):
    """Modal dialog for exporting dry-run simulation reports."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ExportReportModal {
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
        color: $accent;
        margin-bottom: 1;
    }
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
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
        default_path: str = "./simulation_report.html",
        default_format: str = "html",
    ):
        super().__init__()
        self.default_path = default_path
        self.default_format = default_format

    def compose(self) -> ComposeResult:
        """Compose export report modal widgets."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Export Simulation Report",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(
                "Select output file path and report format:",
                classes="modal-subtitle",
            )
            inp = Input(
                value=self.default_path,
                placeholder="Enter output path (e.g. ./report.html)...",
                id="input-export-path",
            )
            inp.tooltip = "Enter path for exported dry-run simulation report"
            yield inp

            fmt_select = Select(
                [("HTML Report (*.html)", "html"), ("JSON Data (*.json)", "json")],
                value=self.default_format,
                id="select-export-format",
                allow_blank=False,
            )
            yield fmt_select

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel report export action"
                yield btn_cancel
                btn_export = Button("Export Report", id="btn-export", variant="primary")
                btn_export.tooltip = "Export simulation report to disk"
                yield btn_export

    def on_mount(self) -> None:
        """Focus input field on mount and emit accessibility announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-export-path", Input).focus()
        self.announce("Opened export simulation report dialog.")

    @on(Input.Submitted, "#input-export-path")
    def action_submit_input(self) -> None:
        """Handle input submission event."""
        self.action_export()

    @on(Button.Pressed, "#btn-export")
    def action_export(self) -> None:
        """Confirm report export and dismiss modal with export parameters."""
        path_val = self.query_one("#input-export-path", Input).value.strip()
        if not path_val:
            self.announce("Output path cannot be empty.")
            return
        format_val = self.query_one("#select-export-format", Select).value or "html"
        self.dismiss((path_val, str(format_val)))

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel report export modal."""
        self.dismiss(None)


class AuditReportModal(A11yMixin, ModalScreen[None]):
    """Interactive Modal Screen for Tabular Verification Tree & Multi-Format Audit Export."""

    BINDINGS = [
        Binding("escape", "cancel", "Close verification dialog", show=True),
        Binding("ctrl+r", "retry_selected", "Retry Selected Failed Item", show=True),
        Binding("ctrl+t", "retry_all", "Retry All Failed Items", show=True),
        Binding("ctrl+j", "export_json", "Export JSON Report", show=True),
        Binding("ctrl+c", "export_csv", "Export CSV Report", show=True),
    ]

    CSS = """
    AuditReportModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .audit-modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 95%;
        max-width: 105;
        min-width: 50;
        height: 85%;
        min-height: 20;
    }
    .narrow .audit-modal-box {
        padding: 0 1;
        width: 98%;
    }
    .audit-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 0;
    }
    .audit-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .audit-stats-row {
        height: 3;
        margin-bottom: 1;
        align: center middle;
    }
    .stat-card {
        padding: 0 1;
        margin-right: 1;
        border: solid $secondary;
        background: $surface;
        min-width: 15;
    }
    .stat-pass {
        color: $success;
        border: solid $success;
    }
    .stat-fail {
        color: $error;
        border: solid $error;
    }
    .stat-skipped {
        color: $warning;
        border: solid $warning;
    }
    .stat-unverified {
        color: $text-muted;
        border: solid $secondary;
    }
    .filter-row {
        height: 3;
        margin-bottom: 1;
    }
    #audit-search-input {
        width: 60%;
        margin-right: 1;
    }
    #audit-status-filter {
        width: 38%;
    }
    #audit-table, #failed-items-table {
        height: 1fr;
        margin-bottom: 1;
        border: solid $accent;
    }
    #audit-tabs {
        height: 1fr;
        margin-bottom: 1;
    }
    .button-row {
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus, Select:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, report_data: Dict[str, Any]):
        super().__init__()
        self.report_data = report_data or {}

    def compose(self) -> ComposeResult:
        """Compose verification audit report modal widgets."""
        with Vertical(classes="audit-modal-box"):
            yield Heading(
                "Post-Sort File Integrity Verification Audit Report",
                level=1,
                classes="audit-title",
                id="modal-title",
            )
            base_dir_str = self.report_data.get("base_dir", "")
            yield Label(f"Target Directory: {base_dir_str}", classes="audit-subtitle")

            total = self.report_data.get("total_files", 0)
            pass_cnt = self.report_data.get("verified_pass_count", 0)
            fail_cnt = self.report_data.get("mismatch_fail_count", 0)
            skip_cnt = self.report_data.get("skipped_count", 0)
            unverified_cnt = self.report_data.get("unverified_count", 0)

            with Horizontal(classes="audit-stats-row"):
                yield Static(f"Total: {total}", classes="stat-card")
                yield Static(f"[PASS]: {pass_cnt}", classes="stat-card stat-pass")
                yield Static(f"[FAIL]: {fail_cnt}", classes="stat-card stat-fail")
                yield Static(f"[SKIPPED]: {skip_cnt}", classes="stat-card stat-skipped")
                yield Static(
                    f"[UNVERIFIED]: {unverified_cnt}",
                    classes="stat-card stat-unverified",
                )

            with TabbedContent(initial="tab-all-records", id="audit-tabs"):
                with TabPane("All Verification Records", id="tab-all-records"):
                    with Horizontal(classes="filter-row"):
                        inp = Input(
                            placeholder="Search by filename or path...",
                            id="audit-search-input",
                        )
                        inp.tooltip = "Filter verification results by filename or path"
                        yield inp

                        status_sel = Select(
                            [
                                ("All Statuses", "ALL"),
                                ("Verified Pass [PASS]", "VERIFIED_PASS"),
                                ("Mismatch Fail [FAIL]", "MISMATCH_FAIL"),
                                ("Skipped [SKIPPED]", "SKIPPED"),
                                ("Unverified [UNVERIFIED]", "UNVERIFIED"),
                            ],
                            value="ALL",
                            id="audit-status-filter",
                            allow_blank=False,
                        )
                        status_sel.tooltip = "Filter results by verification status"
                        yield status_sel

                    table = DataTable(id="audit-table")
                    table.cursor_type = "row"
                    table.tooltip = "Audit report tabular verification tree"
                    yield table

                with TabPane("Failed Items Queue", id="tab-failed-queue"):
                    failed_table = DataTable(id="failed-items-table")
                    failed_table.cursor_type = "row"
                    failed_table.tooltip = "Dedicated Failed Items queue table"
                    yield failed_table

            with Horizontal(classes="button-row"):
                btn_retry_sel = Button(
                    "Retry Selected", id="btn-retry-selected", variant="warning"
                )
                btn_retry_sel.tooltip = (
                    "Re-attempt file move operation for selected failed item"
                )
                yield btn_retry_sel

                btn_retry_all = Button("Retry All", id="btn-retry-all", variant="error")
                btn_retry_all.tooltip = (
                    "Re-attempt file move operations for all failed items in queue"
                )
                yield btn_retry_all

                btn_json = Button(
                    "Export JSON", id="btn-export-json", variant="primary"
                )
                btn_json.tooltip = (
                    "Export audit report to JSON file in target directory"
                )
                yield btn_json

                btn_csv = Button("Export CSV", id="btn-export-csv", variant="success")
                btn_csv.tooltip = "Export audit report to CSV file in target directory"
                yield btn_csv

                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close verification audit report dialog"
                yield btn_close

    def _update_layout(self, width: int, height: int = 24) -> None:
        """Update layout based on viewport width and height breakpoint."""
        if width < 80 or height < 24:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize event."""
        self._update_layout(event.size.width, event.size.height)

    def on_mount(self) -> None:
        """Initialize table rows, accessibility announcement, and initial focus."""
        self._update_layout(self.size.width, self.size.height)
        self._populate_table()
        total = self.report_data.get("total_files", 0)
        pass_cnt = self.report_data.get("verified_pass_count", 0)
        fail_cnt = self.report_data.get("mismatch_fail_count", 0)
        skip_cnt = self.report_data.get("skipped_count", 0)
        unverified_cnt = self.report_data.get("unverified_count", 0)
        self.query_one("#audit-search-input", Input).focus()
        self.announce(
            f"Opened Audit Verification Report dialog. {total} total files: "
            f"{pass_cnt} passed, {fail_cnt} failed, {skip_cnt} skipped, {unverified_cnt} unverified."
        )

    def _populate_table(
        self, search_query: str = "", status_filter: str = "ALL"
    ) -> None:
        """Populate DataTable with verification records matching filter criteria."""
        table = self.query_one("#audit-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Status",
            "Filename",
            "Pre-Sort SHA-256",
            "Post-Sort SHA-256",
            "Destination Path",
        )

        query = search_query.strip().lower()
        records = self.report_data.get("records", [])

        for rec in records:
            status = rec.get("status", "")
            filename = rec.get("filename", "")
            rel_dst = rec.get("relative_destination", "") or rec.get(
                "destination_path", ""
            )
            pre_h = rec.get("pre_hash", "") or ""
            post_h = rec.get("post_hash", "") or ""

            if status_filter != "ALL" and status != status_filter:
                continue

            if query:
                match_fn = query in filename.lower()
                match_src = query in rec.get("relative_source", "").lower()
                match_dst = query in rel_dst.lower()
                match_hash = query in pre_h.lower() or query in post_h.lower()
                if not (match_fn or match_src or match_dst or match_hash):
                    continue

            badge = rec.get("status_badge", f"[{status}]")
            pre_trunc = (pre_h[:12] + "...") if len(pre_h) > 12 else (pre_h or "N/A")
            post_trunc = (
                (post_h[:12] + "...") if len(post_h) > 12 else (post_h or "N/A")
            )

            table.add_row(badge, filename, pre_trunc, post_trunc, rel_dst)

        self._populate_failed_table()

    def _populate_failed_table(self) -> None:
        """Populate dedicated Failed Items queue table with error descriptions."""
        table = self.query_one("#failed-items-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Filename",
            "Status",
            "Error Description",
            "Source Path",
            "Destination Path",
        )

        failed_records = self._get_failed_records()

        for rec in failed_records:
            fn = rec.get("filename", "")
            st = rec.get("status_badge", rec.get("status", ""))
            err = rec.get("error") or rec.get("error_description") or "Operation failed"
            src = rec.get("relative_source") or rec.get("source_path", "")
            dst = rec.get("relative_destination") or rec.get("destination_path", "")
            table.add_row(fn, st, err, src, dst)

    def _get_failed_records(self) -> List[Dict[str, Any]]:
        """Get list of failed item records from report data."""
        failed = self.report_data.get("failed_items", [])
        if not failed:
            records = self.report_data.get("records", [])
            failed = [
                r
                for r in records
                if r.get("status") in ("MISMATCH_FAIL", "MOVE_FAIL") or r.get("error")
            ]
        return failed

    @on(Input.Changed, "#audit-search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        """Handle search input filter update."""
        status_val = self.query_one("#audit-status-filter", Select).value
        self._populate_table(search_query=event.value, status_filter=str(status_val))

    @on(Select.Changed, "#audit-status-filter")
    def on_status_changed(self, event: Select.Changed) -> None:
        """Handle status filter dropdown update."""
        search_val = self.query_one("#audit-search-input", Input).value
        self._populate_table(search_query=search_val, status_filter=str(event.value))

    def action_retry_selected(self) -> None:
        """Retry move operation for selected failed item."""
        failed_table = self.query_one("#failed-items-table", DataTable)
        selected_record = None

        if failed_table.cursor_row is not None and failed_table.row_count > 0:
            failed_records = self._get_failed_records()
            if 0 <= failed_table.cursor_row < len(failed_records):
                selected_record = failed_records[failed_table.cursor_row]

        if not selected_record:
            audit_table = self.query_one("#audit-table", DataTable)
            if audit_table.cursor_row is not None and audit_table.row_count > 0:
                records = self.report_data.get("records", [])
                if 0 <= audit_table.cursor_row < len(records):
                    rec = records[audit_table.cursor_row]
                    if rec.get("status") in ("MISMATCH_FAIL", "MOVE_FAIL") or rec.get(
                        "error"
                    ):
                        selected_record = rec

        if not selected_record:
            msg = "No failed item selected to retry."
            self.announce(msg)
            self.notify(msg, severity="warning")
            return

        self._execute_retry([selected_record])

    def action_retry_all(self) -> None:
        """Retry move operations for all failed items in queue."""
        failed_records = self._get_failed_records()
        if not failed_records:
            msg = "No failed items to retry."
            self.announce(msg)
            self.notify(msg, severity="warning")
            return

        self._execute_retry(failed_records)

    def _execute_retry(self, items_to_retry: List[Dict[str, Any]]) -> None:
        """Execute mover retry for selected failed items and refresh UI."""
        from app.core.mover import retry_failed_moves

        base_dir = self.report_data.get("base_dir", "")
        retry_res = retry_failed_moves(base_dir, items_to_retry)
        new_audit = retry_res.get("audit_report", {})

        retried_map = {r.get("source_path"): r for r in new_audit.get("records", [])}

        records = self.report_data.get("records", [])
        for idx, rec in enumerate(records):
            src = rec.get("source_path")
            if src in retried_map:
                records[idx] = retried_map[src]

        from app.core.audit_reporter import generate_audit_report

        self.report_data = generate_audit_report(base_dir, records)

        self._populate_table()
        self._update_stats_cards()

        pass_cnt = self.report_data.get("verified_pass_count", 0)
        fail_cnt = self.report_data.get("mismatch_fail_count", 0)
        msg = f"Retried {len(items_to_retry)} item(s). Overall status: {pass_cnt} passed, {fail_cnt} failed."
        self.announce(msg)
        self.notify(msg, severity="information")

    def _update_stats_cards(self) -> None:
        """Update stat card labels with current metrics."""
        total = self.report_data.get("total_files", 0)
        pass_cnt = self.report_data.get("verified_pass_count", 0)
        fail_cnt = self.report_data.get("mismatch_fail_count", 0)
        skip_cnt = self.report_data.get("skipped_count", 0)
        unverified_cnt = self.report_data.get("unverified_count", 0)

        try:
            cards = self.query(".stat-card")
            if len(cards) >= 5:
                cards[0].update(f"Total: {total}")
                cards[1].update(f"[PASS]: {pass_cnt}")
                cards[2].update(f"[FAIL]: {fail_cnt}")
                cards[3].update(f"[SKIPPED]: {skip_cnt}")
                cards[4].update(f"[UNVERIFIED]: {unverified_cnt}")
        except Exception:
            pass

    @on(Button.Pressed, "#btn-retry-selected")
    def on_btn_retry_selected(self) -> None:
        """Handle Retry Selected button press."""
        self.action_retry_selected()

    @on(Button.Pressed, "#btn-retry-all")
    def on_btn_retry_all(self) -> None:
        """Handle Retry All button press."""
        self.action_retry_all()

    def action_export_json(self) -> None:
        """Export audit report to JSON format."""
        self._export_report("json")

    def action_export_csv(self) -> None:
        """Export audit report to CSV format."""
        self._export_report("csv")

    def _export_report(self, fmt: str) -> None:
        """Export report to disk with error handling."""
        base_dir = self.report_data.get("base_dir", ".") or "."
        out_name = f"audit_report.{fmt}"
        out_path = os.path.join(base_dir, out_name)

        try:
            from app.core.audit_reporter import export_audit_report

            final_path = export_audit_report(
                self.report_data, out_path, format_type=fmt, base_dir=base_dir
            )
            msg = f"Audit report successfully exported to {final_path}"
            self.announce(msg)
            self.notify(msg, severity="information")
        except Exception as err:
            err_msg = f"Failed to export audit report: {err}"
            logger.error(err_msg)
            self.announce(err_msg)
            self.notify(err_msg, severity="error")

    def action_cancel(self) -> None:
        """Cancel dialog and dismiss modal."""
        self.announce("Closed verification report dialog.")
        self.dismiss()

    @on(Button.Pressed, "#btn-export-json")
    def on_btn_export_json(self) -> None:
        """Handle JSON export button press."""
        self.action_export_json()

    @on(Button.Pressed, "#btn-export-csv")
    def on_btn_export_csv(self) -> None:
        """Handle CSV export button press."""
        self.action_export_csv()

    @on(Button.Pressed, "#btn-close")
    def on_btn_close(self) -> None:
        """Handle Close button press."""
        self.action_cancel()
