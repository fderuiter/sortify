"""Setup wizard module using NiceGUI."""

import logging

from nicegui import ui

from app.ui.dialog_helper import get_dialog_card_classes

logger = logging.getLogger(__name__)


def show_wizard(parent_app, settings):
    """Show the initial setup wizard."""
    with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("md")):
        ui.label("AI Features Setup").classes("text-xl font-bold mb-4").props(
            'aria-label="Setup Wizard Title"'
        )

        ui.label(
            "To use the Smart AutoSorter AI features, the application needs to initialize the local keyword clustering engine (TF-IDF & NMF)."
        ).classes("mb-2").props('aria-label="Setup Description"')
        ui.label(
            "Your privacy is important to us. All processing will happen entirely offline."
        ).classes("mb-4").props('aria-label="Privacy Description"')

        progress_bar = (
            ui.linear_progress(value=0)
            .classes("w-full mb-2")
            .props('aria-label="Download Progress Bar"')
        )
        status_label = (
            ui.label("")
            .classes("text-sm text-gray-500 mb-4")
            .props('aria-label="Download Status"')
        )

        async def accept():
            accept_btn.disable()
            decline_btn.disable()
            status_label.set_text("Initializing download...")
            progress_bar.set_value(0)

            from nicegui import run

            from app.core.downloader import download_ai_models

            def on_progress(bytes_dl, total_bytes, file_idx, total_files, filename):
                file_fraction = 1.0 / total_files
                file_progress = (bytes_dl / total_bytes) if total_bytes else 0
                overall_percent = (file_idx + file_progress) * file_fraction
                progress_bar.set_value(overall_percent)
                if total_bytes:
                    status_label.set_text(
                        f"[{file_idx + 1}/{total_files}] Downloading {filename}: "
                        f"{file_progress:.1%} ({bytes_dl / 1024 / 1024:.1f}MB / {total_bytes / 1024 / 1024:.1f}MB)"
                    )
                else:
                    status_label.set_text(
                        f"[{file_idx + 1}/{total_files}] Downloading {filename}: {bytes_dl / 1024 / 1024:.1f}MB"
                    )

            try:
                settings.AI_CONSENT_GRANTED = True
                settings.AI_ASSISTED_NAMING = True

                success = await run.io_bound(download_ai_models, settings, on_progress)
                if success:
                    ui.notify("Setup Complete.", type="positive")
                    dialog.close()
                else:
                    ui.notify("Setup failed: some models failed to download.", type="negative")
                    accept_btn.enable()
                    decline_btn.enable()
            except Exception as e:
                logger.error(f"Download setup wizard error: {e}")
                ui.notify(f"Download failed: {e}", type="negative")
                status_label.set_text(f"Error: {e}")
                accept_btn.enable()
                decline_btn.enable()

        def decline():
            settings.AI_CONSENT_GRANTED = False
            ui.notify("Offline mode enabled.", type="info")
            dialog.close()

        with ui.row().classes("w-full justify-between flex-wrap gap-2"):
            accept_btn = ui.button("Accept & Download", on_click=accept).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Accept and Download Button"')
            decline_btn = ui.button("Decline", on_click=decline).classes(
                "bg-gray-500 text-white"
            ).props('aria-label="Decline Button"')

    dialog.open()

