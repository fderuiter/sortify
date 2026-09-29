"""Utility functions for handling paths and sanitizing filenames."""

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

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

_BOUND_LEFT = r"(?:^|(?<=[^a-zA-Z0-9]))"
_BOUND_RIGHT = r"(?:$|(?=[^a-zA-Z0-9]))"

PII_FILENAME_PATTERNS = [
    # SSN pattern
    re.compile(_BOUND_LEFT + r"\d{3}[-.\s]?\d{2}[-.\s]?\d{4}" + _BOUND_RIGHT),
    # Credit Card pattern
    re.compile(_BOUND_LEFT + r"(?:\d[ -]*?){13,16}" + _BOUND_RIGHT),
    # Email pattern
    re.compile(_BOUND_LEFT + r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" + _BOUND_RIGHT),
    # Phone number pattern
    re.compile(_BOUND_LEFT + r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}" + _BOUND_RIGHT),
    # Health ID / Patient ID / Medical Record patterns
    re.compile(r"(?i)" + _BOUND_LEFT + r"(?:MRN|PATIENT[ _-]?ID|SUBJECT[ _-]?ID|MED[ _-]?REC|HCID)[ _-]?#?:?\s*[A-Za-z0-9-]+" + _BOUND_RIGHT),
    re.compile(r"(?i)" + _BOUND_LEFT + r"(?:PATIENT|SUBJECT)[ _-]?\d+" + _BOUND_RIGHT),
    re.compile(r"(?i)(Confidential Medical Report|Diagnosis:[^\n]*)"),
]


def _split_name_ext(name: str) -> tuple[str, str, bool]:
    """Split a name into (stem, ext, valid_ext)."""
    if name.startswith(".") and re.match(r"^\.[a-zA-Z0-9]{1,5}$", name):
        return "", name, True
    last_component = re.split(r"[/\\]+", name)[-1]
    _, ext = os.path.splitext(last_component)
    valid_ext = bool(ext and re.match(r"^\.[a-zA-Z0-9]{1,5}$", ext))
    if valid_ext:
        stem = name[:-len(ext)]
        return stem, ext, True
    return name, "", False


def scrub_pii_from_filename(name: str) -> str:
    """Scrub PII expressions (SSN, credit card, email, phone number, health/patient IDs) and secret credentials from a filename or folder name."""
    if not isinstance(name, str) or not name:
        return name if isinstance(name, str) else ""

    from app.core.text_utils import sanitize_secret_patterns

    stem, ext, valid_ext = _split_name_ext(name)

    if valid_ext:
        scrubbed = sanitize_secret_patterns(stem)
        for pattern in PII_FILENAME_PATTERNS:
            scrubbed = pattern.sub(" ", scrubbed)
        scrubbed = re.sub(r"_[ \t]*_", "_", scrubbed)
        scrubbed = re.sub(r"[ \t]+", " ", scrubbed).strip()
        scrubbed = re.sub(r"_{2,}", "_", scrubbed)
        result = (scrubbed + ext) if scrubbed else ext
    else:
        scrubbed = sanitize_secret_patterns(name)
        for pattern in PII_FILENAME_PATTERNS:
            scrubbed = pattern.sub(" ", scrubbed)
        scrubbed = re.sub(r"_[ \t]*_", "_", scrubbed)
        scrubbed = re.sub(r"[ \t]+", " ", scrubbed).strip()
        scrubbed = re.sub(r"_{2,}", "_", scrubbed)
        result = scrubbed

    return result


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

    Strips secret patterns, PII expressions, illegal characters, and appends _safe to reserved names.
    """
    if not name:
        return name

    import unicodedata

    name = scrub_pii_from_filename(name)
    if not name or not name.strip():
        return "Unnamed_safe"

    stem, ext, valid_ext = _split_name_ext(name)

    target_text = stem if valid_ext else name
    target_text = unicodedata.normalize("NFC", target_text)
    target_text = target_text.replace("\x00", "")
    target_text = re.sub(r"[\x00-\x1f\x7f]", "_", target_text)

    escaped_chars = "".join(re.escape(c) for c in ILLEGAL_NAME_CHARS_SET)
    safe_text = re.sub(f"[{escaped_chars}]", "_", target_text)

    safe_text = safe_text.rstrip(". ")
    clean_stem = safe_text.strip(". _")

    if not clean_stem:
        return f"Unnamed_safe{ext}" if valid_ext else "Unnamed_safe"

    upper_name = clean_stem.upper()
    if upper_name in RESERVED_NAMES:
        clean_stem = clean_stem + "_safe"

    if valid_ext:
        return f"{clean_stem}{ext}"
    else:
        return clean_stem


def sanitize_folder_key(key: str) -> tuple[str, bool]:
    """Sanitize a folder hierarchy key to be OS-safe and clean.

    Strips secret patterns, PII expressions, path traversal elements, removes null bytes,
    replaces illegal filesystem characters and slashes with safe delimiters,
    strips trailing dots/spaces, and maps OS-reserved names to safe variants.

    Returns (safe_key, transformed).
    """
    if not isinstance(key, str) or not key:
        return "Unnamed_safe", True

    import unicodedata

    from app.core.text_utils import sanitize_secret_patterns

    orig_key = key
    key = sanitize_secret_patterns(key)
    key = scrub_pii_from_filename(key)

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

    if not s or not s.strip(" ._"):
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
                if isinstance(v, dict):
                    if "target_filename" in v:
                        v["target_filename"] = new_k
                    v["confirmed"] = True
                target_dict[new_k] = v
                warnings.append(
                    f"Disambiguated target item '{k}' to '{new_k}' due to merging conflict"
                )
    return warnings


def _sanitize_plan_key(key: str, is_file: bool = False) -> tuple[str, bool]:
    """Sanitize a plan dictionary key, handling relative paths with slashes segment-by-segment."""
    if not isinstance(key, str) or not key:
        return "Unnamed_safe", True

    if "/" in key or "\\" in key:
        delim = "/" if "/" in key else "\\"
        parts = key.replace("\\", "/").split("/")
        clean_parts = []
        for i, part in enumerate(parts):
            if i == len(parts) - 1 and is_file:
                clean_parts.append(sanitize_name(part))
            else:
                k, _ = sanitize_folder_key(part)
                clean_parts.append(k)
        res = delim.join(clean_parts)
        return res, res != key
    else:
        if is_file:
            res = sanitize_name(key)
            return res, res != key
        else:
            return sanitize_folder_key(key)


def sanitize_plan(plan: Any) -> tuple[dict, list[str]]:
    """Recursively sanitize folder keys, leaf file names, and target filenames in a plan dictionary or SortingPlan.

    Returns (sanitized_plan, warnings).
    """
    if hasattr(plan, "plan") and isinstance(plan.plan, dict):
        plan = plan.plan
    elif hasattr(plan, "model_dump") and not isinstance(plan, dict):
        plan = plan.model_dump()

    if not isinstance(plan, dict):
        return plan, []

    from app.core.text_utils import contains_secrets

    sanitized_plan = {}
    warnings = []

    def _is_file_node(obj):
        if isinstance(obj, BaseModel):
            t = getattr(obj, "node_type", None) or getattr(obj, "__type__", None)
            return t == "file"
        if isinstance(obj, dict):
            return obj.get("__type__") == "file"
        return False

    def _is_dir_node(obj):
        if isinstance(obj, BaseModel):
            t = getattr(obj, "node_type", None) or getattr(obj, "__type__", None)
            return t == "directory"
        if isinstance(obj, dict):
            return obj.get("__type__") == "directory"
        return False

    for key, content in plan.items():
        if content is None:
            safe_file_key, transformed = _sanitize_plan_key(key, is_file=True)
            if transformed:
                warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")
            if safe_file_key in sanitized_plan:
                safe_file_key = _disambiguate_key(
                    sanitized_plan, safe_file_key, is_file=True
                )
            sanitized_plan[safe_file_key] = None

        elif _is_file_node(content):
            safe_file_key, transformed = _sanitize_plan_key(key, is_file=True)
            if transformed:
                warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")

            file_content = dict(content) if isinstance(content, dict) else content
            if isinstance(file_content, dict) and "target_filename" in file_content and file_content["target_filename"]:
                old_tf = file_content["target_filename"]
                if contains_secrets(old_tf) or scrub_pii_from_filename(old_tf) != old_tf:
                    leaf_tf = re.split(r"[/\\]+", old_tf)[-1]
                    new_tf = sanitize_name(leaf_tf)
                    if new_tf != old_tf:
                        file_content["target_filename"] = new_tf
                        file_content["confirmed"] = True
                        warnings.append(
                            f"Sanitized target filename '{old_tf}' to '{new_tf}'"
                        )

            if safe_file_key in sanitized_plan:
                safe_file_key = _disambiguate_key(
                    sanitized_plan, safe_file_key, is_file=True
                )
                if isinstance(file_content, dict) and "target_filename" in file_content:
                    file_content["target_filename"] = safe_file_key
                    file_content["confirmed"] = True

            sanitized_plan[safe_file_key] = file_content

        elif _is_dir_node(content):
            safe_key, transformed = _sanitize_plan_key(key, is_file=False)
            if transformed:
                warnings.append(f"Sanitized folder key '{key}' to '{safe_key}'")

            children = {k: v for k, v in content.items() if not k.startswith("__")} if isinstance(content, dict) else {}
            sub_sanitized, sub_warns = sanitize_plan(children)
            warnings.extend(sub_warns)

            dir_content = sub_sanitized
            dir_content["__type__"] = "directory"
            if isinstance(content, dict):
                for k, v in content.items():
                    if k.startswith("__") and k not in dir_content:
                        dir_content[k] = v

            sanitized_plan[safe_key] = dir_content

        elif isinstance(content, (dict, BaseModel)):
            # Check if this dictionary node represents a file leaf node
            is_file_node = (
                isinstance(content, dict) and (
                    "target_filename" in content
                    or "relative_source" in content
                    or "status" in content
                    or "routed_by" in content
                )
            )

            if is_file_node:
                safe_file_key, transformed = _sanitize_plan_key(key, is_file=True)
                if transformed:
                    warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")

                file_content = dict(content) if isinstance(content, dict) else content
                if isinstance(file_content, dict) and "target_filename" in file_content and file_content["target_filename"]:
                    old_tf = file_content["target_filename"]
                    if contains_secrets(old_tf) or scrub_pii_from_filename(old_tf) != old_tf:
                        leaf_tf = re.split(r"[/\\]+", old_tf)[-1]
                        new_tf = sanitize_name(leaf_tf)
                        if new_tf != old_tf:
                            file_content["target_filename"] = new_tf
                            file_content["confirmed"] = True
                            warnings.append(
                                f"Sanitized target filename '{old_tf}' to '{new_tf}'"
                            )

                if safe_file_key in sanitized_plan:
                    safe_file_key = _disambiguate_key(
                        sanitized_plan, safe_file_key, is_file=True
                    )
                    if isinstance(file_content, dict) and "target_filename" in file_content:
                        file_content["target_filename"] = safe_file_key

                sanitized_plan[safe_file_key] = file_content
            else:
                safe_key, transformed = _sanitize_plan_key(key, is_file=False)
                if transformed:
                    warnings.append(f"Sanitized folder key '{key}' to '{safe_key}'")

                sub_sanitized, sub_warns = sanitize_plan(content)
                warnings.extend(sub_warns)

                if safe_key in sanitized_plan:
                    existing = sanitized_plan[safe_key]
                    if (
                        isinstance(existing, (dict, BaseModel))
                        and not _is_file_node(existing)
                        and not _is_dir_node(existing)
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

