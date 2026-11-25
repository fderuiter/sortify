"""Utility functions for handling paths and sanitizing filenames."""

import contextlib
import functools
import getpass
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

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
    re.compile(
        _BOUND_LEFT + r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}" + _BOUND_RIGHT
    ),
    # Phone number pattern
    re.compile(
        _BOUND_LEFT
        + r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"
        + _BOUND_RIGHT
    ),
    # Health ID / Patient ID / Medical Record patterns
    re.compile(
        r"(?i)"
        + _BOUND_LEFT
        + r"(?:MRN|PATIENT[ _-]?ID|SUBJECT[ _-]?ID|MED[ _-]?REC|HCID)[ _-]?#?:?\s*[A-Za-z0-9-]+"
        + _BOUND_RIGHT
    ),
    re.compile(r"(?i)" + _BOUND_LEFT + r"(?:PATIENT|SUBJECT)[ _-]?\d+" + _BOUND_RIGHT),
    re.compile(r"(?i)(Confidential Medical Report|Diagnosis:[^\n]*)"),
]


def _get_long_path_name(path_str: str) -> str | None:
    """Safely resolve Win32 long path name using kernel32.GetLongPathNameW with dynamic buffer allocation."""
    if sys.platform != "win32" and os.name != "nt":
        return None
    try:
        import ctypes

        buf_size = 1024
        buf = ctypes.create_unicode_buffer(buf_size)
        res = ctypes.windll.kernel32.GetLongPathNameW(path_str, buf, buf_size)
        if res >= buf_size:
            buf_size = res + 1
            buf = ctypes.create_unicode_buffer(buf_size)
            res = ctypes.windll.kernel32.GetLongPathNameW(path_str, buf, buf_size)
        if 0 < res < buf_size and buf.value:
            return buf.value
    except Exception:  # noqa: BLE001, S110 - reached from the log filter; logging here would recurse
        pass
    return None


def _get_short_path_name(path_str: str) -> str | None:
    """Safely resolve Win32 short 8.3 path name using kernel32.GetShortPathNameW with dynamic buffer allocation."""
    if sys.platform != "win32" and os.name != "nt":
        return None
    try:
        import ctypes

        buf_size = 1024
        buf = ctypes.create_unicode_buffer(buf_size)
        res = ctypes.windll.kernel32.GetShortPathNameW(path_str, buf, buf_size)
        if res >= buf_size:
            buf_size = res + 1
            buf = ctypes.create_unicode_buffer(buf_size)
            res = ctypes.windll.kernel32.GetShortPathNameW(path_str, buf, buf_size)
        if 0 < res < buf_size and buf.value:
            return buf.value
    except Exception:  # noqa: BLE001, S110 - reached from the log filter; logging here would recurse
        pass
    return None


@functools.lru_cache(maxsize=32)
def _get_compiled_home_patterns(
    home_dir_str: str | None,
    userprofile: str | None,
    home_env: str | None,
    homepath: str | None,
    homedrive: str | None,
) -> tuple[re.Pattern, ...]:
    """Resolve user home directory variants and return memoized compiled regex pattern objects."""
    home_dirs = []

    if home_dir_str:
        home_dirs.append(home_dir_str)

    # Reached from the log filter, which must never raise: stay blind here.
    with contextlib.suppress(Exception):
        ph = str(Path.home())
        if ph and ph not in home_dirs:
            home_dirs.append(ph)

    with contextlib.suppress(Exception):
        eu = os.path.expanduser("~")
        if eu and eu not in home_dirs:
            home_dirs.append(eu)

    for val in (userprofile, home_env, homepath):
        if val and val not in home_dirs:
            home_dirs.append(val)

    if homedrive and homepath:
        combined = homedrive + homepath
        if combined and combined not in home_dirs:
            home_dirs.append(combined)

    if sys.platform == "win32" or os.name == "nt":
        try:
            for h_dir in list(home_dirs):
                long_p = _get_long_path_name(h_dir)
                if long_p and long_p not in home_dirs:
                    home_dirs.append(long_p)
                short_p = _get_short_path_name(h_dir)
                if short_p and short_p not in home_dirs:
                    home_dirs.append(short_p)
        except Exception:  # noqa: BLE001, S110 - reached from the log filter; logging here would recurse
            pass

    home_dirs = sorted(home_dirs, key=len, reverse=True)

    patterns = []
    for h in home_dirs:
        clean = h.strip("\\/ ")
        if not h or clean in ("", "/", "\\") or len(clean) <= 2:
            continue

        raw_parts = [p for p in re.split(r"[\\/]+", h) if p]
        if not raw_parts:
            continue

        if (
            len(raw_parts[0]) == 2
            and raw_parts[0][1] == ":"
            or (
                len(raw_parts[0]) == 1
                and raw_parts[0].isalpha()
                and (h.startswith(("/", "\\")))
            )
        ):
            body_parts = raw_parts[1:]
        else:
            body_parts = raw_parts

        if not body_parts:
            continue

        pattern_str = (
            r"(?<![a-zA-Z0-9_])"
            + r"(?:[a-zA-Z]:[\/\\]*|[\/\\][a-zA-Z][\/\\]+)?"
            + r"[\/\\]*"
            + r"[\/\\]+".join([re.escape(p) for p in body_parts])
            + r"(?=[\\/]|[^a-zA-Z0-9_-]|$)"
        )
        with contextlib.suppress(Exception):
            patterns.append(re.compile(pattern_str, flags=re.IGNORECASE))

    return tuple(patterns)


def scrub_user_home_paths(text: str, home_dir: str | Path | None = None) -> str:
    """Replace all forms of the current user's home directory path with <USER_HOME>."""
    if not isinstance(text, str) or not text:
        return text

    home_dir_str = None
    if home_dir is not None:
        try:
            hd_str = str(home_dir)
            if hd_str:
                home_dir_str = hd_str
        except Exception:  # noqa: BLE001, S110 - called by the log filter, which must never raise or log
            pass

    userprofile = os.environ.get("USERPROFILE")
    home_env = os.environ.get("HOME")
    homepath = os.environ.get("HOMEPATH")
    homedrive = os.environ.get("HOMEDRIVE")

    patterns = _get_compiled_home_patterns(
        home_dir_str, userprofile, home_env, homepath, homedrive
    )

    for pattern in patterns:
        try:  # noqa: SIM105 - see home_dir above
            text = pattern.sub("<USER_HOME>", text)
        except Exception:  # noqa: BLE001, S110
            pass

    return text


def _split_name_ext(name: str) -> tuple[str, str, bool]:
    """Split a name into (stem, ext, valid_ext)."""
    if name.startswith(".") and re.match(r"^\.[a-zA-Z0-9]{1,5}$", name):
        return "", name, True
    last_component = re.split(r"[/\\]+", name)[-1]
    _, ext = os.path.splitext(last_component)
    valid_ext = bool(ext and re.match(r"^\.[a-zA-Z0-9]{1,5}$", ext))
    if valid_ext:
        stem = name[: -len(ext)]
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
    return getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")


def get_meipass_dir() -> str | None:
    """Get the PyInstaller _MEIPASS bundle directory path if running packaged, else None."""
    if is_packaged():
        return getattr(sys, "_MEIPASS", None)
    return None


def get_base_path(caller_file_path: str | None = None) -> str:
    """Get the standard base path of the application.

    Compatible with frozen/packaged execution and local development.
    """
    if is_packaged():
        mei = get_meipass_dir()
        if mei:
            return mei
        return os.path.dirname(sys.executable)
    else:
        file_path = caller_file_path or __file__
        return os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(file_path)))
        )


def get_sanitized_username() -> str:
    """Get the sanitized operating system username for safe user-isolated directory naming."""
    raw_user = None
    # getuser raises ImportError/KeyError on 3.12 and OSError from 3.13 when no user is found.
    with contextlib.suppress(ImportError, KeyError, OSError):
        raw_user = getpass.getuser()

    if not raw_user:
        raw_user = (
            os.environ.get("USER")
            or os.environ.get("USERNAME")
            or os.environ.get("LOGNAME")
            or "unknown_user"
        )

    # Sanitize for safe directory naming across all supported operating systems
    sanitized = re.sub(r"[^a-zA-Z0-9_\.\-]", "_", str(raw_user))
    sanitized = sanitized.strip(". _")

    if not sanitized or sanitized.upper() in RESERVED_NAMES:
        sanitized = "unknown_user"

    return sanitized


def get_session_base_dir() -> Path:
    """Get the standard base directory for sessions isolated by operating system user."""
    user_str = get_sanitized_username()
    app_dir_env = os.environ.get("AUTOSORTER_APP_DIR")
    if app_dir_env:
        p = Path(app_dir_env) / "sessions"
    else:
        p = Path(tempfile.gettempdir()) / f"autosorter_sessions_{user_str}"
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError as chmod_err:
        logger.debug(
            f"Failed to set permissions on session base directory '{p}': {chmod_err}"
        )
    return p


def setup_session_directory(session_id: str | None = None) -> tuple[str, Path]:
    """Set up and return the session ID and standard session database directory."""
    import uuid

    if not session_id:
        session_id = str(uuid.uuid4())
    session_dir = get_session_base_dir() / session_id
    session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(session_dir, 0o700)
    except OSError as chmod_err:
        logger.debug(
            f"Failed to set permissions on session directory '{session_dir}': {chmod_err}"
        )
    return session_id, session_dir


def resolve_db_crypto(db_path: Path | str):
    """Resolve and return the standard SessionCrypto instance for a given database path."""
    from app.core.crypto import _resolve_cached_session_crypto

    db_path_obj = Path(db_path).resolve()
    key_path = db_path_obj.parent / "secret.key"
    return _resolve_cached_session_crypto(key_path, db_path_obj)


def validate_target_path(target_path: str, keyword: str | None = None) -> None:
    """Validate a target folder path for cross-platform safety and correct structure.

    Args:
        target_path: Candidate relative directory path string to validate.
        keyword: Optional keyword context label for error reporting.

    Raises
    ------
        ValueError: If target_path is not a string, contains absolute path roots, Windows drive prefixes,
            illegal OS characters, directory traversal segments ('..'), reserved names, or trailing dots/spaces.
    """
    if not isinstance(target_path, str):
        suffix = f" for keyword '{keyword}'" if keyword else ""
        raise ValueError(f"Target path{suffix} must be a string.")

    # Check for absolute path roots (/ or \), platform-specific drive roots, or Windows drive prefixes
    if (
        target_path.startswith(("/", "\\"))
        or os.path.isabs(target_path)
        or Path(target_path).is_absolute()
        or bool(re.match(r"^[a-zA-Z]:", target_path))
    ):
        raise ValueError(f"Target path '{target_path}' cannot be an absolute path.")

    # Check for illegal OS characters
    if any(char in ILLEGAL_PATH_CHARS_SET for char in target_path):
        raise ValueError(f"Target path '{target_path}' contains illegal characters.")

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

        if segment.endswith((" ", ".")):
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


def _is_absolute_path(path: str) -> bool:
    """Check if a path string is absolute (POSIX, Windows drive letter, or UNC network path)."""
    if not isinstance(path, str) or not path:
        return False
    return (
        path.startswith(("/", "\\"))
        or os.path.isabs(path)
        or bool(re.match(r"^[a-zA-Z]:[/\\]?", path))
    )


def _split_absolute_path(path_str: str) -> tuple[str, str, list[str], bool, bool]:
    """Split an absolute path into (root, sep, clean_parts, had_traversal, is_unc)."""
    is_unc = path_str.startswith((r"\\", "//"))
    drive_match = re.match(r"^([a-zA-Z]:)[/\\]?", path_str)
    is_posix = path_str.startswith("/") and not is_unc

    if drive_match:
        drive_prefix = drive_match.group(1).upper()
        rest = path_str[len(drive_match.group(0)) :]
        sep = "\\"
        root = f"{drive_prefix}\\"
    elif is_unc:
        raw_unc = [p for p in re.split(r"[/\\]+", path_str) if p]
        had_traversal = any(p in ("..", ".") for p in raw_unc)
        parts_unc = [p for p in raw_unc if p not in ("..", ".")]
        if len(parts_unc) >= 2:
            root = f"\\\\{parts_unc[0]}\\{parts_unc[1]}\\"
            return root, "\\", parts_unc[2:], had_traversal, True
        return "\\\\", "\\", parts_unc, had_traversal, True
    elif is_posix:
        root = "/"
        rest = path_str[1:]
        sep = "/"
    else:
        root = "/"
        rest = path_str.lstrip(r"/\ ")
        sep = "/"

    raw_parts = [p for p in re.split(r"[/\\]+", rest) if p]
    had_traversal = any(p in ("..", ".") for p in raw_parts)
    clean_parts = [p for p in raw_parts if p not in ("..", ".")]
    return root, sep, clean_parts, had_traversal, False


def _join_absolute_parts(root: str, sep: str, parts: list[str]) -> str:
    """Join a parsed root prefix and normalized path segments."""
    if parts:
        return (
            root + sep.join(parts)
            if root.endswith(sep)
            else root + sep + sep.join(parts)
        )
    return root.rstrip(sep) if root not in ("/", "\\") else root


def _sanitize_folder_key_with_base(
    key: str, base_dir: str | None = None
) -> tuple[str, bool]:
    """Sanitize a folder hierarchy key while preserving trusted absolute base_dir or existing directory prefixes."""
    if not isinstance(key, str) or not key:
        return "Unnamed_safe", True

    import unicodedata

    from app.core.text_utils import sanitize_secret_patterns

    orig_key = key

    # If key represents an absolute destination path, preserve its root/drive and any
    # validated base_dir or existing directory prefix while sanitizing trailing segments.
    if _is_absolute_path(orig_key):
        s_raw = unicodedata.normalize("NFC", orig_key)
        s_raw = s_raw.replace("\x00", "")
        s_raw = re.sub(r"[\x00-\x1f\x7f]", "_", s_raw)

        root, sep, clean_parts, had_traversal, is_unc = _split_absolute_path(s_raw)
        protected_count = 0

        if not had_traversal:
            if isinstance(base_dir, str) and _is_absolute_path(base_dir):
                b_raw = unicodedata.normalize("NFC", base_dir).replace("\x00", "")
                b_root, _, b_parts, b_trav, _ = _split_absolute_path(b_raw)
                case_fold = sep == "\\" or os.name == "nt"
                b_root_cmp = b_root.lower() if case_fold else b_root
                root_cmp = root.lower() if case_fold else root
                if (
                    not b_trav
                    and b_root_cmp == root_cmp
                    and len(clean_parts) >= len(b_parts)
                ):
                    head_cmp = [
                        p.lower() if case_fold else p
                        for p in clean_parts[: len(b_parts)]
                    ]
                    base_cmp = [p.lower() if case_fold else p for p in b_parts]
                    if head_cmp == base_cmp:
                        protected_count = max(protected_count, len(b_parts))

            if not is_unc and (sep == "/") == (os.sep == "/"):
                for k in range(len(clean_parts), protected_count, -1):
                    candidate_prefix = _join_absolute_parts(root, sep, clean_parts[:k])
                    if os.path.isdir(candidate_prefix):
                        protected_count = k
                        break

        sanitized_parts = list(clean_parts[:protected_count])
        trailing_parts = clean_parts[protected_count:]
        escaped_chars = "".join(re.escape(c) for c in ILLEGAL_PATH_CHARS_SET)
        sanitized_trailing: list[str] = []

        for part in trailing_parts:
            scrubbed_part = scrub_pii_from_filename(sanitize_secret_patterns(part))
            was_scrubbed = scrubbed_part != part
            part_s = re.sub(f"[{escaped_chars}]", "_", scrubbed_part)
            part_s = part_s.rstrip(". ")
            if was_scrubbed:
                part_s = part_s.strip(" ._")
            upper_name = part_s.upper()
            base_name = upper_name.split(".")[0]
            if base_name in RESERVED_NAMES:
                subparts = part_s.split(".")
                subparts[0] = subparts[0] + "_safe"
                part_s = ".".join(subparts)
            if part_s and part_s not in (".", ".."):
                sanitized_trailing.append(part_s)

        if trailing_parts and not sanitized_trailing:
            sanitized_trailing.append("Unnamed_safe")

        sanitized_parts.extend(sanitized_trailing)
        s = _join_absolute_parts(root, sep, sanitized_parts)
        transformed = s != orig_key
        return s, transformed

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


def sanitize_folder_key(key: str) -> tuple[str, bool]:
    """Sanitize a folder hierarchy key to be OS-safe and clean.

    Strips secret patterns, PII expressions, path traversal elements, removes null bytes,
    replaces illegal filesystem characters and slashes with safe delimiters,
    strips trailing dots/spaces, and maps OS-reserved names to safe variants.
    Accepts and preserves validated absolute destination paths.

    Returns (safe_key, transformed).
    """
    return _sanitize_folder_key_with_base(key)


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
            is_existing_subfolder = isinstance(existing_val, dict) and existing_val.get(
                "__type__"
            ) not in ("file", "directory")
            is_v_subfolder = isinstance(v, dict) and v.get("__type__") not in (
                "file",
                "directory",
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


def sanitize_filename(name: str) -> tuple[str, bool]:
    """Sanitize a proposed target filename entry.

    Strips absolute path prefixes, path separators, and directory traversal operators ('..', '.'),
    and sanitizes secret patterns / PII expressions, converting filenames strictly into safe
    basename entries within target folders.

    Returns (safe_filename, transformed).
    """
    if not isinstance(name, str) or not name:
        return "Unnamed_safe", True

    from app.core.text_utils import contains_secrets

    orig = name
    # Strip drive letter prefixes (e.g. C: or C:\)
    s = re.sub(r"^[a-zA-Z]:[\/\\]*", "", name)
    # Split by slashes
    parts = re.split(r"[/\\]+", s)
    # Filter out directory traversal operators and empty segments
    clean_parts = [p for p in parts if p not in ("..", ".", "")]

    if not clean_parts:
        leaf = "Unnamed_safe"
    else:
        leaf = clean_parts[-1]

    if contains_secrets(leaf) or scrub_pii_from_filename(leaf) != leaf:
        leaf = sanitize_name(leaf)

    transformed = leaf != orig
    return leaf, transformed


def _sanitize_plan_key(
    key: str, is_file: bool = False, base_dir: str | None = None
) -> tuple[str, bool]:
    """Sanitize a plan dictionary key, handling file targets and folder keys safely."""
    if not isinstance(key, str) or not key:
        return "Unnamed_safe", True

    if is_file:
        return sanitize_filename(key)
    else:
        return _sanitize_folder_key_with_base(key, base_dir=base_dir)


def _sanitize_plan_with_base(
    plan: Any, base_dir: str | None = None
) -> tuple[dict, list[str]]:
    """Recursively sanitize plan keys while preserving trusted absolute base_dir prefixes."""
    if hasattr(plan, "plan") and isinstance(plan.plan, dict):
        plan = plan.plan
    elif hasattr(plan, "model_dump") and not isinstance(plan, dict):
        plan = plan.model_dump()

    if not isinstance(plan, dict):
        return plan, []

    sanitized_plan = {}
    warnings = []

    def _is_file_node(obj):
        if isinstance(obj, BaseModel):
            t = getattr(obj, "node_type", None) or getattr(obj, "__type__", None)
            return t == "file"
        if isinstance(obj, dict):
            return (
                obj.get("__type__") == "file"
                or "target_filename" in obj
                or "relative_source" in obj
                or "status" in obj
                or "routed_by" in obj
            )
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
            if isinstance(key, str) and "::" in key:
                src_root_raw, rel_part = key.split("::", 1)
                norm_root = os.path.normpath(src_root_raw)
                safe_leaf, _ = _sanitize_plan_key(rel_part, is_file=True)
                orig_leaf = re.split(r"[/\\]+", rel_part)[-1] if rel_part else ""
                rel_parts = [p for p in re.split(r"[/\\]+", rel_part) if p]
                has_traversal = ".." in rel_parts
                if safe_leaf != orig_leaf or has_traversal:
                    safe_file_key = f"{norm_root}::{safe_leaf}"
                    warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")
                else:
                    safe_file_key = f"{norm_root}::{rel_part}"
            else:
                safe_file_key, transformed = _sanitize_plan_key(key, is_file=True)
                if transformed:
                    warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")
            if safe_file_key in sanitized_plan:
                safe_file_key = _disambiguate_key(
                    sanitized_plan, safe_file_key, is_file=True
                )
            sanitized_plan[safe_file_key] = None

        elif _is_file_node(content):
            file_content = dict(content) if isinstance(content, dict) else content
            if isinstance(key, str) and "::" in key:
                src_root_raw, rel_part = key.split("::", 1)
                norm_root = os.path.normpath(src_root_raw)
                safe_leaf, _ = _sanitize_plan_key(rel_part, is_file=True)
                orig_leaf = re.split(r"[/\\]+", rel_part)[-1] if rel_part else ""
                rel_parts = [p for p in re.split(r"[/\\]+", rel_part) if p]
                has_traversal = ".." in rel_parts
                leaf_changed = safe_leaf != orig_leaf or has_traversal
                if leaf_changed:
                    safe_file_key = f"{norm_root}::{safe_leaf}"
                    warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")
                else:
                    safe_file_key = f"{norm_root}::{rel_part}"

                if isinstance(file_content, dict):
                    file_content.setdefault("source_root", norm_root)
                    if not file_content.get("source_path"):
                        file_content["source_path"] = rel_part
                    if (
                        "target_filename" in file_content
                        and file_content["target_filename"]
                    ):
                        old_tf = file_content["target_filename"]
                        raw_old_tf = old_tf
                        if isinstance(old_tf, str) and "::" in old_tf:
                            _, old_tf = old_tf.split("::", 1)
                        safe_tf, tf_transformed = sanitize_filename(old_tf)
                        if tf_transformed or safe_tf != raw_old_tf:
                            file_content["target_filename"] = safe_tf
                            file_content["confirmed"] = True
                            warnings.append(
                                f"Sanitized target filename '{raw_old_tf}' to '{safe_tf}'"
                            )
                    elif leaf_changed:
                        file_content["confirmed"] = True
                        file_content["target_filename"] = safe_leaf
            else:
                safe_file_key, transformed = _sanitize_plan_key(key, is_file=True)
                if transformed:
                    warnings.append(f"Sanitized filename '{key}' to '{safe_file_key}'")

                if isinstance(file_content, dict):
                    if (
                        "target_filename" in file_content
                        and file_content["target_filename"]
                    ):
                        old_tf = file_content["target_filename"]
                        safe_tf, tf_transformed = sanitize_filename(old_tf)
                        if tf_transformed:
                            file_content["target_filename"] = safe_tf
                            file_content["confirmed"] = True
                            warnings.append(
                                f"Sanitized target filename '{old_tf}' to '{safe_tf}'"
                            )
                    else:
                        if transformed:
                            file_content["confirmed"] = True
                            file_content["target_filename"] = safe_file_key

            if safe_file_key in sanitized_plan:
                safe_file_key = _disambiguate_key(
                    sanitized_plan, safe_file_key, is_file=True
                )
                is_explicit_overwrite = isinstance(file_content, dict) and bool(
                    file_content.get("overwrite") or file_content.get("allow_overwrite")
                )
                if (
                    isinstance(file_content, dict)
                    and "target_filename" in file_content
                    and not is_explicit_overwrite
                ):
                    file_content["target_filename"] = safe_file_key
                    file_content["confirmed"] = True

            sanitized_plan[safe_file_key] = file_content

        elif _is_dir_node(content):
            safe_key, transformed = _sanitize_plan_key(
                key, is_file=False, base_dir=base_dir
            )
            if transformed:
                warnings.append(f"Sanitized folder key '{key}' to '{safe_key}'")

            children = (
                {k: v for k, v in content.items() if not k.startswith("__")}
                if isinstance(content, dict)
                else {}
            )
            sub_sanitized, sub_warns = _sanitize_plan_with_base(
                children, base_dir=base_dir
            )
            warnings.extend(sub_warns)

            dir_content = sub_sanitized
            dir_content["__type__"] = "directory"
            if isinstance(content, dict):
                for k, v in content.items():
                    if k.startswith("__") and k not in dir_content:
                        dir_content[k] = v

            sanitized_plan[safe_key] = dir_content

        elif isinstance(content, (dict, BaseModel)):
            safe_key, transformed = _sanitize_plan_key(
                key, is_file=False, base_dir=base_dir
            )
            if transformed:
                warnings.append(f"Sanitized folder key '{key}' to '{safe_key}'")

            sub_sanitized, sub_warns = _sanitize_plan_with_base(
                content, base_dir=base_dir
            )
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


def sanitize_plan(plan: Any) -> tuple[dict, list[str]]:
    """Recursively sanitize folder keys, leaf file names, and target filenames in a plan dictionary or SortingPlan.

    Returns (sanitized_plan, warnings).
    """
    return _sanitize_plan_with_base(plan)


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


def safe_relpath(path: str | Path, start: str | Path | None = None) -> str:
    """Safely compute relative path, handling cross-drive mismatches on Windows gracefully.

    If path and start reside on different drives/mounts on Windows, os.path.relpath
    raises ValueError. In that scenario, return the normalized absolute path string.
    """
    if not path:
        return ""
    path_str = str(path)
    if start is None:
        return os.path.abspath(path_str)
    start_str = str(start)

    try:
        with contextlib.suppress(OSError, ValueError):
            real_p = os.path.realpath(path_str)
            real_s = os.path.realpath(start_str)
            rel = os.path.relpath(real_p, real_s)
            if not rel.startswith(".."):
                return rel
        return os.path.relpath(path_str, start_str)
    except (OSError, ValueError, TypeError):
        return os.path.abspath(path_str)


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


def safe_extract_path(
    target_dir: str | Path,
    member_path: str | Path,
    allow_same_dir: bool = False,
) -> Path:
    """Validate that member_path resolves safely inside target_dir and return resolved destination Path.

    Sanitizes cross-platform path separators, rejects null bytes, absolute roots,
    Windows drive prefixes, and directory traversal attempts that escape target_dir.

    Args:
        target_dir: The destination directory where files should be extracted.
        member_path: The relative file or directory path from the archive or attachment.
        allow_same_dir: Whether to allow member_path resolving to target_dir itself (default False).

    Returns
    -------
        The resolved Path object representing the safe destination path within target_dir.

    Raises
    ------
        ValueError: If target_dir or member_path is empty, contains null bytes, is absolute,
            contains a drive prefix, or attempts to traverse outside target_dir.
    """
    if not target_dir or not str(target_dir).strip():
        raise ValueError("Target directory must not be empty.")
    if not member_path or not str(member_path).strip():
        raise ValueError("Member path must not be empty.")

    target_str = str(target_dir)
    member_str = str(member_path)

    if "\x00" in target_str or "\x00" in member_str:
        raise ValueError("Path cannot contain null bytes.")

    clean_member = member_str.replace("\\", "/")

    # Check for absolute paths, UNC network shares, or Windows drive prefixes
    if clean_member.startswith("/") or bool(re.match(r"^[a-zA-Z]:", clean_member)):
        raise ValueError(
            f"Path traversal attempt: entry '{member_path}' is an absolute path or contains drive prefix."
        )

    target_dir_path = Path(target_dir).resolve()
    candidate_path = (target_dir_path / clean_member).resolve()

    # Verify containment within target directory
    is_contained = False
    try:
        if candidate_path.is_relative_to(target_dir_path):
            is_contained = True
    except (AttributeError, ValueError):
        pass

    if not is_contained:
        s_candidate = str(candidate_path).replace("\\", "/").rstrip("/").lower()
        s_target = str(target_dir_path).replace("\\", "/").rstrip("/").lower()
        if s_candidate == s_target or s_candidate.startswith(s_target + "/"):
            is_contained = True

    if not is_contained:
        raise ValueError(
            f"Path traversal detected: entry '{member_path}' escapes target directory '{target_dir}'."
        )

    if candidate_path == target_dir_path and not allow_same_dir:
        raise ValueError(
            f"Path traversal detected: entry '{member_path}' resolves to target directory itself."
        )

    return candidate_path


def _is_plan_leaf_node(node: Any) -> bool:
    """Return True when ``node`` represents a file or directory-cleanup leaf rather than a folder dict."""
    return isinstance(node, dict) and node.get("__type__") in ("file", "directory")


def resolve_plan_folder_dict(
    plan_root: Any,
    folder_path: str,
    *,
    create: bool = False,
) -> dict[str, Any] | None:
    """Resolve or create a nested folder container dictionary inside ``plan_root``."""
    raw_root = getattr(plan_root, "plan", plan_root)
    if not isinstance(raw_root, dict):
        return None
    if not folder_path:
        return raw_root
    existing_direct = raw_root.get(folder_path)
    if isinstance(existing_direct, dict) and not _is_plan_leaf_node(existing_direct):
        return existing_direct
    if _is_absolute_path(folder_path):
        if create:
            if folder_path in raw_root and _is_plan_leaf_node(raw_root[folder_path]):
                return None
            return raw_root.setdefault(folder_path, {})
        return None

    clean = folder_path.replace("\\", "/").strip("/")
    if not clean:
        return raw_root
    curr: dict[str, Any] = raw_root
    for part in (p for p in clean.split("/") if p):
        if part in curr:
            nxt = curr[part]
            if isinstance(nxt, dict) and not _is_plan_leaf_node(nxt):
                curr = nxt
            else:
                return None
        elif create:
            curr = curr.setdefault(part, {})
        else:
            return None
    return curr


def collect_plan_folder_paths(
    plan_node: Any,
    prefix: str = "",
) -> list[str]:
    """Recursively enumerate all nested target folder paths in ``plan_node``."""
    raw_node = getattr(plan_node, "plan", plan_node)
    found: list[str] = []
    if not isinstance(raw_node, dict) or _is_plan_leaf_node(raw_node):
        return found
    for k, v in raw_node.items():
        if not isinstance(v, dict) or _is_plan_leaf_node(v):
            continue
        sub_path = k if (_is_absolute_path(k) or not prefix) else f"{prefix}/{k}"
        found.append(sub_path)
        if not _is_absolute_path(k):
            found.extend(collect_plan_folder_paths(v, sub_path))
    return found


def collect_empty_plan_folders(
    plan_node: Any,
    prefix: str = "",
) -> list[str]:
    """Recursively enumerate leaf folder paths in ``plan_node`` that hold no child entries."""
    raw_node = getattr(plan_node, "plan", plan_node)
    empty_paths: list[str] = []
    if not isinstance(raw_node, dict) or _is_plan_leaf_node(raw_node):
        return empty_paths
    for k, v in raw_node.items():
        if not isinstance(v, dict) or _is_plan_leaf_node(v):
            continue
        sub_path = k if (_is_absolute_path(k) or not prefix) else f"{prefix}/{k}"
        if not v:
            empty_paths.append(sub_path)
        elif not _is_absolute_path(k):
            empty_paths.extend(collect_empty_plan_folders(v, sub_path))
    return empty_paths


def prune_empty_plan_ancestors(
    plan_root: Any,
    folder_path: str,
    *,
    preserve_folders: set[str] | None = None,
) -> None:
    """Remove emptied folder dicts along ``folder_path`` unless preserved as manual folders."""
    raw_root = getattr(plan_root, "plan", plan_root)
    if not isinstance(raw_root, dict) or not folder_path:
        return
    preserved_norm: set[str] = set()
    for p in preserve_folders or ():
        if not p:
            continue
        if _is_absolute_path(p):
            preserved_norm.add(p)
        else:
            norm_p = p.replace("\\", "/").strip("/")
            parts = [seg for seg in norm_p.split("/") if seg]
            for idx in range(1, len(parts) + 1):
                preserved_norm.add("/".join(parts[:idx]))

    if folder_path in raw_root and _is_absolute_path(folder_path):
        target = raw_root.get(folder_path)
        if (
            isinstance(target, dict)
            and not _is_plan_leaf_node(target)
            and not target
            and folder_path not in preserved_norm
        ):
            raw_root.pop(folder_path, None)
        return

    clean = folder_path.replace("\\", "/").strip("/")
    if not clean:
        return
    parts = [p for p in clean.split("/") if p]
    chain: list[tuple[dict[str, Any], str, str]] = []
    curr: dict[str, Any] = raw_root
    built = ""
    for part in parts:
        nxt = curr.get(part)
        if not isinstance(nxt, dict) or _is_plan_leaf_node(nxt):
            break
        built = f"{built}/{part}" if built else part
        chain.append((curr, part, built))
        curr = nxt

    for parent_dict, key_name, full_rel in reversed(chain):
        child_dict = parent_dict.get(key_name)
        if (
            isinstance(child_dict, dict)
            and not _is_plan_leaf_node(child_dict)
            and not child_dict
            and full_rel not in preserved_norm
        ):
            parent_dict.pop(key_name, None)
        else:
            break
