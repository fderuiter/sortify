"""Centralized report handler for client-side HTTP file serving, downloading, and viewing."""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional, Union

try:
    from nicegui import app, ui
except ImportError:
    from unittest.mock import MagicMock

    ui = MagicMock()
    app = MagicMock()

from app.ui.tokens import TOKENS

logger = logging.getLogger(__name__)

# Registry for dynamic static routes to prevent duplicate route registration
_SERVED_ROUTES: Dict[str, str] = {}


def get_dialog_card_classes(size: str = "md", extra: str = "") -> str:
    """Get standardized dialog card classes from design tokens.

    Parameters
    ----------
    size : str, optional
        Dialog card size ('md', 'lg', 'xl'), default "md"
    extra : str, optional
        Extra CSS classes to append, default ""

    Returns
    -------
    str
        Combined CSS class string for dialog card layout
    """
    classes = {
        "md": TOKENS.COMPONENTS.DIALOG_CARD_MD,
        "lg": TOKENS.COMPONENTS.DIALOG_CARD_LG,
        "xl": TOKENS.COMPONENTS.DIALOG_CARD_XL,
    }
    base = classes.get(size, TOKENS.COMPONENTS.DIALOG_CARD_MD)
    if extra:
        return f"{base} {extra}"
    return base


def show_file_error_dialog(
    file_path: Union[str, Path, None], error_message: str = ""
) -> None:
    """Display a fallback modal dialog when a report file is unreadable or missing.

    Presents the absolute file path, a 'Copy Path' button, and error feedback.

    Parameters
    ----------
    file_path : str, Path, or None
        Path to the target report file.
    error_message : str, optional
        Detailed error explanation to display in the modal dialog.
    """
    abs_path = os.path.abspath(str(file_path)) if file_path else "Unknown Path"
    msg = (
        error_message
        or "The requested report file could not be read or found on the server."
    )

    with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("md")):
        ui.label("Report File Access Error").classes(
            "text-lg font-bold text-red-600 mb-2"
        )
        ui.label(msg).classes("text-sm text-slate-700 mb-3")

        ui.label("Absolute File Path:").classes(
            "text-xs font-semibold text-slate-500 mb-1"
        )
        ui.input(value=abs_path).props("readonly outlined dense").classes(
            "w-full mb-4 font-mono text-xs"
        )

        def copy_path():
            try:
                js_code = f"navigator.clipboard.writeText({json.dumps(abs_path)});"
                if hasattr(ui, "run_javascript") and callable(ui.run_javascript):
                    ui.run_javascript(js_code)
                elif (
                    hasattr(ui, "clipboard")
                    and hasattr(ui.clipboard, "write")
                    and callable(ui.clipboard.write)
                ):
                    ui.clipboard.write(abs_path)
            except Exception as ex:
                logger.warning(f"Clipboard copy JS failed: {ex}")

            if hasattr(ui, "notify") and callable(ui.notify):
                ui.notify(
                    "File path copied to clipboard", color="positive", icon="content_copy"
                )

        with ui.row().classes("w-full justify-end items-center gap-2"):
            ui.button("Copy Path", on_click=copy_path, icon="content_copy").props(
                "color='primary' outline dense"
            )
            ui.button("Close", on_click=dialog.close).props("color='grey' dense")

    if hasattr(dialog, "open") and callable(dialog.open):
        dialog.open()


def register_static_route_for_file(abs_path: str) -> str:
    """Dynamically register a static HTTP route for serving a local report file.

    Parameters
    ----------
    abs_path : str
        Absolute path to the local report file.

    Returns
    -------
    str
        Relative route URL path for accessing the file over HTTP.
    """
    if abs_path in _SERVED_ROUTES:
        return _SERVED_ROUTES[abs_path]

    filename = os.path.basename(abs_path)
    path_hash = str(abs(hash(abs_path)))
    route_url = f"/served_reports/{path_hash}/{filename}"

    try:
        if hasattr(app, "add_static_file") and callable(app.add_static_file):
            app.add_static_file(local_file=abs_path, url_path=route_url)
            _SERVED_ROUTES[abs_path] = route_url
            return route_url
        elif hasattr(app, "add_media_files") and callable(app.add_media_files):
            parent_dir = os.path.dirname(abs_path)
            media_route = f"/served_media/{path_hash}"
            app.add_media_files(media_route, parent_dir)
            route_url = f"{media_route}/{filename}"
            _SERVED_ROUTES[abs_path] = route_url
            return route_url
    except Exception as e:
        logger.warning(
            f"Dynamic static route registration failed for {abs_path}: {e}"
        )

    return ""


def serve_or_download_report(
    file_path: Union[str, Path, None],
    open_in_new_tab: bool = False,
    filename: Optional[str] = None,
) -> bool:
    """Centralized report handler for client-side file download and HTTP route serving.

    Validates target file path, guards against directory traversal, and delivers file via
    ui.download() or dynamic static route navigation. Displays fallback error modal dialog
    if target file is invalid or unreadable.

    Parameters
    ----------
    file_path : str, Path, or None
        Path to the report or manifest file.
    open_in_new_tab : bool, optional
        Whether to attempt opening the file in a new browser tab via route serving, default False
    filename : str, optional
        Custom filename for download payload, default None

    Returns
    -------
    bool
        True if download or navigation was initiated successfully, False otherwise.
    """
    if not file_path:
        show_file_error_dialog(
            "", "No file path was provided for the requested report."
        )
        return False

    str_path = str(file_path)
    if "\0" in str_path:
        show_file_error_dialog(
            str_path, "Invalid file path containing null characters."
        )
        return False

    try:
        abs_path = os.path.abspath(os.path.expanduser(str_path))
        real_path = os.path.realpath(abs_path)
    except Exception as err:
        show_file_error_dialog(str_path, f"Failed to resolve file path: {err}")
        return False

    if not os.path.exists(real_path) or not os.path.isfile(real_path):
        show_file_error_dialog(
            abs_path, f"Target report file does not exist: {abs_path}"
        )
        return False

    try:
        with open(real_path, "rb") as f:
            _ = f.read(1)
    except (PermissionError, OSError) as read_err:
        show_file_error_dialog(abs_path, f"Cannot read report file: {read_err}")
        return False

    if open_in_new_tab:
        route_url = register_static_route_for_file(real_path)
        if route_url:
            try:
                if (
                    hasattr(ui, "navigate")
                    and hasattr(ui.navigate, "to")
                    and callable(ui.navigate.to)
                ):
                    ui.navigate.to(route_url, new_tab=True)
                    return True
                elif hasattr(ui, "run_javascript") and callable(ui.run_javascript):
                    ui.run_javascript(f"window.open('{route_url}', '_blank');")
                    return True
            except Exception as nav_err:
                logger.warning(
                    f"Browser navigation failed, falling back to ui.download: {nav_err}"
                )

    target_name = filename or os.path.basename(real_path)
    try:
        if hasattr(ui, "download") and callable(ui.download):
            try:
                ui.download(real_path, filename=target_name)
            except TypeError:
                ui.download(real_path)
            return True
        else:
            show_file_error_dialog(abs_path, "Client download interface is unavailable.")
            return False
    except Exception as dl_err:
        logger.error(f"Error during ui.download for {real_path}: {dl_err}")
        show_file_error_dialog(abs_path, f"Failed to initiate file download: {dl_err}")
        return False
