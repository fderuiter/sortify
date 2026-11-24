"""TUI Modal Views for Clinical Compliance Plugin."""

import logging
import os
import threading
from pathlib import Path
from typing import Any

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Log, ProgressBar

from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen
from app.ui.notifications import TROUBLESHOOTING_URL

logger = logging.getLogger(__name__)


class CROForensicModal(ResponsiveModalScreen[None]):
    """Modal dialog for running CRO multi-study forensic ingestion worker."""

    HELP_URL = f"{TROUBLESHOOTING_URL}#cro-forensic-ingestion"

    BINDINGS = [
        Binding("escape", "close", "Close dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        border: thick $success;
    }
    .modal-title {
        color: $success;
    }
    ProgressBar {
        margin-top: 1;
        margin-bottom: 1;
        width: 100%;
    }
    .log-area {
        height: 8;
        min-height: 5;
        border: solid $accent;
        margin-top: 1;
        margin-bottom: 1;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    CROForensicModal {
        &.compact .modal-box, &.narrow .modal-box {
            padding: 0 1;
            width: 96%;
            max-height: 96%;
        }
        &.compact ProgressBar {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .log-area {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.narrow .button-row Button {
            min-width: 0;
            padding: 0 1;
        }
    }
    """

    def __init__(self, settings: Any = None, base_dir: str = ""):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir
        self.help_url = f"{TROUBLESHOOTING_URL}#cro-forensic-ingestion"
        self._cancel_event = threading.Event()

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "CRO Multi-Study Forensic Ingestion",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                yield Heading("Source Storage Drive / Archive Root:", level=2)
                inp_src = Input(
                    value=self.base_dir,
                    placeholder="Select source drive to scan...",
                    id="input-source",
                )
                inp_src.tooltip = "Source storage drive path or archive directory root"
                yield inp_src

                yield Heading("Target Audit Output Folder:", level=2)
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

                pb = ProgressBar(total=100, show_percentage=True, id="progress-bar")
                pb.tooltip = "Live execution progress for CRO forensic ingestion worker"
                yield pb

                log_w = Log(classes="log-area", id="log-widget")
                log_w.tooltip = (
                    "Live execution log output for CRO forensic ingestion worker"
                )
                yield log_w

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close CRO forensic ingestion modal dialog"
                yield btn_close
                btn_help = Button(
                    "Help", id="btn-help", variant="warning", classes="hidden"
                )
                btn_help.tooltip = (
                    "Open troubleshooting documentation for CRO forensic errors"
                )
                yield btn_help
                btn_run = Button("Run Forensic Ingest", id="btn-run", variant="success")
                btn_run.tooltip = (
                    "Trigger CRO multi-study forensic ingestion worker execution"
                )
                yield btn_run

    def _set_run_button_disabled(self, disabled: bool) -> None:
        """Set disabled status of the run button.

        Parameters
        ----------
        disabled : bool
            Whether the run button should be disabled.
        """
        try:
            btn = self.query_one("#btn-run", Button)
            btn.disabled = disabled
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Failed to update run button state: {e}")

    def cancel_worker(self) -> None:
        """Set cancellation token to halt background ingestion thread."""
        self._cancel_event.set()

    def on_unmount(self) -> None:
        """Ensure cancellation signal is emitted if modal unmounts during active execution."""
        self.cancel_worker()
        super().on_unmount()

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event.

        Parameters
        ----------
        event : events.Resize
            Resize event carrying new viewport dimensions.
        """
        super().on_resize(event)

    def on_mount(self) -> None:
        """Focus input field on mount and emit announcement."""
        super().on_mount()
        self.query_one("#input-source", Input).focus()
        self.announce("Opened CRO multi-study forensic ingestion dialog.")

    @on(Button.Pressed, "#btn-run")
    def action_run(self) -> None:
        """Trigger forensic worker execution."""
        try:
            if self.query_one("#btn-run", Button).disabled:
                return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Failed to check run button state: {e}")

        self._cancel_event.clear()
        self._set_run_button_disabled(True)
        self.announce("Started CRO forensic multi-study ingestion worker execution.")
        self.run_forensic_worker()

    @work(exclusive=True, thread=True)
    def run_forensic_worker(self) -> None:
        """Run CRO multi-study forensic pipeline in worker thread."""
        self._call_ui(self._set_run_button_disabled, True)
        log_w = self.query_one("#log-widget", Log)
        src = self.query_one("#input-source", Input).value.strip()
        tgt = self.query_one("#input-target", Input).value.strip()
        help_url = f"{TROUBLESHOOTING_URL}#cro-forensic-ingestion"

        try:
            if not src or not os.path.exists(src):

                def notify_src_err() -> None:
                    log_w.write_line(
                        f"Error: Source directory does not exist. [Help: {help_url}]"
                    )
                    err_msg = "Forensic scan error: Source directory does not exist."
                    self._show_help_button(help_url)
                    from app.ui.notifications import notify

                    notify(err_msg, type="error", help_url=help_url)
                    self.announce(f"{err_msg} [Help: {help_url}]")

                self._call_ui(notify_src_err)
                return

            def init_ui() -> None:
                try:
                    pb = self.query_one("#progress-bar", ProgressBar)
                    pb.progress = 0.0
                except Exception as ex:  # noqa: BLE001
                    logger.debug(f"Failed to reset progress bar: {ex}")
                log_w.write_line(f"Starting CRO Forensic Ingestion on: {src}")

            self._call_ui(init_ui)

            from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
                CROMultiStudyPipeline,
                CROPipelineState,
            )

            pipeline = CROMultiStudyPipeline(
                mode="tmf",
                smart_renaming=getattr(self.settings, "CLINICAL_SMART_RENAMING", True),
                suppress_duplicates=bool(
                    getattr(self.settings, "CLINICAL_SUPPRESS_DUPLICATES", False)
                ),
            )

            def progress_cb(prog_or_update: Any, stage_msg: str = "") -> None:
                from app.core.progress import ProgressUpdate

                if isinstance(prog_or_update, ProgressUpdate):
                    pct_val = prog_or_update.progress * 100.0
                    msg_val = prog_or_update.stage or stage_msg or ""
                elif isinstance(prog_or_update, (int, float)):
                    if 0.0 <= prog_or_update <= 1.0:
                        pct_val = prog_or_update * 100.0
                    else:
                        pct_val = float(prog_or_update)
                    msg_val = stage_msg or ""
                else:
                    pct_val = 0.0
                    msg_val = str(prog_or_update) if stage_msg == "" else stage_msg

                pct_int = int(pct_val)

                def update_ui() -> None:
                    try:
                        pb = self.query_one("#progress-bar", ProgressBar)
                        pb.progress = min(100.0, max(0.0, pct_val))
                        lw = self.query_one("#log-widget", Log)
                        lw.write_line(f"[{pct_int}%] {msg_val}")
                    except Exception as ex:  # noqa: BLE001
                        logger.debug(f"Failed to update progress UI: {ex}")

                self._call_ui(update_ui)

            result = pipeline.run_pipeline(
                source_root=src,
                target_root=tgt,
                progress_callback=progress_cb,
                cancel_check=self._cancel_event.is_set,
            )

            def finish_ui() -> None:
                if result.status == CROPipelineState.CANCELLED:
                    log_w.write_line("CRO Forensic Ingestion cancelled by user.")
                    self.announce("CRO forensic ingestion cancelled.")
                elif result.status == CROPipelineState.FAILED:
                    err_msg = result.error_message or "Unknown failure"
                    log_w.write_line(f"CRO Forensic Ingestion failed: {err_msg}")
                    self.announce(f"CRO forensic ingestion failed: {err_msg}")
                elif result.status == CROPipelineState.COMPLETED:
                    try:
                        pb = self.query_one("#progress-bar", ProgressBar)
                        pb.progress = 100.0
                    except Exception as ex:  # noqa: BLE001
                        logger.debug(f"Failed to set progress bar to 100%: {ex}")
                    summary_msg = f"Completed successfully! Total scanned: {result.total_scanned_files}, Discovered studies: {result.discovered_studies_count}"
                    log_w.write_line(summary_msg)
                    self.announce(summary_msg)
                else:
                    if self._cancel_event.is_set():
                        log_w.write_line("CRO Forensic Ingestion cancelled by user.")
                        self.announce("CRO forensic ingestion cancelled.")
                    else:
                        summary_msg = f"Finished with status {result.status}."
                        log_w.write_line(summary_msg)
                        self.announce(summary_msg)

            self._call_ui(finish_ui)

        except Exception as e:
            logger.warning("CRO forensic scan failed", exc_info=True)
            err_str = str(e)

            def err_ui() -> None:
                log_w.write_line(f"Execution error: {err_str} [Help: {help_url}]")
                err_msg = f"Forensic scan error: {err_str}"
                self._show_help_button(help_url)
                from app.ui.notifications import notify

                notify(err_msg, type="error", help_url=help_url)
                self.announce(f"{err_msg} [Help: {help_url}]")

            self._call_ui(err_ui)

        finally:
            self._call_ui(self._set_run_button_disabled, False)

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open troubleshooting documentation link."""
        super().action_help()

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Dismiss forensic modal and signal cancellation to worker thread."""
        self.cancel_worker()
        self.announce("Closed CRO forensic ingestion dialog.")
        self.dismiss(None)
