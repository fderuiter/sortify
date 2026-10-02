"""Helper module for asynchronous directory selection with focus elevation."""

import asyncio
import base64
import logging
import os
import sys
from unittest.mock import MagicMock

from app.core.env_helper import run_background_process
from app.ui.tokens import TOKENS

_dummy_ui = MagicMock()

try:
    from nicegui import ui

    _NICEGUI_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    ui = _dummy_ui
    _NICEGUI_AVAILABLE = False

logger = logging.getLogger(__name__)

# Standardized responsive fluid layout helpers re-exported from tokens
STANDARD_DIALOG_CARD_MD = TOKENS.COMPONENTS.DIALOG_CARD_MD
STANDARD_DIALOG_CARD_LG = TOKENS.COMPONENTS.DIALOG_CARD_LG
STANDARD_DIALOG_CARD_XL = TOKENS.COMPONENTS.DIALOG_CARD_XL


def get_dialog_card_classes(size="md", extra=""):
    """Get standardized, fluid-width layout classes for dialog cards to ensure responsiveness."""
    classes = {
        "md": TOKENS.COMPONENTS.DIALOG_CARD_MD,
        "lg": TOKENS.COMPONENTS.DIALOG_CARD_LG,
        "xl": TOKENS.COMPONENTS.DIALOG_CARD_XL,
    }
    base = classes.get(size, TOKENS.COMPONENTS.DIALOG_CARD_MD)
    if extra:
        return f"{base} {extra}"
    return base


def _render_fallback_dialog(
    title="Select Directory",
    callback=None,
    enable_ui_callback=None,
):
    """Render an accessible NiceGUI modal dialog as fallback for manual directory selection when native pickers fail."""
    finished = False

    def _cleanup_and_finish(selected_path: str):
        nonlocal finished
        if finished:
            return
        finished = True
        if enable_ui_callback:
            enable_ui_callback()
        if callback:
            callback(selected_path)

    try:
        is_ci = (
            os.environ.get("CI", "").lower() == "true"
            or os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
            or os.environ.get("TF_BUILD", "").lower() == "true"
            or os.environ.get("HEADLESS_BUILD", "") == "1"
        )
        is_mock_ui = isinstance(ui, MagicMock) or getattr(
            ui, "__module__", ""
        ).startswith("unittest.mock")

        if (not _NICEGUI_AVAILABLE and ui is _dummy_ui) or (is_ci and not is_mock_ui):
            logger.warning(
                "NiceGUI is not available or running in headless CI mode; skipping fallback dialog."
            )
            _cleanup_and_finish("")
            return None

        dialog = ui.dialog()
        with dialog, ui.card().classes(get_dialog_card_classes("md")):
            dialog.props("persistent")

            dialog_title = title or "Select Directory"
            ui.label(dialog_title).classes(
                "text-lg font-bold text-gray-900 mb-1"
            ).props('aria-label="Directory Selection Dialog"')

            # Live region alert surfacing native directory picker failure
            ui.label(
                "Native folder picker unavailable. Please enter directory path manually."
            ).classes(
                "text-sm text-amber-700 bg-amber-50 p-2 rounded w-full mb-3"
            ).props('role="alert" aria-live="polite"')

            # Manual path input field with explicit label and aria-label
            path_input = (
                ui.input(
                    label="Enter Directory Path",
                    placeholder="/path/to/directory",
                )
                .classes("w-full mb-2")
                .props('outlined dense autofocus aria-label="Enter Directory Path"')
            )

            # Inline validation message element
            error_label = (
                ui.label("")
                .classes("text-xs text-red-600 font-medium hidden w-full mb-2")
                .props('role="alert" aria-live="assertive"')
            )

            def _on_close(selected_path: str):
                try:
                    dialog.close()
                except Exception:
                    pass
                _cleanup_and_finish(selected_path)

            def _validate_and_submit():
                raw_val = (
                    path_input.value
                    if hasattr(path_input, "value") and path_input.value
                    else ""
                )
                val = raw_val.strip() if isinstance(raw_val, str) else ""
                if not val:
                    error_label.set_text("Directory path cannot be empty.")
                    error_label.classes(remove="hidden")
                    return

                clean_path = os.path.abspath(val)
                if not os.path.exists(clean_path):
                    error_label.set_text("The specified path does not exist.")
                    error_label.classes(remove="hidden")
                    return
                if not os.path.isdir(clean_path):
                    error_label.set_text("The specified path is not a valid directory.")
                    error_label.classes(remove="hidden")
                    return

                _on_close(clean_path)

            def _on_cancel():
                _on_close("")

            # Focus elevation
            if hasattr(path_input, "run_method"):
                try:
                    path_input.run_method("focus")
                except Exception:
                    pass
            elif hasattr(path_input, "focus"):
                try:
                    path_input.focus()
                except Exception:
                    pass

            # Action buttons
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=_on_cancel).classes(
                    "bg-gray-200 text-gray-800"
                ).props('aria-label="Cancel Directory Selection"')
                ui.button("Confirm", on_click=_validate_and_submit).classes(
                    "bg-blue-600 text-white"
                ).props('aria-label="Confirm Directory Selection"')

        # Remove inner try-except on dialog.open() so any open failure triggers fallback cleanup
        dialog.open()
        return dialog
    except Exception as e:
        logger.warning(f"Failed to render fallback directory dialog: {e}")
        _cleanup_and_finish("")
        return None


def ask_directory_async(
    parent=None,
    title="Select Directory",
    callback=None,
    disable_ui_callback=None,
    enable_ui_callback=None,
):
    """Launch native OS directory selector asynchronously to prevent blocking the web UI execution thread.

    Force the native OS window manager to bring the newly opened folder picker window to the front.
    If the native OS directory picker fails, render an accessible modal dialog as fallback.
    """
    if disable_ui_callback:
        disable_ui_callback()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    fut = None
    if loop and callback is None:
        fut = loop.create_future()

        def _future_callback(p):
            if not fut.done():
                fut.set_result(p)

        callback = _future_callback

    def _run_dialog():
        path = ""
        success = False

        is_ci = (
            os.environ.get("CI", "").lower() == "true"
            or os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
            or os.environ.get("TF_BUILD", "").lower() == "true"
            or os.environ.get("HEADLESS_BUILD", "") == "1"
        )
        is_mock_run = isinstance(run_background_process, MagicMock) or getattr(
            run_background_process, "__module__", ""
        ).startswith("unittest.mock")

        try:
            if is_ci and not is_mock_run:
                logger.info(
                    "Running in headless CI environment; skipping native directory picker execution."
                )
                success = False
            elif sys.platform == "darwin":
                # macOS AppleScript
                cmd = [
                    "osascript",
                    "-e",
                    "try",
                    "-e",
                    f'set f to choose folder with prompt "{title}"',
                    "-e",
                    '"SUCCESS:" & POSIX path of f',
                    "-e",
                    "on error",
                    "-e",
                    '"CANCEL:"',
                    "-e",
                    "end try",
                ]
                result = run_background_process(
                    cmd,
                    sandbox=False,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=15,
                )
                output = (
                    result.stdout.strip()
                    if result and getattr(result, "stdout", None)
                    else ""
                )
                lines = [line.strip() for line in output.splitlines() if line.strip()]
                for line in lines:
                    if line.startswith("SUCCESS:"):
                        path = line[8:]
                        success = True
                        break
                    elif line.startswith("CANCEL:"):
                        path = ""
                        success = True
                        break
            elif sys.platform == "win32":
                # Windows PowerShell
                safe_title = title.replace("'", "''")
                script = f"""
$isCI = $env:CI -eq 'true' -or $env:GITHUB_ACTIONS -eq 'true' -or $env:TF_BUILD -eq 'true'
if (-not [System.Environment]::UserInteractive -or $isCI) {{
    Write-Error "Native folder picker unavailable in non-interactive or CI environment."
    exit 1
}}
[System.Reflection.Assembly]::LoadWithPartialName('System.windows.forms') | Out-Null
$objForm = New-Object System.Windows.Forms.FolderBrowserDialog
$objForm.Description = '{safe_title}'
$objForm.ShowNewFolderButton = $true
$result = $objForm.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
    Write-Output "SUCCESS:$($objForm.SelectedPath)"
}} else {{
    Write-Output "CANCEL:"
}}
"""
                encoded_script = base64.b64encode(script.encode("utf-16-le")).decode(
                    "ascii"
                )
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded_script,
                ]
                result = run_background_process(
                    cmd, sandbox=False, capture_output=True, text=True, timeout=15
                )
                output = (
                    result.stdout.strip()
                    if result and getattr(result, "stdout", None)
                    else ""
                )
                lines = [line.strip() for line in output.splitlines() if line.strip()]
                for line in lines:
                    if line.startswith("SUCCESS:"):
                        path = line[8:]
                        success = True
                        break
                    elif line.startswith("CANCEL:"):
                        path = ""
                        success = True
                        break
            elif sys.platform.startswith("linux"):
                # Linux Zenity or KDialog native CLI wrappers
                import shutil

                zenity_path = shutil.which("zenity")
                kdialog_path = shutil.which("kdialog")

                if zenity_path:
                    cmd = [
                        "zenity",
                        "--file-selection",
                        "--directory",
                        f"--title={title}",
                    ]
                    result = run_background_process(
                        cmd, sandbox=False, capture_output=True, text=True, timeout=15
                    )
                    output = result.stdout.strip()
                    if result.returncode == 0:
                        path = output
                        success = True
                    elif result.returncode == 1:
                        path = ""
                        success = True
                    else:
                        success = False
                elif kdialog_path:
                    cmd = ["kdialog", "--getexistingdirectory", ".", "--title", title]
                    result = run_background_process(
                        cmd, sandbox=False, capture_output=True, text=True, timeout=15
                    )
                    output = result.stdout.strip()
                    if result.returncode == 0:
                        path = output
                        success = True
                    elif result.returncode == 1:
                        path = ""
                        success = True
                    else:
                        success = False
                else:
                    success = False
            else:
                success = False
        except Exception as e:
            logger.error(f"Error executing native directory dialog: {e}")
            success = False

        if not success:
            # Fallback to manual path input using an accessible NiceGUI dialog
            def _fallback():
                _render_fallback_dialog(
                    title=title,
                    callback=callback,
                    enable_ui_callback=enable_ui_callback,
                )

            if loop and not getattr(loop, "is_closed", lambda: False)():
                try:
                    loop.call_soon_threadsafe(_fallback)
                except RuntimeError:
                    _fallback()
            else:
                _fallback()
            return

        def _on_complete():
            if enable_ui_callback:
                enable_ui_callback()
            if callback:
                callback(path)

        if loop and not getattr(loop, "is_closed", lambda: False)():
            try:
                loop.call_soon_threadsafe(_on_complete)
            except RuntimeError:
                _on_complete()
        else:
            _on_complete()

    from app.core.shared_registry import ContextPropagatingThread

    ContextPropagatingThread(target=_run_dialog, daemon=True).start()
    return fut
