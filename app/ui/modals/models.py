"""Offline model manager dialog."""

import os
from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ProgressBar

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin


class ModelManagerModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[None]):
    """Modal screen for interactive local ONNX model management."""

    BINDINGS = [
        Binding("escape", "cancel", "Close dialog", show=True),
    ]

    CSS = """
    ModelManagerModal {
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
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .info-label {
        margin-top: 0;
        color: $text;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings
        self._prev_is_downloading = False
        self._prev_status_text = ""

    def _resolve_model_dir(self) -> str:
        """Resolve primary model directory path."""
        try:
            from app.config import get_app_dir

            return str(get_app_dir() / "model")
        except Exception:
            return os.path.expanduser("~/.smart-autosorter/model")

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Interactive Model Management [Ctrl+K]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )

            yield Heading("Model Status:", level=2, classes="field-label")
            yield Label(
                "Status: Checking...", id="lbl-model-status", classes="info-label"
            )
            yield Label(
                "Location: Checking...", id="lbl-model-path", classes="info-label"
            )
            yield Label("Size on Disk: 0 MB", id="lbl-model-size", classes="info-label")
            yield Label(
                "Verification: Checking...", id="lbl-model-hash", classes="info-label"
            )

            yield Heading(
                "Download Progress & Throughput:", level=2, classes="field-label"
            )
            pb = ProgressBar(total=100, show_percentage=True, id="progress-bar")
            pb.tooltip = "Active model download percentage progress"
            yield pb

            lbl_progress = Label("Idle", id="lbl-progress-status", classes="info-label")
            lbl_progress.tooltip = "Current download stage and throughput metrics"
            yield lbl_progress

            yield Heading("Network Proxy Options:", level=2, classes="field-label")
            proxy_val = getattr(self.settings, "PROXY", "") if self.settings else ""
            inp_proxy = Input(
                value=proxy_val,
                placeholder="http://proxy.example.com:8080",
                id="input-proxy",
            )
            inp_proxy.tooltip = "HTTP/HTTPS proxy URL for model weight transfers"
            yield inp_proxy

            with Horizontal(classes="button-row"):
                btn_start = Button("Start Download", id="btn-start", variant="primary")
                btn_start.tooltip = "Start downloading local ONNX model weights"
                yield btn_start

                btn_cancel = Button("Cancel", id="btn-cancel-dl", variant="warning")
                btn_cancel.tooltip = "Cancel active model download thread"
                yield btn_cancel

                btn_retry = Button("Retry Download", id="btn-retry", variant="primary")
                btn_retry.tooltip = "Retry or restart model download"
                yield btn_retry

                btn_delete = Button("Delete Model", id="btn-delete", variant="error")
                btn_delete.tooltip = (
                    "Unload in-memory model instances and delete local files"
                )
                yield btn_delete

                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close Model Management dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Focus initial input, set polling timer, and announce modal launch."""
        self._update_layout(self.size.width)
        self.query_one("#input-proxy", Input).focus()
        self.announce("Opened Model Management dialog.")
        self._refresh_ui_state()
        self.set_interval(0.2, self._poll_download_status)

    def _apply_proxy(self) -> str:
        p_val = self.query_one("#input-proxy", Input).value.strip()
        if self.settings:
            setattr(self.settings, "PROXY", p_val)
        from app.core.downloader import DownloadManager

        dm = DownloadManager.get_instance()
        dm.update_proxy(p_val)
        return p_val

    def _refresh_ui_state(self) -> None:
        """Update model file info labels and control button states."""
        from app.core.downloader import DownloadManager, verify_downloaded_model

        dm = DownloadManager.get_instance()

        model_dir = self._resolve_model_dir()
        onnx_file = os.path.join(model_dir, "model.onnx")
        tmp_file = os.path.join(model_dir, "model.onnx.tmp")

        lbl_status = self.query_one("#lbl-model-status", Label)
        lbl_path = self.query_one("#lbl-model-path", Label)
        lbl_size = self.query_one("#lbl-model-size", Label)
        lbl_hash = self.query_one("#lbl-model-hash", Label)

        lbl_path.update(f"Location: {model_dir}")

        model_exists = os.path.exists(onnx_file) and os.path.getsize(onnx_file) > 0
        tmp_exists = os.path.exists(tmp_file)

        if model_exists:
            size_mb = os.path.getsize(onnx_file) / (1024 * 1024)
            lbl_size.update(f"Size on Disk: {size_mb:.2f} MB")
            lbl_status.update("Status: Downloaded & Present")
            if verify_downloaded_model(model_dir):
                lbl_hash.update("Verification: Valid (Cryptographically Verified)")
            else:
                lbl_hash.update("Verification: Unverified or Missing Configuration")
        elif tmp_exists:
            size_mb = os.path.getsize(tmp_file) / (1024 * 1024)
            lbl_size.update(f"Size on Disk: {size_mb:.2f} MB (Incomplete)")
            lbl_status.update("Status: Downloading / Incomplete")
            lbl_hash.update("Verification: Pending Finalization")
        else:
            lbl_size.update("Size on Disk: 0 MB")
            lbl_status.update("Status: Missing (Not Downloaded)")
            lbl_hash.update("Verification: N/A")

        is_dl = dm.state["is_downloading"]
        self.query_one("#btn-start", Button).disabled = is_dl
        self.query_one("#btn-cancel-dl", Button).disabled = not is_dl
        self.query_one("#btn-retry", Button).disabled = is_dl
        self.query_one("#btn-delete", Button).disabled = not (
            model_exists or tmp_exists or is_dl
        )

    def _poll_download_status(self) -> None:
        """Periodic non-blocking timer callback to poll download status."""
        from app.core.downloader import DownloadManager

        dm = DownloadManager.get_instance()

        is_dl = dm.state["is_downloading"]
        prog = float(dm.state["progress"])
        status_text = str(dm.state["status_text"] or "")
        err = dm.state["error"]
        succ = dm.state["success"]

        pb = self.query_one("#progress-bar", ProgressBar)
        pb.progress = min(100.0, max(0.0, prog * 100.0))

        lbl_prog = self.query_one("#lbl-progress-status", Label)
        if err:
            lbl_prog.update(f"Error: {err}")
        elif succ:
            lbl_prog.update("Download complete!")
        else:
            lbl_prog.update(
                status_text if status_text else ("Downloading..." if is_dl else "Idle")
            )

        if self._prev_is_downloading and not is_dl:
            if succ:
                self.announce(
                    "Model download completed successfully.", priority="polite"
                )
            elif err:
                self.announce(f"Model download failed: {err}", priority="assertive")
            elif "cancelled" in status_text.lower():
                self.announce("Model download cancelled.", priority="polite")

        self._prev_is_downloading = is_dl
        self._prev_status_text = status_text

        self._refresh_ui_state()

    @on(Button.Pressed, "#btn-start")
    def action_start_download(self) -> None:
        """Start background model weight download."""
        p_val = self._apply_proxy()
        from app.core.downloader import DEFAULT_MODEL_URL, DownloadManager

        dm = DownloadManager.get_instance()
        model_dir = self._resolve_model_dir()
        try:
            dm.start_download(DEFAULT_MODEL_URL, model_dir, proxy=p_val)
            self.announce("Started model download.", priority="polite")
        except Exception as e:
            from app.ui.notifications import notify

            notify(f"Download error: {e}", type="error")
            self.announce(f"Download error: {e}", priority="assertive")

    @on(Button.Pressed, "#btn-cancel-dl")
    def action_cancel_download(self) -> None:
        """Cancel active model download thread."""
        from app.core.downloader import DownloadManager

        dm = DownloadManager.get_instance()
        dm.cancel_download()
        self.announce("Cancelled model download.", priority="polite")

    @on(Button.Pressed, "#btn-retry")
    def action_retry_download(self) -> None:
        """Retry model weight download operation."""
        p_val = self._apply_proxy()
        from app.core.downloader import DEFAULT_MODEL_URL, DownloadManager

        dm = DownloadManager.get_instance()
        if dm.state["is_downloading"]:
            dm.cancel_download()
        model_dir = self._resolve_model_dir()
        try:
            dm.start_download(DEFAULT_MODEL_URL, model_dir, proxy=p_val)
            self.announce("Retrying model download.", priority="polite")
        except Exception as e:
            from app.ui.notifications import notify

            notify(f"Download error: {e}", type="error")
            self.announce(f"Download error: {e}", priority="assertive")

    @on(Button.Pressed, "#btn-delete")
    def action_delete_model(self) -> None:
        """Asynchronously delete local model files."""
        from app.core.downloader import DownloadManager

        dm = DownloadManager.get_instance()
        model_dir = self._resolve_model_dir()

        def _on_done(success: bool, err: Optional[Exception]):
            if success:
                self.announce("Model deleted successfully.", priority="polite")
            else:
                self.announce(f"Model deletion failed: {err}", priority="assertive")

        dm.delete_model_async(model_dir, on_done=_on_done)
        self.announce("Deleting model weights...", priority="polite")

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Close model management dialog."""
        self.announce("Closed Model Management dialog.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel and close model management dialog."""
        self.announce("Closed Model Management dialog.")
        self.dismiss(None)
