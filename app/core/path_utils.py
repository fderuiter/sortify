"""Utility functions for handling paths and sanitizing filenames."""

import os
import re
import sys
import tempfile
from pathlib import Path

RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}

ILLEGAL_PATH_CHARS_SET = set('<>:"|?*')
ILLEGAL_NAME_CHARS_SET = ILLEGAL_PATH_CHARS_SET | set("/\\")


def is_packaged() -> bool:
    """Check if the application is running in a frozen/packaged bundle (e.g., PyInstaller)."""
    return getattr(sys, "frozen", False)


def get_base_path(caller_file_path: str = None) -> str:
    """Get the standard base path of the application.

    Compatible with frozen/packaged execution and local development.
    """
    if is_packaged():
        return os.path.dirname(sys.executable)
    else:
        file_path = caller_file_path or __file__
        return os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(file_path)))
        )


def get_session_base_dir() -> Path:
    """Get the standard base directory for sessions."""
    return Path(tempfile.gettempdir()) / "autosorter_sessions"


def setup_session_directory(session_id: str = None) -> tuple[str, Path]:
    """Set up and return the session ID and standard session database directory."""
    import uuid

    if not session_id:
        session_id = str(uuid.uuid4())
    session_dir = get_session_base_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_id, session_dir


def resolve_db_crypto(db_path: Path | str):
    """Resolve and return the standard SessionCrypto instance for a given database path."""
    from app.core.crypto import SessionCrypto

    db_path_obj = Path(db_path)
    key_path = db_path_obj.parent / "secret.key"
    return SessionCrypto(key_path, db_path_obj)


def validate_target_path(target_path: str, keyword: str = None) -> None:
    """Validate a target folder path for safety and correct structure.

    Raises ValueError if invalid.
    """
    if not isinstance(target_path, str):
        suffix = f" for keyword '{keyword}'" if keyword else ""
        raise ValueError(f"Target path{suffix} must be a string.")

    # Check for illegal OS characters
    if any(char in ILLEGAL_PATH_CHARS_SET for char in target_path):
        raise ValueError(f"Target path '{target_path}' contains illegal characters.")

    # Check for absolute path roots (/ or \)
    if target_path.startswith("/") or target_path.startswith("\\"):
        raise ValueError(f"Target path '{target_path}' is an absolute path.")

    # Check for directory traversal segments (..)
    segments = target_path.replace("\\", "/").split("/")
    if ".." in segments:
        raise ValueError(
            f"Target path '{target_path}' contains directory traversal segments."
        )


def sanitize_name(name: str) -> str:
    """Sanitize a file or folder name for Windows.

    Strips illegal characters and appends _safe to reserved names.
    """
    if not name:
        return name

    # Replace illegal characters with underscore (or just strip them)
    # The requirement says "strip illegal path characters" but in the example:
    # "Data: Archives" -> "Data_ Archives" so we should replace `:` with `_`.
    # Let's replace `< > : " / \\ | ? *` with `_`.
    escaped_chars = "".join(re.escape(c) for c in ILLEGAL_NAME_CHARS_SET)
    safe_name = re.sub(f"[{escaped_chars}]", "_", name)

    # Strip trailing periods and spaces (also problematic on Windows)
    safe_name = safe_name.rstrip(". ")

    # Check if the name matches a reserved name (case-insensitive, optionally with an extension)
    upper_name = safe_name.upper()
    base_name = upper_name.split(".")[0]

    if base_name in RESERVED_NAMES:
        # Need to append _safe suffix. For "CON" -> "CON_safe".
        # If there's an extension, e.g. "CON.txt" -> "CON_safe.txt"?
        # The scenario says "CON" -> "CON_safe".

        # Let's preserve the original casing and just append _safe to the base name
        parts = safe_name.split(".")
        parts[0] = parts[0] + "_safe"
        safe_name = ".".join(parts)

    if not safe_name:
        safe_name = "Unnamed_safe"

    return safe_name


def is_valid_name(name: str) -> bool:
    """Check if a file or folder name is valid for Windows."""
    if not name:
        return False

    if any(char in ILLEGAL_NAME_CHARS_SET for char in name):
        return False

    if name != name.rstrip(". "):
        return False

    base_name = name.upper().split(".")[0]
    if base_name in RESERVED_NAMES:
        return False

    return True
