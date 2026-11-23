"""Native OS directory and file selection dialog helpers using Base64-encoded communication.

Prevents character corruption, console codepage mismatches, and application crashes
when selecting folder or file paths containing non-ASCII, accented, or international characters on Windows.
"""

import base64
import binascii
import logging
import os
import subprocess
import sys
from collections.abc import Sequence

from app.core.env_helper import (
    get_cleaned_env,
    get_subprocess_startupinfo,
    run_background_process,
)

logger = logging.getLogger(__name__)

RULE_IMPORT_EXTENSIONS: tuple[str, ...] = (
    ".hazelrules",
    ".plist",
    ".ini",
    ".xml",
    ".json",
    ".dropit",
)


class NativeDialogError(Exception):
    """Raised when a native directory or file selection dialog fails to launch or run."""


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


def _normalize_extensions(extensions: Sequence[str] | None) -> list[str]:
    """Normalize file extension filters into lowercase leading-dot suffixes."""
    raw = extensions if extensions is not None else RULE_IMPORT_EXTENSIONS
    cleaned: list[str] = []
    for ext in raw:
        item = str(ext).strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = f".{item}"
        if all(ch.isalnum() or ch in "._-" for ch in item) and item not in cleaned:
            cleaned.append(item)
    return cleaned


def select_native_directory_powershell(
    initial_dir: str | None = None,
) -> str | None:
    """Launch Windows native FolderBrowserDialog via PowerShell with Base64 ASCII output.

    Parameters
    ----------
    initial_dir : str | None, optional
        Initial directory path to open in the folder browser.

    Returns
    -------
    str | None
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
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        binascii.Error,
        ValueError,
    ) as exc:
        raise NativeDialogError(
            f"Failed to execute native directory selection: {exc}"
        ) from exc


def select_native_directory(initial_dir: str | None = None) -> str | None:
    """Launch platform native directory selection dialog using Base64 ASCII communication.

    On Windows, invokes PowerShell FolderBrowserDialog with Base64 output.
    On other platforms or if native tool fails, raises NativeDialogError.

    Parameters
    ----------
    initial_dir : str | None, optional
        Initial directory path to open in the folder browser.

    Returns
    -------
    str | None
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
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
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
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise NativeDialogError(
                    f"Linux native folder dialog failed: {exc}"
                ) from exc

        raise NativeDialogError(
            f"Native folder selection not supported on platform '{sys.platform}'"
        )


def select_native_file_powershell(
    initial_dir: str | None = None,
    title: str = "Select Rule File",
    extensions: Sequence[str] | None = None,
) -> str | None:
    """Launch Windows native OpenFileDialog via PowerShell with Base64 UTF-8 communication.

    Parameters
    ----------
    initial_dir : str | None, optional
        Initial directory path to open in the file dialog.
    title : str, optional
        Dialog window title.
    extensions : Sequence[str] | None, optional
        Allowed file extensions (defaults to ``RULE_IMPORT_EXTENSIONS``).

    Returns
    -------
    str | None
        Decoded UTF-8 file path string if selected, or None if cancelled by user.

    Raises
    ------
    NativeDialogError
        If PowerShell fails to launch, exits with non-zero code, or encounters an error.
    """
    clean_exts = _normalize_extensions(extensions)
    if clean_exts:
        wildcards = ";".join(f"*{ext}" for ext in clean_exts)
        filter_spec = f"Rule Files ({wildcards})|{wildcards}|All Files (*.*)|*.*"
    else:
        filter_spec = "All Files (*.*)|*.*"

    init_b64 = encode_path_to_base64(initial_dir) if initial_dir else ""
    title_b64 = encode_path_to_base64(title or "Select Rule File")
    filter_b64 = encode_path_to_base64(filter_spec)

    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dlg = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dlg.CheckFileExists = $true; "
        "$dlg.Multiselect = $false; "
        f"$titleBytes = [System.Convert]::FromBase64String('{title_b64}'); "
        "$dlg.Title = [System.Text.Encoding]::UTF8.GetString($titleBytes); "
        f"$filterBytes = [System.Convert]::FromBase64String('{filter_b64}'); "
        "$dlg.Filter = [System.Text.Encoding]::UTF8.GetString($filterBytes); "
        f"$dirB64 = '{init_b64}'; "
        "if ($dirB64 -ne '') { "
        "  try { "
        "    $dirBytes = [System.Convert]::FromBase64String($dirB64); "
        "    $dirPath = [System.Text.Encoding]::UTF8.GetString($dirBytes); "
        "    if (Test-Path -Path $dirPath) { $dlg.InitialDirectory = $dirPath } "
        "  } catch {} "
        "}; "
        "$choice = $dlg.ShowDialog(); "
        "if ($choice -eq [System.Windows.Forms.DialogResult]::OK) { "
        "  $outBytes = [System.Text.Encoding]::UTF8.GetBytes($dlg.FileName); "
        "  Write-Output ([System.Convert]::ToBase64String($outBytes)) "
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
        res = run_background_process(
            cmd,
            sandbox=False,
            capture_output=True,
            text=True,
            env=get_cleaned_env(),
            startupinfo=get_subprocess_startupinfo(),
            timeout=120,
        )
        if res.returncode != 0:
            detail = res.stderr.strip() if res.stderr else f"Exit code {res.returncode}"
            raise NativeDialogError(f"PowerShell file dialog failed: {detail}")

        encoded_file = res.stdout.strip()
        if not encoded_file:
            return None
        return decode_base64_to_path(encoded_file)
    except NativeDialogError:
        raise
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeError,
        binascii.Error,
        ValueError,
    ) as exc:
        raise NativeDialogError(
            f"Failed to execute native file selection: {exc}"
        ) from exc


def select_native_file(
    initial_dir: str | None = None,
    title: str = "Select Rule File",
    extensions: Sequence[str] | None = None,
) -> str | None:
    """Launch platform native file selection dialog using offline local OS helpers.

    Parameters
    ----------
    initial_dir : str | None, optional
        Initial directory path to open in the file picker.
    title : str, optional
        Dialog window title or prompt.
    extensions : Sequence[str] | None, optional
        Allowed file extensions (defaults to ``RULE_IMPORT_EXTENSIONS``).

    Returns
    -------
    str | None
        Selected UTF-8 file path string, or None if cancelled by user.

    Raises
    ------
    NativeDialogError
        If native file dialog utility is unavailable or fails to launch.
    """
    clean_exts = _normalize_extensions(extensions)
    safe_title = (title or "Select Rule File").replace('"', "'").replace("\n", " ")

    if sys.platform == "win32":
        return select_native_file_powershell(
            initial_dir=initial_dir,
            title=safe_title,
            extensions=clean_exts,
        )

    env = get_cleaned_env()

    if sys.platform == "darwin":
        script = f'POSIX path of (choose file with prompt "{safe_title}")'
        try:
            res = run_background_process(
                ["osascript", "-e", script],
                sandbox=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            if res.returncode == 0:
                chosen = res.stdout.strip()
                return chosen or None
            stderr_lower = (res.stderr or "").lower()
            if "user canceled" in stderr_lower or "-128" in stderr_lower:
                return None
            raise NativeDialogError(
                f"macOS native file dialog failed: {res.stderr.strip() or f'Exit code {res.returncode}'}"
            )
        except NativeDialogError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise NativeDialogError(f"macOS native file dialog failed: {exc}") from exc

    if sys.platform == "linux":
        zenity_cmd = [
            "zenity",
            "--file-selection",
            f"--title={safe_title}",
        ]
        if initial_dir:
            norm_dir = os.path.abspath(initial_dir).rstrip(os.sep) + os.sep
            zenity_cmd.append(f"--filename={norm_dir}")
        if clean_exts:
            patterns = " ".join(f"*{ext}" for ext in clean_exts)
            zenity_cmd.append(f"--file-filter=Rule Files ({patterns}) | {patterns}")
            zenity_cmd.append("--file-filter=All Files | *")

        try:
            res = run_background_process(
                zenity_cmd,
                sandbox=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            if res.returncode == 0:
                chosen = res.stdout.strip()
                return chosen or None
            if res.returncode == 1 and not (
                res.stderr and "cannot open display" in res.stderr.lower()
            ):
                return None
            raise NativeDialogError(
                f"Linux native file dialog failed: {res.stderr.strip() or f'Exit code {res.returncode}'}"
            )
        except NativeDialogError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise NativeDialogError(f"Linux native file dialog failed: {exc}") from exc

    raise NativeDialogError(
        f"Native file selection not supported on platform '{sys.platform}'"
    )
