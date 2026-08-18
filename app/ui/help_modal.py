"""Help modal module using NiceGUI."""

import logging
import sys
from pathlib import Path

from nicegui import ui

from app.core.path_utils import get_base_path, is_packaged
from app.ui.dialog_helper import get_dialog_card_classes

logger = logging.getLogger(__name__)


def show_help(parent_app=None):
    """Show the user guide in a native scrollable dialog modal."""
    # Determine the path to the docs directory
    if is_packaged() and hasattr(sys, "_MEIPASS"):
        # In a PyInstaller compiled package, files are unpacked under sys._MEIPASS
        base_dir = Path(sys._MEIPASS)
    else:
        # In development mode, files are located in the repository root directory
        base_dir = Path(get_base_path(__file__))

    path = base_dir / "docs" / "user_guide.md"
    logger.info("Loading user guide from path: %s", path)

    try:
        if path.exists():
            content = path.read_text(encoding="utf-8")
        else:
            content = f"Error: User guide not found at expected path: `{path}`."
            logger.error(content)
    except Exception as e:
        content = f"Error reading user guide: {e}"
        logger.exception("Failed to read user guide file.")

    with ui.dialog() as dialog:
        # Use xl size for the dialog card to render markdown guide with plenty of space
        with ui.card().classes(get_dialog_card_classes("xl", "max-h-[80vh] flex flex-col")):
            # Header Row
            with ui.row().classes(
                "w-full justify-between items-center mb-4 flex-nowrap"
            ):
                ui.label("User Guide & Documentation").classes(
                    "text-2xl font-bold"
                ).props('aria-label="Help Dialog Title"')
                ui.button("Close", on_click=dialog.close).classes(
                    "bg-gray-200 text-black shrink-0"
                ).props('aria-label="Close Help Dialog Button"')

            # Scrollable area containing the fully formatted user guide markdown
            with ui.scroll_area().classes(
                "w-full flex-grow border rounded p-4 overflow-y-auto"
            ):
                ui.markdown(content).classes("w-full")

            # Footer / Close button at bottom
            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("Close", on_click=dialog.close).classes(
                    "bg-blue-500 text-white"
                ).props('aria-label="Close Help Dialog Footer Button"')

    dialog.open()
