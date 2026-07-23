"""Setup wizard module using NiceGUI."""

import asyncio

from nicegui import ui


def show_wizard(parent_app, settings):
    """Show the initial setup wizard."""
    with ui.dialog() as dialog, ui.card().classes("w-96 p-6"):
        ui.label("AI Features Setup").classes("text-xl font-bold mb-4").props(
            'aria-label="Setup Wizard Title"'
        )

        ui.label(
            "To use the Smart AutoSorter AI features, the application needs to initialize the local keyword clustering engine (TF-IDF & NMF)."
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

        async def accept():
            status.set_text("Downloading...")
            progress.set_value(0.5)
            await asyncio.sleep(0.5)  # Simulate async download

            settings.AI_CONSENT_GRANTED = True
            progress.set_value(1.0)
            status.set_text("Done.")

            ui.notify("Setup Complete. Model downloaded.", type="positive")
            dialog.close()

        def decline():
            settings.AI_CONSENT_GRANTED = False
            ui.notify("Offline mode enabled.", type="info")
            dialog.close()

        with ui.row().classes("w-full justify-between"):
            ui.button("Accept & Download", on_click=accept).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Accept and Download Button"')
            ui.button("Decline", on_click=decline).classes(
                "bg-gray-500 text-white"
            ).props('aria-label="Decline Button"')

    dialog.open()
