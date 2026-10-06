"""TUI Modal Views for Clinical Compliance Plugin."""

import os
from pathlib import Path
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Log

from app.ui.tui import A11yMixin


class CROForensicModal(A11yMixin, ModalScreen[None]):
    """Modal dialog for running CRO multi-study forensic ingestion worker."""

    BINDINGS = [
        Binding("escape", "close", "Close dialog", show=True),
    ]

    CSS = """
    CROForensicModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $success;
        width: 90%;
        max-width: 80;
        min-width: 30;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .modal-title {
        text-style: bold;
        color: $success;
        margin-bottom: 1;
    }
    .log-area {
        height: 10;
        border: solid $accent;
        margin-top: 1;
        margin-bottom: 1;
    }
    .button-row {
        align: right middle;
        height: 3;
        margin-top: 1;
    }
    .button-row Button {
        margin-left: 1;
    }
    """

    def __init__(self, settings: Any = None, base_dir: str = ""):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label(
                "CRO Multi-Study Forensic Ingestion [Ctrl+C]", classes="modal-title"
            )
            yield Label("Source Storage Drive / Archive Root:")
            inp_src = Input(
                value=self.base_dir,
                placeholder="Select source drive to scan...",
                id="input-source",
            )
            inp_src.tooltip = "Source storage drive path or archive directory root"
            yield inp_src

            yield Label("Target Audit Output Folder:")
            default_target = (
                (Path(self.base_dir) / "CRO_Audit_Output").as_posix()
                if self.base_dir
                else ""
            )
            inp_tgt = Input(
                value=default_target,
                placeholder="Select target output folder...",
                id="input-target",
            )
            inp_tgt.tooltip = (
                "Target output directory path for CRO forensic audit files"
            )
            yield inp_tgt

            log_w = Log(classes="log-area", id="log-widget")
            log_w.tooltip = (
                "Live execution log output for CRO forensic ingestion worker"
            )
            yield log_w

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close CRO forensic ingestion modal dialog"
                yield btn_close
                btn_run = Button("Run Forensic Ingest", id="btn-run", variant="success")
                btn_run.tooltip = (
                    "Trigger CRO multi-study forensic ingestion worker execution"
                )
                yield btn_run

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        self.set_class(width < 80, "narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        w = event.size.width
        self._update_layout(w)

    def on_mount(self) -> None:
        """Focus input field on mount and emit announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-source", Input).focus()
        self.announce("Opened CRO multi-study forensic ingestion dialog.")

    @on(Button.Pressed, "#btn-run")
    def action_run(self) -> None:
        """Trigger forensic worker execution."""
        self.announce("Started CRO forensic multi-study ingestion worker execution.")
        self.run_forensic_worker()

    @work(exclusive=True, thread=True)
    def run_forensic_worker(self) -> None:
        """Run CRO multi-study forensic pipeline in worker thread."""
        log_w = self.query_one("#log-widget", Log)
        src = self.query_one("#input-source", Input).value.strip()
        tgt = self.query_one("#input-target", Input).value.strip()

        if not src or not os.path.exists(src):
            log_w.write_line("Error: Source directory does not exist.")
            err_msg = "Forensic scan error: Source directory does not exist."
            if self.app:
                self.app.call_from_thread(self.announce, err_msg)
            else:
                self.announce(err_msg)
            return

        log_w.write_line(f"Starting CRO Forensic Ingestion on: {src}")
        try:
            from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
                CROMultiStudyPipeline,
            )

            pipeline = CROMultiStudyPipeline(
                mode="tmf",
                smart_renaming=getattr(self.settings, "CLINICAL_SMART_RENAMING", True),
            )

            def progress_cb(pct: int, msg: str) -> None:
                log_w.write_line(f"[{pct}%] {msg}")

            result = pipeline.run_pipeline(
                source_root=src,
                target_root=tgt,
                progress_callback=progress_cb,
            )
            summary_msg = f"Completed successfully! Total scanned: {result.total_scanned_files}, Discovered studies: {result.discovered_studies_count}"
            log_w.write_line(summary_msg)
            if self.app:
                self.app.call_from_thread(self.announce, summary_msg)
            else:
                self.announce(summary_msg)
        except Exception as e:
            log_w.write_line(f"Execution error: {e}")
            err_msg = f"Forensic scan error: {e}"
            if self.app:
                self.app.call_from_thread(self.announce, err_msg)
            else:
                self.announce(err_msg)

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Dismiss forensic modal."""
        self.announce("Closed CRO forensic ingestion dialog.")
        self.dismiss(None)
