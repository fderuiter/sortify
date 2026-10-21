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
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Log, ProgressBar

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.notifications import TROUBLESHOOTING_URL

logger = logging.getLogger(__name__)


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
    ProgressBar {
        margin-top: 1;
        margin-bottom: 1;
        width: 100%;
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
    Button.hidden {
        display: none;
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
        with Vertical(classes="modal-box"):
            yield Heading(
                "CRO Multi-Study Forensic Ingestion [Ctrl+C]",
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

    def _call_ui(self, func: Any, *args: Any, **kwargs: Any) -> None:
        """Safely dispatch UI callback to the main event loop thread."""
        app_obj = getattr(self, "app", None)

        if app_obj and hasattr(app_obj, "call_from_thread"):
            try:
                app_obj.call_from_thread(func, *args, **kwargs)
                return
            except Exception as e:  # noqa: BLE001
                logger.debug(f"call_from_thread failed: {e}")
        try:
            func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Direct UI function call failed: {e}")

    def _set_run_button_disabled(self, disabled: bool) -> None:
        """Set disabled status of the run button."""
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
            )

            pipeline = CROMultiStudyPipeline(
                mode="tmf",
                smart_renaming=getattr(self.settings, "CLINICAL_SMART_RENAMING", True),
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
                if self._cancel_event.is_set():
                    log_w.write_line("CRO Forensic Ingestion cancelled by user.")
                    self.announce("CRO forensic ingestion cancelled.")
                else:
                    try:
                        pb = self.query_one("#progress-bar", ProgressBar)
                        pb.progress = 100.0
                    except Exception as ex:  # noqa: BLE001
                        logger.debug(f"Failed to set progress bar to 100%: {ex}")
                    summary_msg = f"Completed successfully! Total scanned: {result.total_scanned_files}, Discovered studies: {result.discovered_studies_count}"
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
        url = getattr(
            self,
            "help_url",
            f"{TROUBLESHOOTING_URL}#cro-forensic-ingestion",
        )
        from app.ui.notifications import notify

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Dismiss forensic modal and signal cancellation to worker thread."""
        self.cancel_worker()
        self.announce("Closed CRO forensic ingestion dialog.")
        self.dismiss(None)
