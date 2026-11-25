"""Report dialogs: extraction preview, report export and the audit report."""

import logging
import os
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
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

from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen
from app.ui.widgets.density_slider import DensitySlider

logger = logging.getLogger(__name__)


class ExtractionPreviewModal(ResponsiveModalScreen[int | None]):
    """Modal dialog for inspecting sanitized document text and PDF page extraction density."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .preview-modal-box {
        max-width: 84;
        min-width: 35;
        max-height: 92%;
    }
    .hidden {
        display: none;
    }
    .file-path-label {
        color: $text-muted;
        margin-bottom: 1;
    }
    .field-label {
        margin-top: 1;
        text-style: bold;
    }
    .preview-table {
        height: auto;
        min-height: 6;
        max-height: 10;
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
    .preview-text-box {
        width: 100%;
        height: auto;
        min-height: 3;
        max-height: 10;
        margin-top: 0;
        padding: 0 1;
        border: solid $accent;
        color: $text;
    }
    ExtractionPreviewModal {
        &.compact .preview-modal-box, &.narrow .preview-modal-box {
            padding: 0 1;
            width: 96%;
        }
        &.compact .modal-title {
            margin-bottom: 0;
        }
        &.compact .file-path-label {
            margin-bottom: 0;
        }
        &.compact .field-label {
            margin-top: 0;
        }
        &.compact .preview-table {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .threshold-row {
            height: 1;
        }
        &.compact .narrow-input {
            height: 1;
            border: none;
        }
        &.compact .preview-text-box {
            min-height: 2;
            max-height: 4;
        }
        &.compact .button-row {
            margin-top: 0;
        }
        &.compact .button-row Button {
            height: 1;
            min-height: 1;
            border: none;
            margin: 0;
        }
    }
    """

    MAX_PREVIEW_CHARS: int = 8000

    def __init__(
        self,
        file_path: str,
        settings: Any,
        db: Any = None,
        base_dir: str | None = None,
    ) -> None:
        super().__init__()
        self.file_path = file_path
        self.settings = settings
        self.db = db
        self.base_dir = base_dir
        self.is_pdf = str(file_path).lower().endswith(".pdf")
        self.initial_threshold = getattr(settings, "PDF_TEXT_DENSITY_THRESHOLD", 50)
        self.current_threshold = self.initial_threshold
        self.page_data: list[dict] = []
        self.sanitized_text: str = ""
        self.text_source: str = "missing"

    def compose(self) -> ComposeResult:
        """Compose modal dialog children widgets."""
        heading_title = (
            "PDF Extraction Strategy Preview"
            if self.is_pdf
            else "Document Extraction Preview"
        )
        pdf_section_cls = "" if self.is_pdf else "hidden"
        with ModalBox(classes="preview-modal-box modal-box"):
            yield Heading(
                heading_title,
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    f"File: {os.path.basename(self.file_path)}",
                    classes="file-path-label",
                )

                with Vertical(id="pdf-density-section", classes=pdf_section_cls):
                    yield Label(
                        "Density Threshold (chars/page):", classes="field-label"
                    )
                    with Horizontal(classes="threshold-row"):
                        thresh_inp = Input(
                            value=str(self.current_threshold),
                            placeholder="Density Threshold (chars/page)",
                            name="Density Threshold",
                            id="preview-input-threshold",
                            classes="narrow-input",
                        )
                        thresh_inp.tooltip = (
                            "Adjust PDF text density threshold in characters per page"
                        )
                        setattr(thresh_inp, "label", "Density Threshold (chars/page)")
                        yield thresh_inp
                        slider = DensitySlider(
                            value=self.current_threshold,
                            min_value=0,
                            max_value=500,
                            step=5,
                            announce_formatter=self._format_slider_announcement,
                            id="preview-slider-threshold",
                        )
                        slider.tooltip = "Adjust PDF text density threshold slider"
                        yield slider

                    yield DataTable(id="preview-table", classes="preview-table")
                    yield Label("", id="preview-summary-label")

                yield Label(
                    "Sanitized Extracted Text:",
                    classes="field-label",
                    id="preview-text-heading",
                )
                yield Static(
                    "",
                    id="preview-extracted-text",
                    classes="preview-text-box",
                    markup=False,
                )

            with Horizontal(classes="button-row"):
                cancel_label = "Cancel" if self.is_pdf else "Close"
                btn_cancel = Button(
                    cancel_label, id="btn-preview-cancel", variant="default"
                )
                btn_cancel.tooltip = "Close preview dialog without saving settings"
                yield btn_cancel
                if self.is_pdf:
                    btn_save = Button(
                        "Save & Apply", id="btn-preview-save", variant="primary"
                    )
                    btn_save.tooltip = "Save density threshold to settings and apply"
                    yield btn_save

    def _load_sanitized_text(self) -> str:
        """Read and scrub extracted document text without network calls."""
        from app.core.extractor import _read_preview_text_offline
        from app.core.path_utils import scrub_user_home_paths
        from app.core.text_utils import sanitize_secret_patterns

        eff_db = self.db if self.db is not None else getattr(self.app, "db", None)
        eff_base = (
            self.base_dir
            if self.base_dir is not None
            else getattr(self.app, "base_dir", None)
        )

        raw_text, source_kind = _read_preview_text_offline(
            self.file_path,
            settings=self.settings,
            db=eff_db,
            base_dir=eff_base,
        )
        self.text_source = source_kind

        if not raw_text or not raw_text.strip():
            return "(No extractable text found in document.)"

        scrubbed = sanitize_secret_patterns(
            scrub_user_home_paths(raw_text),
            replacement="[REDACTED]",
        ).strip()
        if not scrubbed:
            return "(No extractable text found in document.)"

        if len(scrubbed) > self.MAX_PREVIEW_CHARS:
            scrubbed = scrubbed[: self.MAX_PREVIEW_CHARS] + "\n... [Preview truncated]"
        return scrubbed

    def on_mount(self) -> None:
        """Handle modal mount event and populate initial preview data."""
        super().on_mount()
        table = self.query_one("#preview-table", DataTable)
        table.add_columns("Page", "Embedded Chars", "Vector Paths", "Predicted Status")

        if self.is_pdf:
            from app.core.extractor_strategies import PdfExtractor

            extractor = PdfExtractor()
            # Light structural analysis without EasyOCR model passes
            self.page_data = extractor.preview_pdf_density(
                self.file_path, density_threshold=self.current_threshold
            )
            self._update_table()

        self.sanitized_text = self._load_sanitized_text()
        try:
            text_widget = self.query_one("#preview-extracted-text", Static)
            text_widget.update(self.sanitized_text)
        except Exception:
            pass

        fname = os.path.basename(self.file_path)
        if self.is_pdf:
            self.announce(
                f"Opened PDF extraction preview for '{fname}' ({len(self.page_data)} page(s))."
            )
        else:
            self.announce(f"Opened document text preview for '{fname}'.")

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

    def _format_slider_announcement(self, val: int) -> str:
        """Format contextual screen reader announcement with page status totals."""
        std_count = sum(
            1
            for p in self.page_data
            if p.get("char_count", 0) >= val and not p.get("has_vector", False)
        )
        rast_count = len(self.page_data) - std_count
        return (
            f"Density threshold set to {val}: {len(self.page_data)} total pages "
            f"({std_count} standard, {rast_count} rasterized)"
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


class ExportReportModal(ResponsiveModalScreen[tuple[str, str] | None]):
    """Modal dialog for exporting dry-run simulation reports."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    ExportReportModal {
        &.compact .modal-box, &.narrow .modal-box {
            width: 95%;
        }
        &.compact .modal-subtitle {
            margin-bottom: 0;
        }
    }
    """

    def __init__(
        self,
        default_path: str = "./simulation_report.html",
        default_format: str = "html",
        *,
        title: str = "Export Simulation Report",
        format_options: list[tuple[str, str]] | None = None,
    ):
        super().__init__()
        self.default_path = default_path
        self.default_format = default_format
        self.modal_title_text = title
        self.format_options = format_options or [
            ("HTML Report (*.html)", "html"),
            ("JSON Data (*.json)", "json"),
        ]

    def compose(self) -> ComposeResult:
        """Compose export report modal widgets."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                self.modal_title_text,
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    "Select output file path and report format:",
                    classes="modal-subtitle",
                )
                inp = Input(
                    value=self.default_path,
                    placeholder=f"Enter output path (e.g. {self.default_path})...",
                    id="input-export-path",
                )
                inp.tooltip = "Enter external path for exported report file"
                yield inp

                fmt_select = Select(
                    self.format_options,
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
                btn_export.tooltip = "Export report to disk"
                yield btn_export

    def on_mount(self) -> None:
        """Focus input field on mount and emit accessibility announcement."""
        super().on_mount()
        self.query_one("#input-export-path", Input).focus()
        self.announce(f"Opened {self.modal_title_text.lower()} dialog.")

    @on(Select.Changed, "#select-export-format")
    def _on_format_changed(self, event: Select.Changed) -> None:
        """Update default file extension when user switches export format."""
        new_fmt = str(event.value or "").strip().lower()
        if not new_fmt:
            return
        for inp in self.query("#input-export-path").results(Input):
            curr = inp.value.strip()
            default_stem = str(Path(self.default_path).with_suffix(""))
            if curr and str(Path(curr).with_suffix("")) == default_stem:
                inp.value = f"{default_stem}.{new_fmt}"

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


class AuditReportModal(ResponsiveModalScreen[None]):
    """Interactive Modal Screen for Tabular Verification Tree & Multi-Format Audit Export."""

    BINDINGS = [
        Binding("escape", "cancel", "Close verification dialog", show=True),
        Binding("ctrl+r", "retry_selected", "Retry Selected Failed Item", show=True),
        Binding("ctrl+t", "retry_all", "Retry All Failed Items", show=True),
        Binding("ctrl+j", "export_json", "Export JSON Report", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .audit-modal-box {
        width: 95%;
        max-width: 108;
        min-width: 40;
        height: 90%;
        min-height: 20;
    }
    .modal-body {
        height: 1fr;
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
        width: 100%;
        height: auto;
        margin-bottom: 1;
        layout: horizontal;
    }
    .stat-card {
        width: 1fr;
        min-width: 0;
        padding: 0 1;
        margin-right: 1;
        border: solid $secondary;
        background: $surface;
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
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }
    #audit-search-input {
        width: 1fr;
        margin-right: 1;
    }
    #audit-status-filter {
        width: 26;
    }
    #audit-table, #failed-items-table {
        width: 100%;
        height: auto;
        min-height: 7;
        max-height: 16;
        margin-bottom: 1;
        border: solid $accent;
    }
    #audit-tabs {
        height: auto;
        min-height: 11;
        margin-bottom: 1;
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
    Button:focus, Input:focus, Select:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    AuditReportModal {
        background: rgba(0, 0, 0, 0.7);
        &.compact .audit-modal-box, &.narrow .audit-modal-box {
            padding: 0 1;
            width: 98%;
            height: 96%;
            max-height: 96%;
        }
        &.compact .audit-subtitle {
            margin-bottom: 0;
        }
        &.compact .audit-stats-row, &.narrow .audit-stats-row {
            margin-bottom: 0;
            layout: grid;
            grid-size: 3;
            grid-columns: 1fr 1fr 1fr;
            grid-gutter: 0 1;
        }
        &.compact .stat-card, &.narrow .stat-card {
            border: none;
            height: 1;
            margin-right: 0;
        }
        &.compact .filter-row {
            margin-bottom: 0;
        }
        &.narrow #audit-status-filter {
            width: 20;
        }
        &.compact #audit-table, &.compact #failed-items-table {
            margin-bottom: 0;
        }
        &.compact #audit-tabs {
            margin-bottom: 0;
        }
        &.compact .button-row {
            margin-top: 0;
        }
        &.narrow .button-row {
            grid-size: 2;
            grid-columns: 1fr 1fr;
        }
        &.compact .button-row Button {
            height: 1;
            min-height: 1;
            border: none;
        }
    }
    """

    def __init__(self, report_data: dict[str, Any], session: Any = None):
        super().__init__()
        self.report_data = report_data or {}
        self.session = session

    def compose(self) -> ComposeResult:
        """Compose verification audit report modal widgets."""
        with ModalBox(classes="audit-modal-box modal-box"):
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

            with Vertical(classes="modal-body"):
                with Horizontal(classes="audit-stats-row"):
                    yield Static(f"Total: {total}", classes="stat-card")
                    yield Static(f"[PASS]: {pass_cnt}", classes="stat-card stat-pass")
                    yield Static(f"[FAIL]: {fail_cnt}", classes="stat-card stat-fail")
                    yield Static(
                        f"[SKIPPED]: {skip_cnt}", classes="stat-card stat-skipped"
                    )
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
                            inp.tooltip = (
                                "Filter verification results by filename or path"
                            )
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
                btn_json.tooltip = "Export audit report to an external JSON file"
                yield btn_json

                btn_csv = Button("Export CSV", id="btn-export-csv", variant="success")
                btn_csv.tooltip = "Export audit report to an external CSV file"
                yield btn_csv

                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close verification audit report dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Initialize table rows, accessibility announcement, and initial focus."""
        super().on_mount()
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

    def _get_failed_records(self) -> list[dict[str, Any]]:
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

    def _execute_retry(self, items_to_retry: list[dict[str, Any]]) -> None:
        """Execute mover retry for selected failed items via AppSession and refresh UI."""
        from app.config import AppSettings
        from app.core.audit_reporter import generate_audit_report
        from app.core.session import AppSession

        base_dir = self.report_data.get("base_dir", "")
        session_id = self.report_data.get("session_id")
        app_ref: Any = getattr(self, "app", None)

        active_session: Any = None
        for cand in (self.session, getattr(app_ref, "app_session", None)):
            if (
                cand is not None
                and not getattr(cand, "_closed", False)
                and callable(getattr(cand, "retry_failed_moves", None))
            ):
                active_session = cand
                break

        effective_base = base_dir or getattr(app_ref, "base_dir", None) or "."
        if active_session is not None:
            retry_res = active_session.retry_failed_moves(
                items_to_retry, session_id=session_id
            )
        else:
            settings = getattr(app_ref, "settings", None) or AppSettings()
            temp_session = AppSession(settings, effective_base)
            try:
                retry_res = temp_session.retry_failed_moves(
                    items_to_retry, session_id=session_id
                )
            finally:
                temp_session.close()

        new_audit = retry_res.get("audit_report", {})
        effective_session_id = (
            retry_res.get("session_id") or new_audit.get("session_id") or session_id
        )

        retried_map: dict[str, dict[str, Any]] = {}
        for r in new_audit.get("records", []):
            raw_src = r.get("source_path")
            if isinstance(raw_src, str) and raw_src:
                retried_map[raw_src] = r
                norm_src = os.path.normcase(
                    os.path.normpath(
                        os.path.abspath(
                            os.path.join(base_dir, raw_src)
                            if (base_dir and not os.path.isabs(raw_src))
                            else raw_src
                        )
                    )
                )
                retried_map[norm_src] = r

        records = list(self.report_data.get("records", []))
        for idx, rec in enumerate(records):
            src = rec.get("source_path")
            if isinstance(src, str) and src:
                if src in retried_map:
                    records[idx] = retried_map[src]
                    continue
                norm_rec_src = os.path.normcase(
                    os.path.normpath(
                        os.path.abspath(
                            os.path.join(base_dir, src)
                            if (base_dir and not os.path.isabs(src))
                            else src
                        )
                    )
                )
                if norm_rec_src in retried_map:
                    records[idx] = retried_map[norm_rec_src]

        self.report_data = generate_audit_report(
            base_dir, records, session_id=effective_session_id
        )

        self._populate_table()
        self._update_stats_cards()

        if (
            int(new_audit.get("verified_pass_count") or 0) > 0
            and app_ref is not None
            and getattr(app_ref, "source_dirs", None)
            and callable(getattr(app_ref, "_start_scan", None))
        ):
            try:
                app_ref._start_scan()
            except Exception:
                pass

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
            cards = list(self.query(".stat-card").results(Static))
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

    def _perform_export_report(self, out_path: str, fmt: str) -> None:
        """Write audit report to ``out_path`` while guarding against sorted-folder writes."""
        base_dir = self.report_data.get("base_dir") or getattr(
            getattr(self, "app", None), "base_dir", None
        )
        effective_base = str(base_dir) if base_dir else None

        try:
            from app.core.audit_reporter import export_audit_report

            final_path = export_audit_report(
                self.report_data,
                out_path,
                format_type=fmt,
                base_dir=effective_base,
            )
            msg = f"Audit report successfully exported to {final_path}"
            self.announce(msg)
            self.notify(msg, severity="information")
        except Exception as err:
            err_msg = f"Failed to export audit report: {err}"
            logger.error(err_msg)
            self.announce(err_msg)
            self.notify(err_msg, severity="error")

    def _export_report(self, fmt: str) -> None:
        """Prompt for an external export path defaulting to ``<app_dir>/exports``."""
        from app.config import get_app_dir

        clean_fmt = (fmt or "json").strip().lower()
        default_out = str(Path(get_app_dir()) / "exports" / f"audit_report.{clean_fmt}")

        def handle_export_prompt(result: tuple[str, str] | None) -> None:
            if not result:
                return
            chosen_path, chosen_fmt = result
            self._perform_export_report(chosen_path, chosen_fmt or clean_fmt)

        app_ref = getattr(self, "app", None)
        if app_ref is not None and hasattr(app_ref, "push_screen"):
            app_ref.push_screen(
                ExportReportModal(
                    default_path=default_out,
                    default_format=clean_fmt,
                    title="Export Audit Verification Report",
                    format_options=[
                        ("JSON Report (*.json)", "json"),
                        ("CSV Report (*.csv)", "csv"),
                    ],
                ),
                handle_export_prompt,
            )
        else:
            self._perform_export_report(default_out, clean_fmt)

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
