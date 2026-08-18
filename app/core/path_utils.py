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
        raise ValueError(f"Target path '{target_path}' cannot be an absolute path.")

    # Check for directory traversal segments (..)
    segments = target_path.replace("\\", "/").split("/")
    if ".." in segments:
        raise ValueError(
            f"Target path '{target_path}' cannot contain directory traversal segments."
        )

    # Validate each individual segment against platform naming rules
    for segment in segments:
        if not segment:
            continue

        if segment.endswith(" ") or segment.endswith("."):
            raise ValueError(
                f"Target path '{target_path}' contains segment '{segment}' with trailing space or period."
            )

        base_name = segment.upper().split(".")[0]
        if base_name in RESERVED_NAMES:
            raise ValueError(
                f"Target path '{target_path}' contains reserved device name '{segment}'."
            )


def sanitize_name(name: str) -> str:
    """Sanitize a file or folder name for Windows.

    Strips illegal characters and appends _safe to reserved names.
    """
    if not name:
        return name

    import unicodedata

    name = unicodedata.normalize("NFC", name)
    name = name.replace("\x00", "")
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)

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


def sanitize_folder_key(key: str) -> tuple[str, bool]:
    """Sanitize a folder hierarchy key to be OS-safe and clean.

    Strips path traversal elements, removes null bytes, replaces illegal
    filesystem characters and slashes with safe delimiters, strips trailing
    dots/spaces, and maps OS-reserved names to safe variants.

    Returns (safe_key, transformed).
    """
    if not isinstance(key, str) or not key:
        return "Unnamed_safe", True

    import unicodedata

    orig_key = key
    s = unicodedata.normalize("NFC", key)

    # Remove null bytes and replace control characters
    s = s.replace("\x00", "")
    s = re.sub(r"[\x00-\x1f\x7f]", "_", s)

    # Split by slashes/backslashes to clean path traversal segments
    parts = re.split(r"[/\\]+", s)
    clean_parts = [p for p in parts if p not in ("..", ".", "")]

    if not clean_parts:
        s = "Unnamed_safe"
    else:
        s = "_".join(clean_parts)

    # Replace remaining illegal chars (<>:"|?*) with _
    escaped_chars = "".join(re.escape(c) for c in ILLEGAL_PATH_CHARS_SET)
    s = re.sub(f"[{escaped_chars}]", "_", s)

    # Strip trailing periods and spaces
    s = s.rstrip(". ")

    # Check for reserved OS keyword names
    upper_name = s.upper()
    base_name = upper_name.split(".")[0]
    if base_name in RESERVED_NAMES:
        parts = s.split(".")
        parts[0] = parts[0] + "_safe"
        s = ".".join(parts)

    if not s:
        s = "Unnamed_safe"

    transformed = s != orig_key
    return s, transformed


def _disambiguate_key(existing_keys, key: str, is_file: bool = True) -> str:
    """Disambiguate a key if it already exists in existing_keys."""
    if key not in existing_keys:
        return key

    if is_file and "." in key and not key.startswith("."):
        parts = key.rsplit(".", 1)
        stem, ext = parts[0], "." + parts[1]
    else:
        stem, ext = key, ""

    counter = 1
    while True:
        candidate = f"{stem}_{counter}{ext}"
        if candidate not in existing_keys:
            return candidate
        counter += 1


def _merge_plan_dicts(target_dict: dict, source_dict: dict) -> list[str]:
    """Recursively merge source_dict into target_dict.

    Returns list of warning messages for disambiguated items.
    """
    warnings = []
    for k, v in source_dict.items():
        if k not in target_dict:
            target_dict[k] = v
        else:
            existing_val = target_dict[k]
            is_existing_subfolder = (
                isinstance(existing_val, dict)
                and existing_val.get("__type__") not in ("file", "directory")
            )
            is_v_subfolder = (
                isinstance(v, dict)
                and v.get("__type__") not in ("file", "directory")
            )

            if is_existing_subfolder and is_v_subfolder:
                sub_warns = _merge_plan_dicts(existing_val, v)
                warnings.extend(sub_warns)
            else:
                is_file = (
                    isinstance(v, dict) and v.get("__type__") == "file"
                ) or v is None
                new_k = _disambiguate_key(target_dict, k, is_file=is_file)
                if isinstance(v, dict) and "target_filename" in v:
                    v["target_filename"] = new_k
                target_dict[new_k] = v
                warnings.append(
                    f"Disambiguated target item '{k}' to '{new_k}' due to merging conflict"
                )
    return warnings


def sanitize_plan(plan: dict) -> tuple[dict, list[str]]:
    """Recursively sanitize folder keys and file targets in a plan dictionary.

    Returns (sanitized_plan, warnings).
    """
    if not isinstance(plan, dict):
        return plan, []

    sanitized_plan = {}
    warnings = []

    for key, content in plan.items():
        if content is None or (
            isinstance(content, dict) and content.get("__type__") == "file"
        ):
            new_content = dict(content) if isinstance(content, dict) else {}

            if isinstance(new_content, dict) and "target_filename" in new_content:
                orig_tf = new_content["target_filename"]
                safe_tf = sanitize_name(orig_tf)
                if orig_tf != safe_tf:
                    new_content["target_filename"] = safe_tf
                    warnings.append(f"Sanitized file target '{orig_tf}' to '{safe_tf}'")

            safe_file_key = sanitize_name(key)
            if key != safe_file_key:
                warnings.append(f"Sanitized file key '{key}' to '{safe_file_key}'")
                new_file_key = safe_file_key
            else:
                new_file_key = key

            if new_file_key in sanitized_plan:
                new_file_key = _disambiguate_key(
                    sanitized_plan, new_file_key, is_file=True
                )
                if isinstance(new_content, dict) and "target_filename" in new_content:
                    new_content["target_filename"] = new_file_key

            sanitized_plan[new_file_key] = new_content

        elif isinstance(content, dict) and content.get("__type__") == "directory":
            safe_key, transformed = sanitize_folder_key(key)
            if transformed:
                warnings.append(f"Sanitized folder key '{key}' to '{safe_key}'")

            if safe_key not in sanitized_plan:
                dir_content = dict(content)
                if "source_path" in dir_content:
                    dir_content["source_path"] = safe_key
                sanitized_plan[safe_key] = dir_content

        elif isinstance(content, dict):
            safe_key, transformed = sanitize_folder_key(key)
            if transformed:
                warnings.append(f"Sanitized folder key '{key}' to '{safe_key}'")

            sub_sanitized, sub_warns = sanitize_plan(content)
            warnings.extend(sub_warns)

            if safe_key in sanitized_plan:
                existing = sanitized_plan[safe_key]
                if (
                    isinstance(existing, dict)
                    and existing.get("__type__") not in ("file", "directory")
                ):
                    merge_warns = _merge_plan_dicts(existing, sub_sanitized)
                    warnings.extend(merge_warns)
                else:
                    safe_key = _disambiguate_key(
                        sanitized_plan, safe_key, is_file=False
                    )
                    sanitized_plan[safe_key] = sub_sanitized
            else:
                sanitized_plan[safe_key] = sub_sanitized

        else:
            sanitized_plan[key] = content

    return sanitized_plan, warnings


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


def is_path_too_long(path: str, limit: int = 260) -> bool:
    """Check if the normalized/absolute target path equals or exceeds 260 characters."""
    if not path:
        return False
    normalized_path = os.path.abspath(path)
    return len(normalized_path) >= limit


def is_junction_path(path: str) -> bool:
    """Check if a path is an NTFS directory junction."""
    if not path:
        return False
    try:
        return os.path.isjunction(path)
    except (AttributeError, OSError):
        return False


def is_junction_entry(entry) -> bool:
    """Check if a DirEntry or path is an NTFS directory junction."""
    if entry is None:
        return False
    try:
        if hasattr(entry, "is_junction") and entry.is_junction():
            return True
    except (AttributeError, OSError):
        pass
    try:
        path = entry.path if hasattr(entry, "path") else str(entry)
        return os.path.isjunction(path)
    except (AttributeError, OSError):
        return False

