"""Helper module for asynchronous directory selection with focus elevation."""

import asyncio
import logging
import sys

from app.core.env_helper import run_background_process
from app.ui.report_helper import serve_or_download_report, show_file_error_dialog
from app.ui.tokens import TOKENS

logger = logging.getLogger(__name__)

__all__ = [
    "ask_directory_async",
    "get_dialog_card_classes",
    "serve_or_download_report",
    "show_file_error_dialog",
]

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


def ask_directory_async(
    parent, title, callback, disable_ui_callback, enable_ui_callback
):
    """Launch native OS directory selector asynchronously to prevent blocking the web UI execution thread.

    Force the native OS window manager to bring the newly opened folder picker window to the front.
    """
    if disable_ui_callback:
        disable_ui_callback()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    def _run_dialog():
        path = ""
        success = False

        try:
            if sys.platform == "darwin":
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
                    cmd, sandbox=False, capture_output=True, text=True, check=True
                )
                output = result.stdout.strip()
                if output.startswith("SUCCESS:"):
                    path = output[8:]
                    success = True
                elif output.startswith("CANCEL:"):
                    path = ""
                    success = True
                else:
                    success = False
            elif sys.platform == "win32":
                # Windows PowerShell
                script = f"""
[System.Reflection.Assembly]::LoadWithPartialName('System.windows.forms') | Out-Null;
$objForm = New-Object System.Windows.Forms.FolderBrowserDialog;
$objForm.Description = '{title}';
$objForm.ShowNewFolderButton = $true;
$result = $objForm.ShowDialog();
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{
    Write-Output "SUCCESS:$($objForm.SelectedPath)"
}} else {{
    Write-Output "CANCEL:"
}}
"""
                cmd = ["powershell", "-Command", script]
                result = run_background_process(
                    cmd, sandbox=False, capture_output=True, text=True
                )
                output = result.stdout.strip()
                if output.startswith("SUCCESS:"):
                    path = output[8:]
                    success = True
                elif output.startswith("CANCEL:"):
                    path = ""
                    success = True
                else:
                    success = False
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
                        cmd, sandbox=False, capture_output=True, text=True
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
                        cmd, sandbox=False, capture_output=True, text=True
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
            # Fallback to manual path input using a NiceGUI dialog
            def _fallback():
                if enable_ui_callback:
                    enable_ui_callback()
                if callback:
                    callback("")

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
