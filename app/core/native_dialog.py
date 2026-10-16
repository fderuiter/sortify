"""Native OS directory selection dialog helper using Base64-encoded communication.

Prevents character corruption, console codepage mismatches, and application crashes
when selecting folder paths containing non-ASCII, accented, or international characters on Windows.
"""

import base64
import logging
import sys
from typing import Optional

from app.core.env_helper import (
    get_cleaned_env,
    get_subprocess_startupinfo,
    run_background_process,
)

logger = logging.getLogger(__name__)


class NativeDialogError(Exception):
    """Raised when native directory selection dialog fails to launch or run."""

    pass


def encode_path_to_base64(path: str) -> str:
    """Encode a UTF-8 path string into a Base64 ASCII string."""
    if not path:
        return ""
    utf8_bytes = path.encode("utf-8")
    base64_bytes = base64.b64encode(utf8_bytes)
    return base64_bytes.decode("ascii")


def decode_base64_to_path(base64_str: str) -> str:
    """Decode a Base64 ASCII string back into a UTF-8 path string."""
    clean_str = base64_str.strip()
    if not clean_str:
        return ""
    decoded_bytes = base64.b64decode(clean_str)
    return decoded_bytes.decode("utf-8")


def select_native_directory_powershell(
    initial_dir: Optional[str] = None,
) -> Optional[str]:
    """Launch Windows native FolderBrowserDialog via PowerShell with Base64 ASCII output.

    Parameters
    ----------
    initial_dir : str, optional
        Initial directory path to open in the folder browser.

    Returns
    -------
    str or None
        Decoded UTF-8 directory path string if selected, or None if cancelled by user.

    Raises
    ------
    NativeDialogError
        If PowerShell fails to launch, exits with non-zero code, or encounters error.
    """
    initial_dir_b64 = encode_path_to_base64(initial_dir) if initial_dir else ""

    # PowerShell script that uses System.Windows.Forms.FolderBrowserDialog and returns Base64 UTF-8 path
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dialog.Description = 'Select Target Directory'; "
        "$dialog.ShowNewFolderButton = $true; "
        f"$initB64 = '{initial_dir_b64}'; "
        "if ($initB64 -ne '') { "
        "  try { "
        "    $initBytes = [System.Convert]::FromBase64String($initB64); "
        "    $initPath = [System.Text.Encoding]::UTF8.GetString($initBytes); "
        "    if (Test-Path -Path $initPath) { $dialog.SelectedPath = $initPath } "
        "  } catch {} "
        "}; "
        "$res = $dialog.ShowDialog(); "
        "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { "
        "  $bytes = [System.Text.Encoding]::UTF8.GetBytes($dialog.SelectedPath); "
        "  $base64 = [System.Convert]::ToBase64String($bytes); "
        "  Write-Output $base64 "
        "}"
    )

    cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps_script,
    ]

    try:
        # Sanitize PyInstaller environment variables
        env = get_cleaned_env()
        startupinfo = get_subprocess_startupinfo()

        res = run_background_process(
            cmd,
            sandbox=False,
            capture_output=True,
            text=True,
            env=env,
            startupinfo=startupinfo,
            timeout=120,
        )

        if res.returncode != 0:
            err_msg = (
                res.stderr.strip() if res.stderr else f"Exit code {res.returncode}"
            )
            raise NativeDialogError(f"PowerShell folder dialog failed: {err_msg}")

        b64_output = res.stdout.strip()
        if not b64_output:
            # User cancelled selection
            return None

        decoded_path = decode_base64_to_path(b64_output)
        return decoded_path

    except NativeDialogError:
        raise
    except Exception as exc:
        raise NativeDialogError(
            f"Failed to execute native directory selection: {exc}"
        ) from exc


def select_native_directory(initial_dir: Optional[str] = None) -> Optional[str]:
    """Launch platform native directory selection dialog using Base64 ASCII communication.

    On Windows, invokes PowerShell FolderBrowserDialog with Base64 output.
    On other platforms or if native tool fails, raises NativeDialogError.

    Parameters
    ----------
    initial_dir : str, optional
        Initial directory path to open in the folder browser.

    Returns
    -------
    str or None
        Decoded UTF-8 directory path string if selected, or None if cancelled by user.

    Raises
    ------
    NativeDialogError
        If native folder dialog utility is unsupported or fails to launch.
    """
    if sys.platform == "win32":
        return select_native_directory_powershell(initial_dir)
    else:
        if sys.platform == "darwin":
            try:
                cmd = [
                    "osascript",
                    "-e",
                    'POSIX path of (choose folder with prompt "Select Target Directory")',
                ]
                res = run_background_process(
                    cmd, sandbox=False, capture_output=True, text=True, timeout=60
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
                return None
            except Exception as exc:
                raise NativeDialogError(
                    f"macOS native folder dialog failed: {exc}"
                ) from exc
        elif sys.platform == "linux":
            try:
                cmd = [
                    "zenity",
                    "--file-selection",
                    "--directory",
                    "--title=Select Target Directory",
                ]
                res = run_background_process(
                    cmd, sandbox=False, capture_output=True, text=True, timeout=60
                )
                if res.returncode == 0 and res.stdout.strip():
                    return res.stdout.strip()
                return None
            except Exception as exc:
                raise NativeDialogError(
                    f"Linux native folder dialog failed: {exc}"
                ) from exc

        raise NativeDialogError(
            f"Native folder selection not supported on platform '{sys.platform}'"
        )
