"""Settings module using NiceGUI."""

import asyncio
import os
import shutil
from pathlib import Path

from nicegui import ui
from huggingface_hub import snapshot_download

from app.config import get_app_dir
from app.ui.wizard import verify_model, _DownloadProgressTracker, _shared_download_state


def show_settings(parent_app, settings):
    """Show the settings dialog."""
    with ui.dialog() as dialog, ui.card().classes("w-3/4 max-w-4xl p-6"):
        with ui.row().classes("w-full justify-between items-center mb-6"):
            ui.label("Application Settings").classes("text-2xl font-bold").props(
                'aria-label="Settings Dialog Title"'
            )
            ui.button("Close", on_click=dialog.close).classes(
                "bg-gray-200 text-black"
            ).props('aria-label="Close Settings Button"')

        with ui.tabs().classes("w-full") as tabs:
            ui.tab("General", label="General").props(
                'aria-label="General Settings Tab"'
            )
            ui.tab("AI", label="AI Configuration").props(
                'aria-label="AI Configuration Tab"'
            )
            ui.tab("Rules", label="Routing Rules").props(
                'aria-label="Routing Rules Tab"'
            )

        with ui.tab_panels(tabs, value="General").classes("w-full mt-4"):
            with ui.tab_panel("General"):
                ui.label("Cleanup & Maintenance").classes("text-lg font-bold mb-2")
                ui.switch(
                    "Automatically remove empty directories",
                    value=settings.CLEANUP_EMPTY_DIRS,
                ).props('aria-label="Cleanup empty directories toggle"')
                ui.label("Processing Limits").classes("text-lg font-bold mt-4 mb-2")
                ui.number("Max folder depth", value=settings.MAX_FOLDER_DEPTH).props(
                    'aria-label="Max folder depth input"'
                )

            with ui.tab_panel("AI"):
                ui.label("Privacy Options").classes("text-lg font-bold mb-2")
                ui.label("AI processing is fully offline.").classes(
                    "text-gray-500 mb-2"
                )

                async def do_reset_cache():
                    model_dir = get_app_dir() / "model"
                    if model_dir.exists():
                        shutil.rmtree(model_dir)
                    settings.AI_CONSENT_GRANTED = False
                    ui.notify("Model cache cleared and consent reset.", type="info")
                    await update_ui_state()

                ui.button(
                    "Reset Model Cache", on_click=do_reset_cache
                ).props('aria-label="Reset Model Cache Button"')

                ui.label("Recovery Download").classes("text-lg font-bold mt-4 mb-2")
                status_label = ui.label("").classes("text-sm mb-2 font-bold")
                download_btn = ui.button("Download Missing Model Files").classes("bg-blue-500 text-white").props('aria-label="Recovery Download Button"')
                download_btn.set_visibility(False)

                download_timer = None

                def update_progress():
                    if _shared_download_state.total > 0:
                        p = _shared_download_state.n / _shared_download_state.total
                        status_label.set_text(f"Downloading... {int(p*100)}%")

                def do_download():
                    model_dir = get_app_dir() / "model"
                    snapshot_download("sentence-transformers/all-MiniLM-L6-v2", local_dir=str(model_dir), tqdm_class=_DownloadProgressTracker)

                async def trigger_download():
                    nonlocal download_timer
                    download_btn.disable()
                    _shared_download_state.n = 0
                    _shared_download_state.total = 1
                    status_label.set_text("Downloading... 0%")
                    
                    download_timer = ui.timer(0.2, update_progress)
                    
                    try:
                        await asyncio.to_thread(do_download)
                    except Exception as e:
                        download_timer.cancel()
                        ui.notify(f"Download failed: {e}", type="negative")
                        status_label.set_text("Download failed.")
                        download_btn.enable()
                        return
                        
                    download_timer.cancel()
                    status_label.set_text("Verifying files...")
                    
                    is_valid = await asyncio.to_thread(verify_model)
                    if not is_valid:
                        ui.notify("Verification failed. Corrupted files.", type="negative")
                        status_label.set_text("Verification failed.")
                        download_btn.enable()
                        return

                    settings.AI_CONSENT_GRANTED = True
                    status_label.set_text("Model files verified.")
                    ui.notify("Model downloaded successfully.", type="positive")
                    download_btn.set_visibility(False)
                    await update_ui_state()

                download_btn.on('click', trigger_download)

                async def update_ui_state():
                    status_label.set_text("Checking model integrity...")
                    download_btn.set_visibility(False)
                    is_valid = await asyncio.to_thread(verify_model)
                    if is_valid:
                        status_label.set_text("Model files are verified and present.")
                        download_btn.set_visibility(False)
                    else:
                        status_label.set_text("Model files are missing or corrupted.")
                        download_btn.set_visibility(True)
                        download_btn.enable()

                # Trigger initial state check
                ui.timer(0, update_ui_state, once=True)

            with ui.tab_panel("Rules"):
                ui.label("Keyword Routing").classes("text-lg font-bold mb-2")
                ui.input("Keyword").props(
                    'placeholder="e.g. invoice" aria-label="Keyword input"'
                )
                ui.input("Target Path").props(
                    'placeholder="/path/to/folder" aria-label="Target Path input"'
                )
                ui.button("Add Rule", on_click=lambda: ui.notify("Rule added.")).props(
                    'aria-label="Add Rule Button"'
                )

    dialog.open()
