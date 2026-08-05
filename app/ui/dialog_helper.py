"""Helper module for asynchronous directory selection with focus elevation."""

import asyncio
import logging
import os
import sys
import threading

from app.core.env_helper import run_background_process

logger = logging.getLogger(__name__)


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

    try:
        from nicegui.slot import Slot

        stack = Slot.get_stack()
    except Exception:
        stack = None

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
                    cmd, capture_output=True, text=True, check=True
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
                result = run_background_process(cmd, capture_output=True, text=True)
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
                    result = run_background_process(cmd, capture_output=True, text=True)
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
                    result = run_background_process(cmd, capture_output=True, text=True)
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
                try:
                    from nicegui import ui

                    def show_dialog():
                        tid = None
                        try:
                            from nicegui.slot import Slot

                            tid = (
                                id(asyncio.current_task())
                                if asyncio.current_task()
                                else 0
                            )
                            if stack is not None:
                                Slot.stacks[tid] = stack
                        except Exception:
                            tid = None

                        try:
                            if sys.platform.startswith("linux"):
                                ui.notify(
                                    "No native CLI directory picker (Zenity or KDialog) was found. Please install Zenity or KDialog.",
                                    type="warning",
                                )
                            elif sys.platform == "win32":
                                ui.notify(
                                    "Could not open native folder picker (PowerShell may be restricted). Please enter path manually.",
                                    type="warning",
                                )
                            else:
                                ui.notify(
                                    "Could not open native folder picker. Please enter path manually.",
                                    type="warning",
                                )

                            with ui.dialog() as dialog, ui.card().classes("w-96 p-6 manual-fallback-dialog-card"):
                                make_dialog_accessible(dialog, "manual-fallback-dialog-card")
                                ui.label(title).classes("text-lg font-bold mb-4")
                                ui.label(
                                    "Please enter the directory path manually:"
                                ).classes("text-sm mb-4")
                                path_input = ui.input(
                                    label="Folder Path", placeholder="/path/to/folder"
                                ).classes("w-full mb-4")

                                def on_confirm():
                                    p = path_input.value.strip()
                                    if p and os.path.isdir(p):
                                        dialog.close()
                                        if enable_ui_callback:
                                            enable_ui_callback()
                                        if callback:
                                            callback(p)
                                    else:
                                        ui.notify(
                                            "Invalid directory path. Please check if the path exists and is a directory.",
                                            type="negative",
                                        )

                                def on_confirm_key(e):
                                    if e.key == "Enter":
                                        on_confirm()

                                path_input.on("keydown", on_confirm_key)

                                def on_cancel():
                                    dialog.close()
                                    if enable_ui_callback:
                                        enable_ui_callback()
                                    if callback:
                                        callback("")

                                with ui.row().classes("w-full justify-end gap-2"):
                                    ui.button("Cancel", on_click=on_cancel).classes(
                                        "bg-gray-200 text-black"
                                    )
                                    ui.button("OK", on_click=on_confirm).classes(
                                        "bg-blue-500 text-white"
                                    )

                            dialog.open()
                        finally:
                            try:
                                if tid and tid in Slot.stacks:
                                    del Slot.stacks[tid]
                            except Exception:
                                pass

                    if loop:
                        loop.call_soon_threadsafe(show_dialog)
                    else:
                        if enable_ui_callback:
                            enable_ui_callback()
                        if callback:
                            callback("")
                except Exception as ex:
                    logger.error(f"Failed to show manual path dialog: {ex}")
                    if enable_ui_callback:
                        enable_ui_callback()
                    if callback:
                        callback("")

            _fallback()
            return

        def _on_complete():
            tid = None
            try:
                from nicegui.slot import Slot

                tid = id(asyncio.current_task()) if asyncio.current_task() else 0
                if stack is not None:
                    Slot.stacks[tid] = stack
            except Exception:
                tid = None

            try:
                if enable_ui_callback:
                    enable_ui_callback()
                if callback:
                    callback(path)
            finally:
                try:
                    if tid and tid in Slot.stacks:
                        del Slot.stacks[tid]
                except Exception:
                    pass

        if loop:
            loop.call_soon_threadsafe(_on_complete)
        else:
            _on_complete()

    threading.Thread(target=_run_dialog, daemon=True).start()


def make_dialog_accessible(dialog, card_class: str, is_persistent: bool = False, on_escape=None):
    """Make a NiceGUI dialog accessible by managing focus and Escape dismissal."""
    from nicegui import ui

    # Register custom_escape event to cleanly close the dialog
    if on_escape:
        dialog.on('custom_escape', on_escape)
    else:
        dialog.on('custom_escape', dialog.close)

    def handle_value_change(e):
        if e.value:
            # Dialog is opening:
            # 1. Record the current active element in the browser.
            # 2. Wait a short time for the dialog to open, then focus the first title or interactive element.
            ui.run_javascript(f"""
                window._lastTriggeringElement = document.activeElement;
                setTimeout(() => {{
                    const card = document.querySelector('.{card_class}');
                    if (card) {{
                        const title = card.querySelector('h1, h2, h3, h4, h5, h6, .text-h6, .text-lg, .text-xl, .text-2xl, [role="heading"], [aria-label*="Title"], [aria-label*="title"]');
                        if (title) {{
                            if (!title.hasAttribute('tabindex')) {{
                                title.setAttribute('tabindex', '-1');
                            }}
                            title.focus();
                        }} else {{
                            const interactive = card.querySelector('button, [href], input, select, textarea, [tabindex="0"]');
                            if (interactive) {{
                                interactive.focus();
                            }}
                        }}
                    }}
                }}, 150);
            """)
        else:
            # Dialog is closing:
            # Restore focus to the initiating element.
            ui.run_javascript("""
                if (window._lastTriggeringElement && typeof window._lastTriggeringElement.focus === 'function') {
                    window._lastTriggeringElement.focus();
                    window._lastTriggeringElement = null;
                }
            """)

    dialog.on_value_change(handle_value_change)


def preserve_slot_context(func):
    """Decorator to preserve NiceGUI slot state contexts across asynchronous task/thread boundaries."""
    from nicegui.slot import Slot
    import asyncio
    import functools

    try:
        stack = Slot.get_stack()
        if not stack and Slot.stacks:
            stack = next(iter(Slot.stacks.values()))
    except Exception:
        stack = None

    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            tid = id(asyncio.current_task()) if asyncio.current_task() else 0
            if stack is not None and tid:
                Slot.stacks[tid] = stack
            try:
                return await func(*args, **kwargs)
            finally:
                if stack is not None and tid in Slot.stacks:
                    try:
                        del Slot.stacks[tid]
                    except Exception:
                        pass
        return wrapper
    else:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tid = id(asyncio.current_task()) if asyncio.current_task() else 0
            if stack is not None and tid:
                Slot.stacks[tid] = stack
            try:
                return func(*args, **kwargs)
            finally:
                if stack is not None and tid in Slot.stacks:
                    try:
                        del Slot.stacks[tid]
                    except Exception:
                        pass
        return wrapper


