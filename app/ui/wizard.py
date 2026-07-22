"""Setup wizard module using NiceGUI."""

import asyncio
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download
from nicegui import ui

from app.config import get_app_dir


class SharedTracker:
    """Shared state tracker for background downloads."""

    def __init__(self):
        self.n = 0
        self.total = 1

_shared_download_state = SharedTracker()

class _DownloadProgressTracker:
    def __init__(self, *args, **kwargs):
        self.total = kwargs.get('total', 1)
        self.n = kwargs.get('initial', 0)
        
        _shared_download_state.total = self.total
        _shared_download_state.n = self.n

    def update(self, n=1):
        self.n += n
        _shared_download_state.n = self.n

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def verify_model():
    """Verify model integrity offline by comparing SHA256 checksums."""
    model_dir = get_app_dir() / "model"
    if not model_dir.exists():
        return False
        
    manifest_path = Path(__file__).parent.parent / "core" / "hf_manifest.json"
    if not manifest_path.exists():
        return True
        
    with open(manifest_path, "r") as f:
        manifest = json.load(f)
        
    for rel_path, expected_hash in manifest.items():
        if rel_path.startswith(".cache"):
            continue
            
        filepath = model_dir / rel_path
        if not filepath.exists():
            return False
            
        sha256 = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    sha256.update(chunk)
            if sha256.hexdigest() != expected_hash:
                return False
        except Exception:
            return False
            
    return True


def show_wizard(parent_app, settings):
    """Show the initial setup wizard."""
    with ui.dialog() as dialog, ui.card().classes("w-96 p-6"):
        ui.label("AI Features Setup").classes("text-xl font-bold mb-4").props(
            'aria-label="Setup Wizard Title"'
        )

        ui.label(
            "To use the Smart AutoSorter AI features, the application needs to download a small AI model (all-MiniLM-L6-v2)."
        ).classes("mb-2").props('aria-label="Setup Description"')
        ui.label(
            "Your privacy is important to us. All processing will happen entirely offline."
        ).classes("mb-4").props('aria-label="Privacy Description"')

        progress = (
            ui.linear_progress(value=0)
            .classes("w-full mb-2")
            .props('aria-label="Download Progress Bar"')
        )
        status = (
            ui.label("")
            .classes("text-sm text-gray-500 mb-4")
            .props('aria-label="Download Status"')
        )

        download_timer = None

        def update_progress():
            if _shared_download_state.total > 0:
                p = _shared_download_state.n / _shared_download_state.total
                progress.set_value(p)

        def do_download():
            model_dir = get_app_dir() / "model"
            snapshot_download("sentence-transformers/all-MiniLM-L6-v2", local_dir=str(model_dir), tqdm_class=_DownloadProgressTracker)

        async def accept():
            nonlocal download_timer
            btn_accept.disable()
            btn_decline.disable()
            
            _shared_download_state.n = 0
            _shared_download_state.total = 1
            
            status.set_text("Downloading...")
            progress.set_value(0.0)
            
            download_timer = ui.timer(0.2, update_progress)
            
            try:
                await asyncio.to_thread(do_download)
            except Exception as e:
                download_timer.cancel()
                ui.notify(f"Download failed: {e}", type="negative")
                btn_accept.enable()
                btn_decline.enable()
                status.set_text("Download failed.")
                return
                
            download_timer.cancel()
            progress.set_value(1.0)
            status.set_text("Verifying files...")
            
            is_valid = await asyncio.to_thread(verify_model)
            if not is_valid:
                ui.notify("Verification failed. Corrupted files.", type="negative")
                btn_accept.enable()
                btn_decline.enable()
                status.set_text("Verification failed.")
                return

            settings.AI_CONSENT_GRANTED = True
            status.set_text("Done.")

            ui.notify("Setup Complete. Model downloaded.", type="positive")
            dialog.close()

        def decline():
            settings.AI_CONSENT_GRANTED = False
            ui.notify("Offline mode enabled.", type="info")
            dialog.close()

        with ui.row().classes("w-full justify-between"):
            btn_accept = ui.button("Accept & Download", on_click=accept).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Accept and Download Button"')
            btn_decline = ui.button("Decline", on_click=decline).classes(
                "bg-gray-500 text-white"
            ).props('aria-label="Decline Button"')

    dialog.open()
