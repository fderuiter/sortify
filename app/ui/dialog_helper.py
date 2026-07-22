"""Helper module for asynchronous directory selection with focus elevation."""

import subprocess
import sys
import threading
from tkinter import filedialog


def ask_directory_async(
    parent, title, callback, disable_ui_callback, enable_ui_callback
):
    """Launch native OS directory selector asynchronously to prevent blocking the web UI execution thread.

    Force the native OS window manager to bring the newly opened folder picker window to the front.
    """
    if disable_ui_callback:
        disable_ui_callback()

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
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
                cmd = ["powershell", "-Command", script]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, startupinfo=startupinfo
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
        except Exception:
            success = False

        if not success:
            # Fallback
            def _fallback():
                parent.attributes("-topmost", True)
                parent.focus_force()
                selected = filedialog.askdirectory(parent=parent, title=title)
                parent.attributes("-topmost", False)
                if enable_ui_callback:
                    enable_ui_callback()
                if callback:
                    callback(selected)

            parent.after(0, _fallback)
            return

        def _on_complete():
            if enable_ui_callback:
                enable_ui_callback()
            if callback:
                callback(path)

        parent.after(0, _on_complete)

    threading.Thread(target=_run_dialog, daemon=True).start()
