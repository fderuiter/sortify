"""Extensible modular rule importer framework for Hazel and DropIt rule packages.

Provides strategy classes HazelRuleImporter and DropItRuleImporter to parse external
rule definitions and safely translate them into Sortify KEYWORD_RULES and POLICIES.
"""

import abc
import configparser
import json
import logging
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

from app.core.path_utils import validate_target_path

logger = logging.getLogger(__name__)

_SIZE_ATTR_WORDS = frozenset(
    {
        "size",
        "filesize",
        "byte",
        "bytes",
        "kilobyte",
        "kilobytes",
        "megabyte",
        "megabytes",
        "gigabyte",
        "gigabytes",
    }
)
_DATE_ATTR_WORDS = frozenset(
    {
        "date",
        "created",
        "modified",
        "added",
        "opened",
        "accessed",
        "mtime",
        "ctime",
        "atime",
        "timestamp",
        "age",
        "time",
    }
)
_NEGATED_OP_WORDS = frozenset({"not", "none", "exclude", "excludes", "neither"})
_NEGATED_OP_EXACT = frozenset(
    {
        "!=",
        "<>",
        "isnot",
        "doesnotcontain",
        "doesnotmatch",
        "doesnotbeginwith",
        "doesnotstartwith",
        "doesnotendwith",
        "notcontains",
        "notmatches",
        "not_in",
        "notin",
    }
)
_NUMERIC_OP_EXACT = frozenset(
    {">", "<", ">=", "<=", "gt", "lt", "gte", "lte", "between"}
)
_NUMERIC_OP_PHRASES = (
    "greater than",
    "less than",
    "at least",
    "at most",
    "in the last",
    "not in the last",
    "is before",
    "is after",
    "older than",
    "newer than",
    "more than",
    "fewer than",
    "before",
    "after",
)
_SUPPORTED_POSITIVE_OPS = frozenset(
    {
        "",
        "is",
        "equals",
        "equal",
        "==",
        "=",
        "contains",
        "contain",
        "includes",
        "include",
        "matches",
        "match",
        "like",
        "glob",
        "wildcard",
        "in",
        "begins with",
        "starts with",
        "ends with",
    }
)
_SHELL_HOME_ENV_VARS = frozenset(
    {
        "userprofile",
        "homepath",
        "homedrive",
        "systemdrive",
        "appdata",
        "localappdata",
        "temp",
        "tmp",
        "dropit",
        "home",
    }
)
_DROPIT_SIZE_KEYS = frozenset(
    {
        "size",
        "filesize",
        "file_size",
        "minsize",
        "min_size",
        "maxsize",
        "max_size",
        "sizefilter",
        "size_filter",
    }
)
_DROPIT_DATE_KEYS = frozenset(
    {
        "date",
        "datecreated",
        "date_created",
        "datemodified",
        "date_modified",
        "dateopened",
        "date_opened",
        "dateaccessed",
        "date_accessed",
        "created",
        "modified",
        "age",
        "datefilter",
        "date_filter",
    }
)
_DROPIT_EXCLUDE_KEYS = frozenset(
    {"exclude", "excluded", "excludes", "notpattern", "not_pattern", "ignore"}
)
_DROPIT_NEGATE_KEYS = frozenset({"negate", "negated", "isnegated", "is_negated", "not"})
_UNARCHIVE_ACTION_CORES = frozenset(
    {
        "unarchive",
        "extract",
        "extractarchive",
        "extracthere",
        "unpack",
        "decompress",
        "unzip",
        "untar",
    }
)
_MOVE_ACTION_CORES = frozenset(
    {
        "",
        "move",
        "movefile",
        "moveto",
        "movetofolder",
        "sort",
        "sortintofolder",
        "sortintosubfolder",
        "renameandmove",
        "route",
        "relocate",
    }
)
_COPY_ACTION_CORES = frozenset(
    {"copy", "copyfile", "copyto", "copytofolder", "duplicate"}
)
_TRASH_ACTION_CORES = frozenset(
    {
        "trash",
        "movetotrash",
        "sendtotrash",
        "putintrash",
        "throwaway",
        "recycle",
        "recyclebin",
        "movetorecyclebin",
        "sendtorecyclebin",
        "deletetorecyclebin",
        "deleterecyclebin",
        "delete",
        "remove",
    }
)
_PERMANENT_DELETE_MODES = frozenset(
    {"directlydelete", "permanent", "permanently", "safelyerase", "shred", "wipe"}
)
_RENAME_ACTION_CORES = frozenset(
    {"rename", "renamefile", "setname", "applyrenamepattern"}
)
_HAZEL_TOKEN_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$\(\s*year\s*\)|%Y", re.IGNORECASE), "{year}"),
    (re.compile(r"\$\(\s*month\s*\)|%m", re.IGNORECASE), "{month}"),
    (re.compile(r"\$\(\s*day\s*\)|%d", re.IGNORECASE), "{day}"),
    (
        re.compile(
            r"\$\(\s*(?:date|date_?modified|date_?created|exif_?date)\s*\)",
            re.IGNORECASE,
        ),
        "{date}",
    ),
    (re.compile(r"\$\(\s*category\s*\)", re.IGNORECASE), "{category}"),
    (re.compile(r"\$\(\s*(?:name|stem)\s*\)", re.IGNORECASE), "{stem}"),
    (re.compile(r"\$\(\s*original\s*\)", re.IGNORECASE), "{original}"),
    (re.compile(r"\$\(\s*(?:ext|extension)\s*\)", re.IGNORECASE), "{extension}"),
    (re.compile(r"\$\(\s*(?:seq|sequence|#)\s*\)", re.IGNORECASE), "{seq}"),
)
_HAZEL_ANGLE_RENAME_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<\s*year\s*>", re.IGNORECASE), "{year}"),
    (re.compile(r"<\s*month\s*>", re.IGNORECASE), "{month}"),
    (re.compile(r"<\s*day\s*>", re.IGNORECASE), "{day}"),
    (
        re.compile(
            r"<\s*(?:date|date\s*modified|datemodified|date\s*created|datecreated|exif\s*date|exifdate)\s*>",
            re.IGNORECASE,
        ),
        "{date}",
    ),
    (re.compile(r"<\s*category\s*>", re.IGNORECASE), "{category}"),
    (re.compile(r"<\s*(?:name|stem)\s*>", re.IGNORECASE), "{stem}"),
    (re.compile(r"<\s*original\s*>", re.IGNORECASE), "{original}"),
    (re.compile(r"<\s*(?:ext|extension)\s*>", re.IGNORECASE), "{extension}"),
    (re.compile(r"<\s*(?:#|seq|sequence)\s*>", re.IGNORECASE), "{seq}"),
)


def _is_system_trash_destination(raw_path: str | None) -> bool:
    """Return True when an external destination path targets macOS Trash or Windows Recycle Bin."""
    if not raw_path or not str(raw_path).strip():
        return False
    cleaned = str(raw_path).strip().strip("\"'").replace("\\", "/").rstrip("/")
    lower = cleaned.lower()
    if lower in (".trash", "~/.trash", "trash", "recycle bin", "$recycle.bin"):
        return True
    return bool(re.search(r"(?:^|/)(?:\.trash|\$recycle\.bin)$", lower))


_INLINE_SIZE_RE = re.compile(
    r"^(?:"
    r"(?:size|filesize|file_size|bytes?)\s*(?:[<>]=?|!=|==|=|:)\s*\S+"
    r"|[<>]=?\s*\d+(?:\.\d+)?\s*(?:b|bytes?|kb|kib|mb|mib|gb|gib|tb|tib)\b"
    r"|\d+(?:\.\d+)?\s*(?:b|bytes?|kb|kib|mb|mib|gb|gib|tb|tib)\s*(?:\.\.|-)\s*\d+(?:\.\d+)?\s*(?:b|bytes?|kb|kib|mb|mib|gb|gib|tb|tib)?\b"
    r")$",
    flags=re.IGNORECASE,
)
_INLINE_DATE_RE = re.compile(
    r"^(?:"
    r"(?:date|created|modified|added|opened|accessed|age|mtime|ctime)\s*(?:[<>]=?|!=|==|=|:)\s*\S+"
    r"|[<>]=?\s*\d+\s*(?:s|sec|secs|m|min|mins|h|hr|hrs|d|days?|w|weeks?|mo|months?|y|yrs?|years?)\b"
    r"|[<>]=?\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r")$",
    flags=re.IGNORECASE,
)
_INLINE_NUMERIC_RE = re.compile(r"^[<>]=?\s*\d+(?:\.\d+)?$")


def _split_identifier_words(raw: str) -> set[str]:
    """Split camelCase, snake_case, or space-separated attribute/operator text into lowercase words."""
    if not raw:
        return set()
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", str(raw))
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
    return set(re.findall(r"[a-z0-9]+", spaced.lower()))


def _classify_condition_attribute(attribute: str) -> str | None:
    """Return ``'size'`` or ``'date'`` if attribute targets numeric size or temporal metadata."""
    words = _split_identifier_words(attribute)
    if words & _SIZE_ATTR_WORDS:
        return "size"
    if words & _DATE_ATTR_WORDS:
        return "date"
    return None


def _classify_condition_operator(
    operator: str, *, is_negated_flag: bool = False
) -> str:
    """Classify condition operator into ``'negated'``, ``'numeric'``, ``'starts_with'``, ``'ends_with'``, ``'supported'``, or ``'unsupported'``."""
    if is_negated_flag:
        return "negated"
    op_clean = " ".join(str(operator or "").strip().lower().split())
    if not op_clean:
        return "supported"
    op_compact = re.sub(r"[\s_-]+", "", op_clean)
    op_words = _split_identifier_words(operator)

    if (
        op_clean.startswith("!")
        or op_clean in _NEGATED_OP_EXACT
        or op_compact in _NEGATED_OP_EXACT
        or bool(op_words & _NEGATED_OP_WORDS)
    ):
        return "negated"

    if (
        op_clean in _NUMERIC_OP_EXACT
        or op_compact in _NUMERIC_OP_EXACT
        or op_clean.startswith((">", "<"))
        or any(phrase in op_clean for phrase in _NUMERIC_OP_PHRASES)
    ):
        return "numeric"

    if op_clean in ("begins with", "starts with") or op_compact in (
        "beginswith",
        "startswith",
    ):
        return "starts_with"
    if op_clean == "ends with" or op_compact == "endswith":
        return "ends_with"

    if op_clean in _SUPPORTED_POSITIVE_OPS or op_compact in {
        "is",
        "equals",
        "equal",
        "contains",
        "contain",
        "includes",
        "include",
        "matches",
        "match",
        "like",
        "glob",
        "wildcard",
        "in",
    }:
        return "supported"

    return "unsupported"


def _classify_inline_filter_token(token: str) -> str | None:
    """Detect inline size, date, or numeric comparison tokens in pattern masks."""
    tok = token.strip()
    if not tok:
        return None
    if _INLINE_SIZE_RE.match(tok):
        return "size"
    if _INLINE_DATE_RE.match(tok):
        return "date"
    if _INLINE_NUMERIC_RE.match(tok):
        return "numeric"
    return None


def _has_wildcard_or_ext_mask(expr: str) -> bool:
    """Return True if expression uses glob wildcards or a leading-dot extension mask."""
    return (
        "*" in expr
        or "?" in expr
        or ("[" in expr and "]" in expr)
        or expr.startswith(".")
    )


def _translate_hazel_path_tokens(raw_text: str) -> str:
    """Translate unambiguous Hazel $(token) and strftime date placeholders into Sortify {token} syntax."""
    out = raw_text
    for pat, repl in _HAZEL_TOKEN_SUBS:
        out = pat.sub(repl, out)
    return out


def _normalize_imported_rename_pattern(raw_pattern: Any) -> tuple[str, str | None]:
    """Normalize and validate an imported rename pattern string."""
    if not raw_pattern or not isinstance(raw_pattern, str):
        return "", None
    cleaned = _translate_hazel_path_tokens(raw_pattern.strip())
    for pat, repl in _HAZEL_ANGLE_RENAME_SUBS:
        cleaned = pat.sub(repl, cleaned)
    if not cleaned:
        return "", None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        return "", f"Rename pattern '{raw_pattern}' contains control characters."
    if (
        ".." in cleaned.replace("\\", "/").split("/")
        or "/" in cleaned
        or "\\" in cleaned
    ):
        return (
            "",
            f"Rename pattern '{raw_pattern}' cannot contain path separators or '..' traversal.",
        )
    from app.core.pattern_formatter import PatternTokenFormatter

    preview = PatternTokenFormatter.format_pattern(
        cleaned,
        metadata={
            "date": "2026-01-15",
            "category": "sample",
            "original": "document",
            "stem": "document",
            "extension": ".pdf",
        },
        seq=1,
        fallback_original="",
    )
    if not preview:
        return "", f"Rename pattern '{raw_pattern}' resolved to an empty filename."
    return cleaned, None


def _map_hazel_metadata_condition(
    raw_attr: str,
    raw_op: str,
    raw_val: Any,
    *,
    negated_flag: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """Map a single-condition Hazel size, age, or date clause to a Sortify metadata policy.

    Returns ``(policy_type, expression, error_reason)``.
    """
    from app.core.policy_engine import validate_metadata_expression

    words = _split_identifier_words(raw_attr)
    attr_compact = re.sub(r"[^a-z0-9]+", "", str(raw_attr or "").lower())

    p_type: str | None = None
    implied_size_unit = ""
    if words & _SIZE_ATTR_WORDS:
        p_type = "size"
        if words & {"kilobyte", "kilobytes"}:
            implied_size_unit = "KB"
        elif words & {"megabyte", "megabytes"}:
            implied_size_unit = "MB"
        elif words & {"gigabyte", "gigabytes"}:
            implied_size_unit = "GB"
    elif (
        "exif" in words
        or "taken" in words
        or "contentcreationdate" in attr_compact
        or "contentcreated" in attr_compact
        or ("original" in words and "date" in words)
    ):
        p_type = "exif_date"
    elif "age" in words:
        p_type = "age"
    elif words & {"created", "creation", "ctime", "birthtime"}:
        p_type = "created_date"
    elif words & {"modified", "modification", "mtime"} or attr_compact in {
        "date",
        "filedate",
        "lastmodified",
        "datelastmodified",
    }:
        p_type = "modified_date"
    elif words & _DATE_ATTR_WORDS:
        return (
            None,
            None,
            f"Unsupported date filter attribute '{raw_attr}' (supported: modified_date, created_date, exif_date, age).",
        )
    else:
        return None, None, None

    if raw_val is None:
        return None, None, f"Missing comparison value for '{raw_attr}'."
    val_str = str(raw_val).strip()
    if not val_str:
        return None, None, f"Empty comparison value for '{raw_attr}'."

    op_clean = " ".join(str(raw_op or "").strip().lower().split())
    op_compact = re.sub(r"[\s_-]+", "", op_clean)

    # Reject pure inequality / negation except "not in the last" (which expresses age > duration)
    is_not_in_last = "not in the last" in op_clean or op_compact in (
        "notinthelast",
        "isnotinthelast",
    )
    if (negated_flag and "in the last" not in op_clean) or (
        not is_not_in_last
        and (
            op_clean.startswith("!")
            or op_clean in _NEGATED_OP_EXACT
            or op_compact in _NEGATED_OP_EXACT
            or ("not" in _split_identifier_words(raw_op))
        )
    ):
        return (
            None,
            None,
            f"Negated condition operator '{raw_op or 'negated'}' is not supported for '{raw_attr}'.",
        )
    if negated_flag and "in the last" in op_clean:
        is_not_in_last = True

    if op_clean in ("", "is", "equals", "equal", "==", "=", "matches", "match"):
        try:
            validate_metadata_expression(p_type, val_str)
            return p_type, val_str, None
        except ValueError:
            pass

    expr = ""
    if p_type == "size":
        val_with_unit = (
            f"{val_str}{implied_size_unit}"
            if implied_size_unit and re.fullmatch(r"\d+(?:\.\d+)?", val_str)
            else val_str
        )
        if op_clean in (">", "gt") or any(
            p in op_clean for p in ("greater than", "more than")
        ):
            expr = f"> {val_with_unit}"
        elif op_clean in (">=", "gte") or "at least" in op_clean:
            expr = f">= {val_with_unit}"
        elif op_clean in ("<", "lt") or any(
            p in op_clean for p in ("less than", "fewer than")
        ):
            expr = f"< {val_with_unit}"
        elif op_clean in ("<=", "lte") or "at most" in op_clean:
            expr = f"<= {val_with_unit}"
        elif op_clean in ("", "is", "equals", "equal", "==", "="):
            expr = (
                val_with_unit
                if val_with_unit.startswith((">", "<", "="))
                else f"= {val_with_unit}"
            )
        else:
            return (
                None,
                None,
                f"Unsupported size operator '{raw_op}' on '{raw_attr}'.",
            )
    elif p_type == "age":
        dur_val = val_str if re.search(r"[a-zA-Z]", val_str) else f"{val_str}d"
        if (
            is_not_in_last
            or op_clean in (">", "gt")
            or any(p in op_clean for p in ("greater than", "more than", "older than"))
        ):
            expr = f"> {dur_val}"
        elif op_clean in (">=", "gte") or "at least" in op_clean:
            expr = f">= {dur_val}"
        elif (
            "in the last" in op_clean
            or op_clean in ("<", "lt")
            or any(p in op_clean for p in ("less than", "fewer than", "newer than"))
        ):
            expr = f"< {dur_val}"
        elif op_clean in ("<=", "lte") or "at most" in op_clean:
            expr = f"<= {dur_val}"
        elif op_clean in ("", "is", "equals", "equal", "==", "="):
            expr = dur_val if dur_val.startswith((">", "<", "=")) else f"= {dur_val}"
        else:
            return (
                None,
                None,
                f"Unsupported age operator '{raw_op}' on '{raw_attr}'.",
            )
    else:
        # modified_date, created_date, exif_date
        if is_not_in_last or "older than" in op_clean:
            dur_val = val_str if re.search(r"[a-zA-Z]", val_str) else f"{val_str}d"
            expr = f"older than {dur_val}"
        elif "in the last" in op_clean or "newer than" in op_clean:
            dur_val = val_str if re.search(r"[a-zA-Z]", val_str) else f"{val_str}d"
            expr = f"in the last {dur_val}"
        elif op_clean in ("<=", "lte") or any(
            p in op_clean for p in ("on or before", "at most")
        ):
            expr = f"<= {val_str}"
        elif op_clean in (">=", "gte") or any(
            p in op_clean for p in ("on or after", "at least")
        ):
            expr = f">= {val_str}"
        elif op_clean in ("<", "lt") or any(
            p in op_clean for p in ("is before", "before", "less than")
        ):
            expr = f"< {val_str}"
        elif op_clean in (">", "gt") or any(
            p in op_clean for p in ("is after", "after", "greater than")
        ):
            expr = f"> {val_str}"
        elif op_clean in ("", "is", "equals", "equal", "==", "="):
            expr = val_str if val_str.startswith((">", "<", "=")) else f"= {val_str}"
        else:
            return (
                None,
                None,
                f"Unsupported date operator '{raw_op}' on '{raw_attr}'.",
            )

    try:
        validate_metadata_expression(p_type, expr)
    except ValueError as err:
        return None, None, str(err)
    return p_type, expr, None


def _percent_decode_uri_path(value: str) -> str:
    """Decode percent-encoded UTF-8 sequences in a local file:// URI path."""
    return re.sub(
        r"(?:%[0-9A-Fa-f]{2})+",
        lambda m: bytes.fromhex(m.group(0).replace("%", "")).decode(
            "utf-8", errors="replace"
        ),
        value,
    )


def normalize_imported_target_path(raw_path: str) -> tuple[str, bool, str | None]:
    """Convert an exported destination path into a validated relative folder path.

    Parameters
    ----------
    raw_path : str
        Raw destination path string from an external rule package.

    Returns
    -------
    tuple[str, bool, str | None]
        ``(normalized_path, was_converted, error_reason)``. When ``raw_path`` is empty
        or whitespace-only, returns ``("", False, None)``. When ``raw_path`` cannot
        become a safe relative folder, returns ``("", False, error_reason)``.
    """
    if not raw_path or not isinstance(raw_path, str):
        return "", False, None

    stripped = raw_path.strip().strip("\"'").strip()
    if not stripped:
        return "", False, None

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        return (
            "",
            False,
            f"Destination path '{raw_path}' contains control characters.",
        )

    clean = stripped
    was_converted = False

    if clean.lower().startswith("file://"):
        unc_uri_match = re.match(r"(?i)^file://(?!/)(?!localhost(?:/|$))(.+)$", clean)
        if unc_uri_match:
            clean = "//" + _percent_decode_uri_path(unc_uri_match.group(1))
        else:
            clean = _percent_decode_uri_path(
                re.sub(r"(?i)^file://(?:localhost)?", "", clean)
            )
            if re.match(r"^/[a-zA-Z]:", clean):
                clean = clean[1:]
        was_converted = True
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean):
            return (
                "",
                False,
                f"Destination path '{raw_path}' contains control characters.",
            )

    clean = clean.replace("\\", "/")

    # Reject directory traversal before stripping any prefixes so '..' can never bypass validation
    if ".." in [seg for seg in clean.split("/") if seg]:
        return (
            "",
            False,
            f"Destination path '{raw_path}' contains directory traversal segments ('..').",
        )

    # Strip Windows extended-length path prefixes (\\?\ or \\?\UNC\)
    if re.match(r"^//[?.]/unc/", clean, flags=re.IGNORECASE):
        clean = "//" + re.sub(r"^//[?.]/unc/+", "", clean, flags=re.IGNORECASE)
        was_converted = True
    elif re.match(r"^//[?.]/", clean):
        clean = re.sub(r"^//[?.]/+", "", clean)
        was_converted = True

    # Strip UNC share root (//server/share/subpath)
    if clean.startswith("//"):
        was_converted = True
        unc_parts = [p for p in clean.lstrip("/").split("/") if p]
        if len(unc_parts) < 3:
            return (
                "",
                False,
                f"Destination path '{raw_path}' points to a root network share without a relative subfolder.",
            )
        clean = "/".join(unc_parts[2:])
    else:
        drive_match = re.match(r"^[a-zA-Z]:(.*)$", clean)
        env_match = re.match(r"^%([^%/]+)%/?(.*)$", clean)
        if drive_match:
            was_converted = True
            clean = drive_match.group(1).lstrip("/")
            if re.match(r"^(?:users|home)/[^/]+/?$", clean, flags=re.IGNORECASE):
                return (
                    "",
                    False,
                    f"Destination path '{raw_path}' points to a user home root without a relative subfolder.",
                )
            clean = re.sub(
                r"^(?:users|home)/[^/]+/(.+)$", r"\1", clean, flags=re.IGNORECASE
            )
        elif (
            clean == "~" or clean.startswith("~/") or re.match(r"^~[^/]+(?:/|$)", clean)
        ):
            was_converted = True
            clean = re.sub(r"^~[^/]*/?", "", clean)
        elif env_match:
            was_converted = True
            env_name = env_match.group(1).strip()
            env_rest = env_match.group(2).lstrip("/")
            if env_name.lower() in _SHELL_HOME_ENV_VARS:
                clean = env_rest
            else:
                clean = f"{env_name}/{env_rest}".strip("/") if env_rest else env_name
        elif clean.startswith("/"):
            was_converted = True
            clean = clean.lstrip("/")
            vol_match = re.match(
                r"^(?:volumes|mnt|media)/([^/]+)(?:/(.*))?$",
                clean,
                flags=re.IGNORECASE,
            )
            if vol_match:
                vol_rest = (vol_match.group(2) or "").lstrip("/")
                if not vol_rest:
                    return (
                        "",
                        False,
                        f"Destination path '{raw_path}' points to a volume root without a relative subfolder.",
                    )
                clean = vol_rest
            if re.match(r"^(?:users|home)/[^/]+/?$", clean, flags=re.IGNORECASE):
                return (
                    "",
                    False,
                    f"Destination path '{raw_path}' points to a user home root without a relative subfolder.",
                )
            clean = re.sub(
                r"^(?:users|home)/[^/]+/(.+)$", r"\1", clean, flags=re.IGNORECASE
            )

    segments = [seg for seg in clean.split("/") if seg and seg != "."]
    candidate = "/".join(segments)
    translated_candidate = _translate_hazel_path_tokens(candidate)
    if translated_candidate != candidate:
        was_converted = True
        candidate = translated_candidate

    if not candidate:
        return (
            "",
            False,
            f"Destination path '{raw_path}' cannot be converted into a safe relative folder.",
        )

    try:
        if "{" in candidate and "}" in candidate:
            from app.core.pattern_formatter import PatternTokenFormatter

            PatternTokenFormatter.format_target_path(
                candidate,
                metadata={
                    "date": "2026-01-15",
                    "category": "sample",
                    "original": "document",
                    "stem": "document",
                    "extension": ".pdf",
                },
                seq=1,
            )
        else:
            validate_target_path(candidate)
    except ValueError as err:
        return "", False, str(err)

    return candidate, was_converted, None


@dataclass
class RuleConflict:
    """Data structure representing a collision between an imported rule and an existing rule."""

    rule_type: str  # "keyword" or "policy"
    identifier: str  # keyword name or policy expression/type
    existing_target: str
    imported_target: str
    existing_priority: int | None = None
    imported_priority: int | None = None
    resolution: str = (
        "keep_existing"  # "keep_existing", "overwrite", or "skip_conflicting"
    )


def normalize_rule_path(path_str: str | None) -> str:
    """Normalize target folder path strings for rule conflict comparison."""
    if not path_str:
        return ""
    clean = str(path_str).strip().replace("\\", "/").rstrip("/")
    clean = clean.strip("\"'")
    return clean


def _find_matching_policy_index(
    policies: list[dict[str, Any]],
    policy_type: str,
    expression: str,
    *,
    strip_fields: bool = True,
) -> int | None:
    """Return the index of the first policy matching ``(type, expression)`` case-insensitively."""
    want_type = (
        str(policy_type).strip().lower() if strip_fields else str(policy_type).lower()
    )
    want_expr = (
        str(expression).strip().lower() if strip_fields else str(expression).lower()
    )
    for idx, existing_p in enumerate(policies):
        raw_type = str(existing_p.get("type", ""))
        raw_expr = str(existing_p.get("expression", ""))
        ex_type = raw_type.strip().lower() if strip_fields else raw_type.lower()
        ex_expr = raw_expr.strip().lower() if strip_fields else raw_expr.lower()
        if ex_type == want_type and ex_expr == want_expr:
            return idx
    return None


class RuleConflictAnalyzer:
    """Evaluates incoming keyword rules and policy patterns against existing settings to detect collisions and overlaps."""

    @staticmethod
    def analyze(import_result: Any, settings: Any) -> list[RuleConflict]:
        """Analyze imported rules against active and disabled settings to discover destination path collisions and policy overlaps.

        Parameters
        ----------
        import_result : Any
            The ImportResult containing incoming keyword rules and policies.
        settings : Any
            The AppSettings configuration object.

        Returns
        -------
        list[RuleConflict]
            List of detected RuleConflict instances.
        """
        conflicts: list[RuleConflict] = []
        if not import_result or not settings:
            return conflicts

        # 1. Evaluate keyword rule collisions across active and disabled rules
        existing_disabled = dict(getattr(settings, "DISABLED_KEYWORD_RULES", {}) or {})
        existing_keywords = dict(getattr(settings, "KEYWORD_RULES", {}) or {})
        existing_kw_map: dict[str, tuple[str, str]] = {}
        for k, v in existing_disabled.items():
            existing_kw_map[str(k).strip().lower()] = (k, v)
        for k, v in existing_keywords.items():
            existing_kw_map[str(k).strip().lower()] = (k, v)

        keyword_rules = getattr(import_result, "keyword_rules", {}) or {}
        for kw, target_path in keyword_rules.items():
            kw_clean = str(kw).strip()
            if not kw_clean:
                continue
            kw_lower = kw_clean.lower()
            if kw_lower in existing_kw_map:
                ex_orig_kw, ex_target = existing_kw_map[kw_lower]
                san_ex = normalize_rule_path(ex_target)
                san_imp = normalize_rule_path(target_path)
                if san_ex != san_imp:
                    conflicts.append(
                        RuleConflict(
                            rule_type="keyword",
                            identifier=ex_orig_kw,
                            existing_target=str(ex_target),
                            imported_target=str(target_path),
                            resolution="keep_existing",
                        )
                    )

        # 2. Evaluate policy pattern collisions
        existing_policies = list(getattr(settings, "POLICIES", []) or [])
        imported_policies = list(getattr(import_result, "policies", []) or [])

        for policy in imported_policies:
            p_type = str(policy.get("type", "")).strip().lower()
            p_expr = str(policy.get("expression", "")).strip().lower()
            p_target = policy.get("target_path", "")
            p_prio = policy.get("priority", 75)
            p_ren = str(policy.get("rename_pattern", "") or "").strip()

            if not p_expr:
                continue

            matching_idx = _find_matching_policy_index(
                existing_policies, p_type, p_expr
            )
            if matching_idx is not None:
                ex_p = existing_policies[matching_idx]
                ex_type = str(ex_p.get("type", "")).strip().lower()
                ex_expr = str(ex_p.get("expression", "")).strip().lower()
                ex_target = ex_p.get("target_path", "")
                ex_prio = ex_p.get("priority", 75)
                ex_act = str(ex_p.get("action", "")).strip().lower()
                p_act = str(policy.get("action", "")).strip().lower()
                ex_ren = str(ex_p.get("rename_pattern", "") or "").strip()
                san_ex = normalize_rule_path(ex_target)
                san_imp = normalize_rule_path(p_target)
                if (
                    san_ex != san_imp
                    or ex_prio != p_prio
                    or ex_act != p_act
                    or ex_ren != p_ren
                ):
                    ident = f"{ex_expr} ({ex_type})" if ex_type else ex_expr
                    conflicts.append(
                        RuleConflict(
                            rule_type="policy",
                            identifier=ident,
                            existing_target=str(ex_target),
                            imported_target=str(p_target),
                            existing_priority=int(ex_prio)
                            if ex_prio is not None
                            else None,
                            imported_priority=int(p_prio)
                            if p_prio is not None
                            else None,
                            resolution="keep_existing",
                        )
                    )

        return conflicts


@dataclass
class ImportResult:
    """Data container holding extracted keyword rules, policies, and warnings."""

    keyword_rules: dict[str, str] = field(default_factory=dict)
    policies: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    source_format: str = "Unknown"

    def merge_into_settings(
        self,
        settings: Any,
        resolutions: dict[str, str] | None = None,
        default_strategy: str = "keep_existing",
        import_disabled: bool = False,
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Safely merge extracted rules into settings according to resolution choices.

        Parameters
        ----------
        settings : Any
            The AppSettings configuration object.
        resolutions : dict[str, str] | None, optional
            Map of rule identifier to conflict resolution choice
            (``'keep_existing'``, ``'overwrite'``, ``'skip_conflicting'``).
        default_strategy : str, optional
            Default bulk resolution strategy if identifier is not specified.
        import_disabled : bool, optional
            When True, stage newly added keyword rules in ``DISABLED_KEYWORD_RULES``
            and newly added policies with ``enabled=False`` until reviewed.

        Returns
        -------
        tuple[dict[str, str], list[dict[str, Any]]]
            Tuple of updated ``(KEYWORD_RULES, POLICIES)``.
        """
        resolutions = resolutions or {}
        conflicts = RuleConflictAnalyzer.analyze(self, settings)
        kw_conflicts = {
            c.identifier.lower(): c for c in conflicts if c.rule_type == "keyword"
        }
        policy_conflicts = {
            c.identifier.lower(): c for c in conflicts if c.rule_type == "policy"
        }

        existing_keywords = dict(getattr(settings, "KEYWORD_RULES", {}) or {})
        existing_disabled = dict(getattr(settings, "DISABLED_KEYWORD_RULES", {}) or {})
        existing_policies = list(getattr(settings, "POLICIES", []) or [])

        new_keywords = dict(existing_keywords)
        new_disabled = dict(existing_disabled)
        new_policies = [dict(p) for p in existing_policies]

        existing_kw_lower = {k.lower(): k for k in existing_keywords.keys()}
        existing_dis_lower = {k.lower(): k for k in existing_disabled.keys()}

        for kw, target_path in self.keyword_rules.items():
            kw_clean = str(kw).strip()
            if not kw_clean:
                continue

            norm_target, was_conv, err = normalize_imported_target_path(
                str(target_path)
            )
            if err is not None or not norm_target:
                reason = err or "Empty destination folder"
                self.warnings.append(
                    f"Keyword rule '{kw_clean}' skipped before merge: {reason}"
                )
                self.unsupported.append({"rule": kw_clean, "reason": reason})
                continue
            if was_conv:
                self.warnings.append(
                    f"Keyword rule '{kw_clean}': converted destination path '{target_path}' -> '{norm_target}'."
                )

            kw_lower = kw_clean.lower()
            if kw_lower in existing_kw_lower:
                ex_kw = existing_kw_lower[kw_lower]
                if kw_lower in kw_conflicts:
                    conflict_obj = kw_conflicts[kw_lower]
                    res = resolutions.get(
                        conflict_obj.identifier,
                        resolutions.get(
                            kw_clean, resolutions.get(ex_kw, default_strategy)
                        ),
                    )
                    if res == "overwrite":
                        new_keywords[ex_kw] = norm_target
            elif kw_lower in existing_dis_lower:
                ex_dis_kw = existing_dis_lower[kw_lower]
                if kw_lower in kw_conflicts:
                    conflict_obj = kw_conflicts[kw_lower]
                    res = resolutions.get(
                        conflict_obj.identifier,
                        resolutions.get(
                            kw_clean, resolutions.get(ex_dis_kw, default_strategy)
                        ),
                    )
                    if res == "overwrite":
                        if import_disabled:
                            new_disabled[ex_dis_kw] = norm_target
                        else:
                            new_disabled.pop(ex_dis_kw, None)
                            new_keywords[ex_dis_kw] = norm_target
                elif not import_disabled:
                    new_disabled.pop(ex_dis_kw, None)
                    new_keywords[ex_dis_kw] = norm_target
            else:
                if import_disabled:
                    new_disabled[kw_clean] = norm_target
                else:
                    new_keywords[kw_clean] = norm_target

        for policy in self.policies:
            p_type = str(policy.get("type", "")).strip().lower()
            p_expr = str(policy.get("expression", "")).strip()
            p_expr_lower = p_expr.lower()

            if not p_expr_lower:
                continue

            from app.core.policy_engine import (
                METADATA_POLICY_TYPES,
                validate_metadata_expression,
            )

            if p_type in METADATA_POLICY_TYPES:
                try:
                    validate_metadata_expression(p_type, p_expr)
                except ValueError as meta_err:
                    self.warnings.append(
                        f"Policy rule '{p_expr}' ({p_type}) skipped before merge: {meta_err}"
                    )
                    self.unsupported.append({"rule": p_expr, "reason": str(meta_err)})
                    continue

            raw_target = str(policy.get("target_path", ""))
            p_action = str(policy.get("action", "")).strip().lower()
            allows_empty_target = p_action == "trash" or p_type == "duplicate"
            if not raw_target.strip() and allows_empty_target:
                norm_target, was_conv, err = "", False, None
            else:
                norm_target, was_conv, err = normalize_imported_target_path(raw_target)
            if err is not None or (not norm_target and not allows_empty_target):
                reason = err or "Empty destination folder"
                self.warnings.append(
                    f"Policy rule '{p_expr}' skipped before merge: {reason}"
                )
                self.unsupported.append({"rule": p_expr, "reason": reason})
                continue
            if was_conv:
                self.warnings.append(
                    f"Policy rule '{p_expr}': converted destination path '{raw_target}' -> '{norm_target}'."
                )

            pol_entry = dict(policy)
            pol_entry["target_path"] = norm_target
            if "rename_pattern" in pol_entry:
                norm_ren, ren_err = _normalize_imported_rename_pattern(
                    pol_entry.get("rename_pattern")
                )
                if ren_err is not None:
                    self.warnings.append(
                        f"Policy rule '{p_expr}': omitted invalid rename pattern ({ren_err})."
                    )
                    pol_entry.pop("rename_pattern", None)
                elif norm_ren:
                    pol_entry["rename_pattern"] = norm_ren
                else:
                    pol_entry.pop("rename_pattern", None)

            ident = f"{p_expr_lower} ({p_type})" if p_type else p_expr_lower
            ident_lower = ident.lower()

            matching_idx = _find_matching_policy_index(
                new_policies, p_type, p_expr_lower
            )

            if matching_idx is not None:
                if ident_lower in policy_conflicts:
                    conflict_obj = policy_conflicts[ident_lower]
                    res = resolutions.get(
                        conflict_obj.identifier,
                        resolutions.get(
                            ident, resolutions.get(p_expr_lower, default_strategy)
                        ),
                    )
                    if res == "overwrite":
                        if (
                            "enabled" not in pol_entry
                            and "enabled" in new_policies[matching_idx]
                        ):
                            pol_entry["enabled"] = new_policies[matching_idx]["enabled"]
                        new_policies[matching_idx] = pol_entry
            else:
                if import_disabled:
                    pol_entry["enabled"] = False
                new_policies.append(pol_entry)

        setattr(settings, "KEYWORD_RULES", new_keywords)
        if import_disabled or hasattr(settings, "DISABLED_KEYWORD_RULES"):
            setattr(settings, "DISABLED_KEYWORD_RULES", new_disabled)
        setattr(settings, "POLICIES", new_policies)

        if not import_disabled and hasattr(settings, "DUPLICATE_ACTION"):
            for pol_item in new_policies:
                if (
                    str(pol_item.get("type", "")).strip().lower() == "duplicate"
                    and str(pol_item.get("expression", "")).strip() == "*"
                    and pol_item.get("enabled", True) is not False
                ):
                    dup_act = str(pol_item.get("action", "")).strip().lower()
                    if dup_act in ("trash", "stage", "skip") and (
                        getattr(settings, "DUPLICATE_ACTION", "off") == "off"
                        or default_strategy == "overwrite"
                    ):
                        setattr(settings, "DUPLICATE_ACTION", dup_act)
                    det_mode = pol_item.get("detect_original_by")
                    if (
                        isinstance(det_mode, str)
                        and det_mode.strip()
                        and hasattr(settings, "DETECT_ORIGINAL_BY")
                    ):
                        setattr(settings, "DETECT_ORIGINAL_BY", det_mode.strip())
                    break

        if (
            hasattr(settings, "flush")
            and getattr(settings, "_save_timer", None) is not None
        ):
            settings.flush()
        elif hasattr(settings, "_save"):
            settings._save()

        return new_keywords, new_policies


class BaseRuleImporter(abc.ABC):
    """Abstract base class for rule strategy importers."""

    @abc.abstractmethod
    def parse_file(self, file_path: str | Path) -> ImportResult:
        """Parse rule package file and return extracted ImportResult."""

    @abc.abstractmethod
    def can_import(self, file_path: str | Path) -> bool:
        """Check if this importer handles the specified file path or content."""

    def _sanitize_path(self, path_str: str | None) -> str:
        """Normalize target folder path strings for Sortify configuration."""
        if not path_str:
            return ""
        clean = str(path_str).strip().replace("\\", "/")
        clean = clean.strip("\"'")
        return clean

    def _resolve_safe_destination(
        self,
        raw_path: str | None,
        rule_name: str,
        result: ImportResult,
        source_label: str,
    ) -> str | None:
        """Convert and validate an imported target path into a safe relative folder.

        Returns ``""`` when ``raw_path`` is empty/missing, ``None`` when ``raw_path``
        cannot become a safe relative folder (recording a skip warning), or the
        validated relative folder path (recording a preview warning if converted).
        """
        if not raw_path or not str(raw_path).strip():
            return ""
        raw_str = str(raw_path)
        normalized, was_converted, err = normalize_imported_target_path(raw_str)
        if err is not None:
            result.warnings.append(
                f"{source_label} '{rule_name}' skipped: unsafe destination path '{raw_str.strip()}' ({err})"
            )
            result.unsupported.append(
                {
                    "rule": rule_name,
                    "reason": f"Unsafe destination path '{raw_str.strip()}': {err}",
                }
            )
            return None
        if was_converted:
            result.warnings.append(
                f"{source_label} '{rule_name}': converted destination path '{raw_str.strip()}' -> '{normalized}'."
            )
        return normalized

    def _is_script_action(self, action_data: Any) -> bool:
        """Detect embedded scripts or command executions to ensure safe parsing."""
        if isinstance(action_data, str):
            lower = action_data.lower()
            return any(
                k in lower
                for k in (
                    "script",
                    "applescript",
                    "shell",
                    "bash",
                    "python",
                    "powershell",
                    "cmd.exe",
                    "exec",
                )
            )
        elif isinstance(action_data, dict):
            act_type = str(
                action_data.get("type", "")
                or action_data.get("action", "")
                or action_data.get("NSRuleActionType", "")
                or action_data.get("Action", "")
            ).lower()
            return any(
                k in act_type
                for k in (
                    "script",
                    "applescript",
                    "shell",
                    "bash",
                    "python",
                    "powershell",
                    "cmd",
                    "exec",
                    "run",
                )
            )
        return False

    def _classify_action_kind(self, action_data: Any) -> tuple[str, str]:
        """Classify an external rule action into ``move``, ``unarchive``, ``copy``, ``script``, or ``unsupported``."""
        if self._is_script_action(action_data):
            if isinstance(action_data, str):
                return "script", action_data.strip() or "script"
            if isinstance(action_data, dict):
                lbl = (
                    action_data.get("NSRuleActionType")
                    or action_data.get("type")
                    or action_data.get("action")
                    or action_data.get("Action")
                    or "script"
                )
                return "script", str(lbl)
            return "script", "script"

        raw_label = ""
        if isinstance(action_data, str):
            raw_label = action_data.strip()
            if "/" in raw_label or "\\" in raw_label:
                return "move", "Move"
        elif isinstance(action_data, dict):
            raw_label = str(
                action_data.get("NSRuleActionType")
                or action_data.get("type")
                or action_data.get("action")
                or action_data.get("Action")
                or action_data.get("Type")
                or ""
            ).strip()

        if not raw_label:
            return "move", "Move"

        compact = re.sub(r"[^a-z0-9]+", "", raw_label.lower())
        core = (
            compact[len("nsruleaction") :]
            if compact.startswith("nsruleaction")
            else compact
        )

        if core in _TRASH_ACTION_CORES:
            return "trash", raw_label
        if core in _UNARCHIVE_ACTION_CORES:
            return "unarchive", raw_label
        if core in _MOVE_ACTION_CORES:
            return "move", raw_label
        if core in _RENAME_ACTION_CORES:
            return "rename", raw_label
        if core in _COPY_ACTION_CORES:
            return "copy", raw_label
        return "unsupported", raw_label

    def _validate_rule_action(
        self,
        action_data: Any,
        rule_name: str,
        result: ImportResult,
        source_label: str,
        *,
        embedded_script: bool = False,
        record_script: bool = True,
    ) -> tuple[bool, str]:
        """Classify an action and record unsupported or script omissions when disallowed."""
        act_kind, act_label = self._classify_action_kind(action_data)
        if act_kind == "script":
            if record_script:
                if embedded_script:
                    reason = "Embedded script action omitted for security."
                    warning = (
                        f"{source_label} '{rule_name}' contains embedded script action "
                        "which was omitted for security."
                    )
                else:
                    reason = f"External script/command action '{act_label}' omitted."
                    warning = (
                        f"{source_label} '{rule_name}' contains command/script execution "
                        "action which was omitted for security."
                    )
                result.unsupported.append({"rule": rule_name, "reason": reason})
                result.warnings.append(warning)
            return False, "script"
        if act_kind == "copy":
            result.unsupported.append(
                {
                    "rule": rule_name,
                    "reason": f"Unsupported copy action '{act_label}' omitted.",
                }
            )
            result.warnings.append(
                f"{source_label} '{rule_name}' skipped unsupported copy action '{act_label}'."
            )
            return False, "copy"
        if act_kind == "unsupported":
            result.unsupported.append(
                {
                    "rule": rule_name,
                    "reason": f"Unsupported action '{act_label}' omitted.",
                }
            )
            result.warnings.append(
                f"{source_label} '{rule_name}' skipped unsupported action '{act_label}'."
            )
            return False, "unsupported"
        return True, act_kind

    def _validate_condition_clause(
        self,
        raw_attr: str,
        raw_op: str,
        negated_flag: bool,
        rule_name: str,
        result: ImportResult,
        source_label: str,
        *,
        reject_metadata_attrs: bool = False,
        detailed_operator_messages: bool = False,
        operator_noun: str = "condition operator",
    ) -> str | None:
        """Validate condition attribute and operator, returning supported operator kind or ``None``."""
        if reject_metadata_attrs:
            attr_kind = _classify_condition_attribute(raw_attr)
            if attr_kind is not None:
                result.warnings.append(
                    f"{source_label} '{rule_name}' skipped unsupported {attr_kind} filter ('{raw_attr}')."
                )
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"Unsupported {attr_kind} filter '{raw_attr}' omitted.",
                    }
                )
                return None

        op_kind = _classify_condition_operator(raw_op, is_negated_flag=negated_flag)
        if op_kind not in ("negated", "numeric", "unsupported"):
            return op_kind

        if detailed_operator_messages:
            if op_kind == "negated":
                desc = raw_op or "negated"
                warn_detail = f"negated condition operator '{desc}'"
                reason = f"Negated condition operator '{desc}' omitted."
            elif op_kind == "numeric":
                warn_detail = f"numeric/date comparison operator '{raw_op}'"
                reason = f"Numeric/date comparison operator '{raw_op}' omitted."
            else:
                warn_detail = f"unsupported condition operator '{raw_op}'"
                reason = f"Unsupported condition operator '{raw_op}' omitted."
        else:
            desc = raw_op or "negated"
            warn_detail = f"unsupported {operator_noun} '{desc}'"
            reason = f"Unsupported {operator_noun} '{desc}' omitted."

        result.warnings.append(f"{source_label} '{rule_name}' skipped {warn_detail}.")
        result.unsupported.append({"rule": rule_name, "reason": reason})
        return None

    def _emit_mask_rule(
        self,
        raw_mask: Any,
        target_dir: str,
        result: ImportResult,
        *,
        rule_name: str,
        source_label: str,
        priority: int = 75,
        unsupported_key: str = "rule",
        mask_noun: str = "mask",
        negated_prefixes: tuple[str, ...] = ("!", "-"),
        check_inline_filters: bool = True,
        op_kind: str = "supported",
        attribute: str = "",
        normalize_bare_wildcard_ext: bool = False,
        is_unarchive: bool = False,
        is_trash: bool = False,
        rename_pattern: str = "",
    ) -> str | None:
        """Validate a pattern/keyword mask and emit the mapped keyword rule or policy."""
        if raw_mask is None:
            return None
        val_clean = str(raw_mask).strip()
        if not val_clean:
            return None

        if val_clean.startswith(negated_prefixes) and len(val_clean) > 1:
            if unsupported_key == "tag":
                result.warnings.append(
                    f"TagSpaces tag '{val_clean}' skipped negated pattern."
                )
                result.unsupported.append(
                    {"tag": val_clean, "reason": "Negated tag pattern omitted."}
                )
            else:
                result.warnings.append(
                    f"{source_label} '{rule_name}' skipped negated {mask_noun} '{val_clean}'."
                )
                result.unsupported.append(
                    {
                        unsupported_key: rule_name,
                        "reason": f"Negated {mask_noun} '{val_clean}' omitted.",
                    }
                )
            return None

        if check_inline_filters:
            inline_kind = _classify_inline_filter_token(val_clean)
            if inline_kind is not None:
                result.warnings.append(
                    f"{source_label} '{rule_name}' skipped unsupported {inline_kind} filter '{val_clean}'."
                )
                result.unsupported.append(
                    {
                        unsupported_key: rule_name,
                        "reason": f"Unsupported {inline_kind} filter '{val_clean}' omitted.",
                    }
                )
                return None

        is_pattern = False
        expr = val_clean
        if op_kind == "starts_with":
            is_pattern = True
            expr = val_clean if val_clean.endswith("*") else f"{val_clean}*"
        elif op_kind == "ends_with":
            is_pattern = True
            expr = val_clean if val_clean.startswith("*") else f"*{val_clean}"
        elif (
            "extension" in attribute
            or "pattern" in attribute
            or _has_wildcard_or_ext_mask(val_clean)
        ):
            is_pattern = True
            if (
                "extension" in attribute
                or "pattern" in attribute
                or (normalize_bare_wildcard_ext and "*" in val_clean)
            ):
                expr = (
                    val_clean
                    if val_clean.startswith("*") or "." in val_clean
                    else f"*.{val_clean}"
                )

        if is_pattern:
            pol_dict: dict[str, Any] = {
                "type": "unarchive" if is_unarchive else "pattern",
                "expression": expr,
                "target_path": "" if is_trash else target_dir,
                "priority": priority,
                "halting": False,
            }
            if is_trash:
                pol_dict["action"] = "trash"
            elif is_unarchive:
                pol_dict["action"] = "unarchive"
            if rename_pattern and not is_trash:
                pol_dict["rename_pattern"] = rename_pattern
            result.policies.append(pol_dict)
            return expr

        if is_trash:
            result.policies.append(
                {
                    "type": "keyword",
                    "action": "trash",
                    "expression": val_clean,
                    "target_path": "",
                    "priority": priority,
                    "halting": False,
                }
            )
        elif is_unarchive:
            pol_dict = {
                "type": "unarchive",
                "action": "unarchive",
                "expression": val_clean,
                "target_path": target_dir,
                "priority": priority,
                "halting": False,
            }
            if rename_pattern:
                pol_dict["rename_pattern"] = rename_pattern
            result.policies.append(pol_dict)
        elif (
            len(val_clean.split()) == 1
            and all(c.isalnum() or c in "_-" for c in val_clean)
            and not rename_pattern
        ):
            result.keyword_rules[val_clean.lower()] = target_dir
        else:
            pol_dict = {
                "type": "keyword",
                "expression": val_clean,
                "target_path": target_dir,
                "priority": priority,
                "halting": False,
            }
            if rename_pattern:
                pol_dict["rename_pattern"] = rename_pattern
            result.policies.append(pol_dict)

        return val_clean


class HazelRuleImporter(BaseRuleImporter):
    """Importer strategy for Hazel .hazelrules XML/plist packages."""

    def can_import(self, file_path: str | Path) -> bool:
        """Return True if file extension is .hazelrules/.plist or header resembles plist."""
        p = Path(file_path)
        if p.suffix.lower() in (".hazelrules", ".plist"):
            return True
        try:
            with open(p, "rb") as f:
                header = f.read(100)
                return b"plist" in header or b"bplist" in header
        except OSError:
            return False

    def parse_file(self, file_path: str | Path) -> ImportResult:
        """Parse Hazel .hazelrules plist file into Sortify rules."""
        result = ImportResult(source_format="Hazel (.hazelrules)")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        plist_data = None
        try:
            with open(p, "rb") as f:
                plist_data = plistlib.load(f)
        except (
            plistlib.InvalidFileException,
            ExpatError,
            OSError,
            ValueError,
            TypeError,
            OverflowError,
            EOFError,
        ) as exc:
            logger.warning(
                f"Failed to parse plist file '{file_path}': {exc}", exc_info=True
            )
            result.warnings.append(
                f"Failed to parse Hazel plist file '{p.name}': {exc}"
            )
            return result

        if not plist_data:
            result.warnings.append(f"Hazel rule package '{p.name}' is empty.")
            return result

        rules_list: list[Any] = []
        folder_throw_away_duplicates = False
        if isinstance(plist_data, dict):
            folder_opts = (
                plist_data.get("FolderOptions")
                or plist_data.get("NSFolderOptions")
                or plist_data
            )
            if isinstance(folder_opts, dict) and any(
                bool(folder_opts.get(k))
                for k in (
                    "RemoveDuplicates",
                    "DuplicateFiles",
                    "ThrowAwayDuplicates",
                    "NSRemoveDuplicates",
                )
            ):
                folder_throw_away_duplicates = True
            rules_list = (
                plist_data.get("Rules")
                or plist_data.get("HazelRules")
                or plist_data.get("rules")
                or []
            )
            if not rules_list and ("NSRuleName" in plist_data or "Name" in plist_data):
                rules_list = [plist_data]
        elif isinstance(plist_data, list):
            rules_list = plist_data

        if not isinstance(rules_list, list):
            result.warnings.append(
                f"Unrecognized Hazel rule package structure in '{p.name}'."
            )
            return result

        if folder_throw_away_duplicates:
            result.policies.append(
                {
                    "type": "duplicate",
                    "action": "trash",
                    "expression": "*",
                    "target_path": "",
                    "priority": 80,
                    "halting": False,
                }
            )

        for idx, rule in enumerate(rules_list):
            if not isinstance(rule, dict):
                continue

            rule_name = str(
                rule.get("NSRuleName")
                or rule.get("Name")
                or rule.get("title")
                or f"Rule #{idx + 1}"
            )

            actions = (
                rule.get("NSRuleActions")
                or rule.get("Actions")
                or rule.get("NSActions")
                or []
            )
            if isinstance(actions, (dict, str)):
                actions = [actions]

            raw_target = ""
            raw_rename_pattern = ""
            is_unarchive = False
            is_trash = False
            throw_away_duplicate = bool(
                rule.get("ThrowAwayDuplicate")
                or rule.get("NSRuleThrowAwayDuplicate")
                or rule.get("throwAwayDuplicate")
            )
            saw_explicit_actions = False
            saw_supported_action = False

            if isinstance(actions, list):
                for act in actions:
                    saw_explicit_actions = True
                    supported, act_kind = self._validate_rule_action(
                        act,
                        rule_name,
                        result,
                        "Hazel rule",
                        embedded_script=True,
                    )
                    if not supported:
                        continue

                    saw_supported_action = True
                    if act_kind == "unarchive":
                        is_unarchive = True
                    elif act_kind == "trash":
                        is_trash = True

                    if isinstance(act, dict):
                        if any(
                            bool(act.get(k))
                            for k in (
                                "ThrowAwayDuplicate",
                                "NSRuleThrowAwayDuplicate",
                                "throwAwayDuplicate",
                            )
                        ):
                            throw_away_duplicate = True
                        act_target = (
                            act.get("DestinationPath")
                            or act.get("Destination")
                            or act.get("target")
                            or act.get("folder")
                            or act.get("path")
                            or act.get("TargetFolder")
                        )
                        if act_target and isinstance(act_target, str):
                            if _is_system_trash_destination(act_target):
                                is_trash = True
                            else:
                                raw_target = act_target
                        act_rename = (
                            act.get("RenamePattern")
                            or act.get("rename_pattern")
                            or act.get("NewName")
                            or (
                                act.get("pattern")
                                or act.get("value")
                                or act.get("name")
                                if act_kind == "rename"
                                else None
                            )
                        )
                        if act_rename and isinstance(act_rename, str):
                            raw_rename_pattern = act_rename
                    elif isinstance(act, str) and ("/" in act or "\\" in act):
                        if _is_system_trash_destination(act):
                            is_trash = True
                        else:
                            raw_target = act

            top_action_raw = (
                rule.get("NSRuleActionType")
                or rule.get("Action")
                or rule.get("action")
                or ""
            )
            if top_action_raw:
                supported, top_kind = self._validate_rule_action(
                    top_action_raw,
                    rule_name,
                    result,
                    "Hazel rule",
                    embedded_script=True,
                )
                if not supported:
                    continue
                if top_kind == "unarchive":
                    is_unarchive = True
                    saw_supported_action = True
                elif top_kind == "trash":
                    is_trash = True
                    saw_supported_action = True

            if not raw_rename_pattern:
                top_rename = rule.get("RenamePattern") or rule.get("rename_pattern")
                if top_rename and isinstance(top_rename, str):
                    raw_rename_pattern = top_rename

            rule_rename_pattern = ""
            if raw_rename_pattern and not is_trash:
                norm_ren, ren_err = _normalize_imported_rename_pattern(
                    raw_rename_pattern
                )
                if ren_err is not None:
                    result.warnings.append(
                        f"Hazel rule '{rule_name}' skipped invalid rename pattern '{raw_rename_pattern}': {ren_err}"
                    )
                    result.unsupported.append({"rule": rule_name, "reason": ren_err})
                else:
                    rule_rename_pattern = norm_ren

            if not raw_target and (not saw_explicit_actions or saw_supported_action):
                top_target = (
                    rule.get("DestinationPath")
                    or rule.get("Destination")
                    or rule.get("TargetFolder")
                )
                if top_target and isinstance(top_target, str):
                    if _is_system_trash_destination(top_target):
                        is_trash = True
                    else:
                        raw_target = top_target

            if is_trash:
                target_dir = ""
            else:
                if not raw_target:
                    if saw_explicit_actions and not saw_supported_action:
                        continue
                    result.warnings.append(
                        f"Hazel rule '{rule_name}' has no target destination folder; omitted auto-routing."
                    )
                    result.unsupported.append(
                        {"rule": rule_name, "reason": "Missing target destination path"}
                    )
                    continue

                resolved_dir = self._resolve_safe_destination(
                    raw_target, rule_name, result, "Hazel rule"
                )
                if resolved_dir is None:
                    continue
                target_dir = resolved_dir

            cond_mode = (
                str(
                    rule.get("NSRuleConditionMode")
                    or rule.get("ConditionMode")
                    or rule.get("matchType")
                    or ""
                )
                .strip()
                .lower()
            )
            if cond_mode in ("none", "neither", "not"):
                result.warnings.append(
                    f"Hazel rule '{rule_name}' skipped negated condition group ('{cond_mode}')."
                )
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"Negated condition group '{cond_mode}' is not supported.",
                    }
                )
                continue

            conditions = (
                rule.get("NSRuleConditions")
                or rule.get("Conditions")
                or rule.get("NSConditions")
                or []
            )

            if not isinstance(conditions, list) or not conditions:
                if is_trash:
                    expr_val = rule_name if rule_name else "*"
                    p_type = (
                        "pattern" if _has_wildcard_or_ext_mask(expr_val) else "keyword"
                    )
                    result.policies.append(
                        {
                            "type": p_type,
                            "action": "trash",
                            "expression": expr_val,
                            "target_path": "",
                            "priority": 75,
                            "halting": False,
                        }
                    )
                elif is_unarchive:
                    expr_val = rule_name if rule_name else "*"
                    p_type = (
                        "pattern"
                        if _has_wildcard_or_ext_mask(expr_val)
                        else "unarchive"
                    )
                    pol_no_cond: dict[str, Any] = {
                        "type": p_type,
                        "action": "unarchive",
                        "expression": expr_val,
                        "target_path": target_dir,
                        "priority": 75,
                        "halting": False,
                    }
                    if rule_rename_pattern:
                        pol_no_cond["rename_pattern"] = rule_rename_pattern
                    result.policies.append(pol_no_cond)
                elif (
                    rule_name
                    and len(rule_name.split()) == 1
                    and not rule_rename_pattern
                ):
                    result.keyword_rules[rule_name.lower()] = target_dir
                else:
                    pol_no_cond = {
                        "type": "keyword",
                        "expression": rule_name,
                        "target_path": target_dir,
                        "priority": 75,
                        "halting": False,
                    }
                    if rule_rename_pattern:
                        pol_no_cond["rename_pattern"] = rule_rename_pattern
                    result.policies.append(pol_no_cond)
                if throw_away_duplicate and not is_trash:
                    result.policies.append(
                        {
                            "type": "duplicate",
                            "action": "trash",
                            "expression": rule_name if rule_name else "*",
                            "target_path": "",
                            "priority": 80,
                            "halting": False,
                        }
                    )
                continue

            def _has_nested_subconditions(c: dict[str, Any]) -> bool:
                return any(
                    isinstance(c.get(k), list)
                    for k in (
                        "subconditions",
                        "NSRuleSubconditions",
                        "Subconditions",
                        "NSRuleConditions",
                        "Conditions",
                        "NSConditions",
                        "conditions",
                        "subrules",
                    )
                )

            dict_conditions = [c for c in conditions if isinstance(c, dict)]
            has_nested_group = any(
                _has_nested_subconditions(c) for c in dict_conditions
            )
            is_multi_condition = len(dict_conditions) > 1 or has_nested_group
            if has_nested_group:
                result.warnings.append(
                    f"Hazel rule '{rule_name}' contains nested boolean multi-condition groups which are not supported in flat policies."
                )
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": "Nested boolean multi-condition groups are not supported.",
                    }
                )
            elif len(dict_conditions) > 1:
                result.warnings.append(
                    f"Hazel rule '{rule_name}' contains multiple conditions ({len(dict_conditions)} clauses); compound boolean rules are flattened or skipped."
                )

            def _attach_optional_rename(p_dict: dict[str, Any]) -> dict[str, Any]:
                if rule_rename_pattern and not is_trash:
                    p_dict["rename_pattern"] = rule_rename_pattern
                return p_dict

            for cond in conditions:
                if not isinstance(cond, dict):
                    continue

                if _has_nested_subconditions(cond):
                    continue

                raw_attr = str(
                    cond.get("target")
                    or cond.get("attribute")
                    or cond.get("Target")
                    or cond.get("Attribute")
                    or cond.get("NSRuleConditionTarget")
                    or ""
                )
                attribute = raw_attr.lower()

                raw_op = str(
                    cond.get("operator")
                    or cond.get("qualifier")
                    or cond.get("Operator")
                    or cond.get("Qualifier")
                    or cond.get("NSRuleConditionOperator")
                    or ""
                )
                negated_flag = bool(
                    cond.get("negated")
                    or cond.get("isNegated")
                    or cond.get("Negated")
                    or cond.get("NSRuleConditionNegated")
                    or cond.get("not")
                )

                val = (
                    cond.get("value")
                    or cond.get("pattern")
                    or cond.get("Value")
                    or cond.get("Pattern")
                    or cond.get("NSRuleConditionValue")
                    or cond.get("string")
                    or cond.get("String")
                )

                if any(
                    tok in attribute
                    for tok in ("duplicate", "isduplicate", "duplicatefile")
                ):
                    result.policies.append(
                        {
                            "type": "duplicate",
                            "action": "trash" if is_trash else "archive",
                            "expression": "*",
                            "target_path": target_dir,
                            "priority": 80,
                            "halting": False,
                        }
                    )
                    continue

                attr_filter_kind = _classify_condition_attribute(raw_attr)
                if attr_filter_kind is not None:
                    if is_multi_condition:
                        result.warnings.append(
                            f"Hazel rule '{rule_name}' skipped multi-condition {attr_filter_kind} filter clause ('{raw_attr}'); metadata rules require a single condition."
                        )
                        result.unsupported.append(
                            {
                                "rule": rule_name,
                                "reason": f"Multi-condition {attr_filter_kind} filter attribute '{raw_attr}' omitted.",
                            }
                        )
                        continue

                    meta_type, meta_expr, meta_err = _map_hazel_metadata_condition(
                        raw_attr,
                        raw_op,
                        val,
                        negated_flag=negated_flag,
                    )
                    if meta_type and meta_expr and meta_err is None:
                        meta_pol: dict[str, Any] = {
                            "type": meta_type,
                            "expression": meta_expr,
                            "target_path": "" if is_trash else target_dir,
                            "priority": 75,
                            "halting": False,
                        }
                        if is_trash:
                            meta_pol["action"] = "trash"
                        elif is_unarchive:
                            meta_pol["action"] = "unarchive"
                        result.policies.append(_attach_optional_rename(meta_pol))
                        if throw_away_duplicate and not is_trash:
                            result.policies.append(
                                {
                                    "type": "duplicate",
                                    "action": "trash",
                                    "expression": "*",
                                    "target_path": "",
                                    "priority": 80,
                                    "halting": False,
                                }
                            )
                    else:
                        reason_msg = (
                            meta_err
                            or f"Unsupported {attr_filter_kind} filter attribute '{raw_attr}' omitted."
                        )
                        result.warnings.append(
                            f"Hazel rule '{rule_name}' skipped unsupported {attr_filter_kind} filter clause ('{raw_attr}'): {reason_msg}"
                        )
                        result.unsupported.append(
                            {
                                "rule": rule_name,
                                "reason": reason_msg,
                            }
                        )
                    continue

                op_kind = self._validate_condition_clause(
                    raw_attr,
                    raw_op,
                    negated_flag,
                    rule_name,
                    result,
                    "Hazel rule",
                    detailed_operator_messages=True,
                )
                if op_kind is None:
                    continue

                matched_expr_for_dup = self._emit_mask_rule(
                    val,
                    target_dir,
                    result,
                    rule_name=rule_name,
                    source_label="Hazel rule",
                    negated_prefixes=("!",),
                    op_kind=op_kind,
                    attribute=attribute,
                    normalize_bare_wildcard_ext=True,
                    is_unarchive=is_unarchive,
                    is_trash=is_trash,
                    rename_pattern=rule_rename_pattern,
                )
                if matched_expr_for_dup is None:
                    continue

                if throw_away_duplicate and not is_trash:
                    result.policies.append(
                        {
                            "type": "duplicate",
                            "action": "trash",
                            "expression": matched_expr_for_dup,
                            "target_path": "",
                            "priority": 80,
                            "halting": False,
                        }
                    )

        return result


class DropItRuleImporter(BaseRuleImporter):
    """Importer strategy for DropIt XML, JSON, and INI profile export packages."""

    def can_import(self, file_path: str | Path) -> bool:
        """Return True if file extension or content matches DropIt export formats."""
        p = Path(file_path)
        ext = p.suffix.lower()
        if ext in (".xml", ".json", ".dropit", ".ini"):
            return True
        return False

    def parse_file(self, file_path: str | Path) -> ImportResult:
        """Parse DropIt XML, JSON, or INI profile export file into Sortify rules."""
        result = ImportResult(source_format="DropIt Export")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        content = ""
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
        except OSError as exc:
            logger.warning(f"Failed to read file '{file_path}': {exc}")
            result.warnings.append(f"Failed to read file '{p.name}': {exc}")
            return result

        content_strip = content.strip()
        if not content_strip:
            result.warnings.append(f"DropIt export file '{p.name}' is empty.")
            return result

        if p.suffix.lower() == ".ini":
            return self._parse_ini(content_strip, p.name, result)
        elif content_strip.startswith("<") or p.suffix.lower() == ".xml":
            return self._parse_xml(content_strip, p.name, result)
        elif p.suffix.lower() == ".json":
            return self._parse_json(content_strip, p.name, result)
        elif re.match(r"^\[[\w\s.-]+\]\s*$", content_strip.splitlines()[0].strip()):
            return self._parse_ini(content_strip, p.name, result)
        else:
            return self._parse_json(content_strip, p.name, result)

    def _inspect_dropit_extra_filters(
        self,
        rule_name: str,
        extra_fields: dict[str, Any],
        result: ImportResult,
    ) -> tuple[bool, bool]:
        """Inspect DropIt rule metadata keys for unsupported size, date, or negation clauses.

        Returns ``(abort_entire_rule, omitted_any_clause)``.
        """
        omitted_any = False
        for raw_k, raw_v in extra_fields.items():
            if raw_v is None:
                continue
            v_str = str(raw_v).strip()
            if not v_str:
                continue
            k_norm = raw_k.strip().lower().replace("-", "_")

            if k_norm in _DROPIT_NEGATE_KEYS:
                if isinstance(raw_v, bool) and raw_v:
                    is_neg = True
                else:
                    is_neg = v_str.lower() in ("1", "true", "yes", "on")
                if is_neg:
                    result.warnings.append(
                        f"DropIt rule '{rule_name}' skipped negated rule condition ('{raw_k}={v_str}')."
                    )
                    result.unsupported.append(
                        {
                            "rule": rule_name,
                            "reason": f"Negated rule condition '{raw_k}={v_str}' omitted.",
                        }
                    )
                    return True, True

            if k_norm == "operator":
                if (
                    self._validate_condition_clause(
                        "",
                        v_str,
                        False,
                        rule_name,
                        result,
                        "DropIt rule",
                        operator_noun="operator",
                    )
                    is None
                ):
                    return True, True

            if k_norm in _DROPIT_SIZE_KEYS:
                result.warnings.append(
                    f"DropIt rule '{rule_name}' skipped unsupported size filter ('{raw_k}={v_str}')."
                )
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"Unsupported size filter '{raw_k}={v_str}' omitted.",
                    }
                )
                omitted_any = True
            elif k_norm in _DROPIT_DATE_KEYS:
                result.warnings.append(
                    f"DropIt rule '{rule_name}' skipped unsupported date filter ('{raw_k}={v_str}')."
                )
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"Unsupported date filter '{raw_k}={v_str}' omitted.",
                    }
                )
                omitted_any = True
            elif k_norm in _DROPIT_EXCLUDE_KEYS:
                result.warnings.append(
                    f"DropIt rule '{rule_name}' skipped negated/excluded filter ('{raw_k}={v_str}')."
                )
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"Excluded pattern '{raw_k}={v_str}' omitted.",
                    }
                )
                omitted_any = True

        return False, omitted_any

    def _process_dropit_entry(
        self,
        rule_name: str,
        pattern: str,
        dest: str,
        action: str,
        result: ImportResult,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        """Validate action, destination, and filter clauses for a single DropIt association."""
        supported, act_kind = self._validate_rule_action(
            action, rule_name, result, "DropIt rule"
        )
        if not supported:
            return

        is_unarchive = act_kind == "unarchive"
        is_trash = act_kind == "trash" or _is_system_trash_destination(dest)

        if is_trash and extra_fields:
            for raw_k, raw_v in extra_fields.items():
                k_compact = re.sub(r"[^a-z0-9]+", "", str(raw_k).lower())
                if k_compact in ("deletemode", "deletionmode", "erasemode"):
                    mode_compact = re.sub(r"[^a-z0-9]+", "", str(raw_v or "").lower())
                    if mode_compact in _PERMANENT_DELETE_MODES:
                        result.unsupported.append(
                            {
                                "rule": rule_name,
                                "reason": f"Permanent delete mode '{raw_v}' omitted for safety.",
                            }
                        )
                        result.warnings.append(
                            f"DropIt rule '{rule_name}' skipped permanent delete mode '{raw_v}'."
                        )
                        return

        if is_trash:
            target_dir = ""
        else:
            if not dest or not str(dest).strip():
                result.warnings.append(
                    f"DropIt rule '{rule_name}' has no target directory; omitted auto-routing."
                )
                result.unsupported.append(
                    {"rule": rule_name, "reason": "Missing target destination path"}
                )
                return

            resolved_dir = self._resolve_safe_destination(
                str(dest), rule_name, result, "DropIt rule"
            )
            if resolved_dir is None:
                return
            target_dir = resolved_dir

        omitted_extra = False
        if extra_fields:
            abort_rule, omitted_extra = self._inspect_dropit_extra_filters(
                rule_name, extra_fields, result
            )
            if abort_rule:
                return

        self._process_pattern_and_dest(
            rule_name,
            pattern,
            target_dir,
            result,
            is_unarchive=is_unarchive,
            is_trash=is_trash,
            allow_name_fallback=not omitted_extra,
        )

    def _parse_ini(
        self, ini_content: str, file_name: str, result: ImportResult
    ) -> ImportResult:
        """Parse DropIt INI profile representation."""
        result.source_format = "DropIt INI Profile"
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read_string(ini_content)
        except (configparser.Error, OSError) as exc:
            logger.warning(f"Failed to parse INI in '{file_name}': {exc}")
            result.warnings.append(
                f"Failed to parse INI in DropIt profile '{file_name}': {exc}"
            )
            return result

        if config.has_section("Associations"):
            for _key, val in config.items("Associations"):
                val_str = str(val).strip()
                if "**" in val_str:
                    parts = val_str.split("**")
                    rule_name = parts[0].strip()
                    pattern = parts[1].strip() if len(parts) > 1 else rule_name
                    action = parts[2].strip() if len(parts) > 2 else "Move"
                    dest = parts[3].strip() if len(parts) > 3 else ""
                    self._process_dropit_entry(rule_name, pattern, dest, action, result)

        for sec_name in config.sections():
            sec_lower = sec_name.lower()
            if sec_lower in (
                "dropit",
                "general",
                "settings",
                "profile",
                "associations",
            ):
                if not any(
                    k in config[sec_name]
                    for k in (
                        "rules",
                        "pattern",
                        "destination",
                        "directory",
                        "folder",
                        "target",
                        "action",
                        "type",
                    )
                ):
                    continue

            sec = config[sec_name]
            rule_name = (
                sec.get("Name") or sec.get("title") or sec.get("name") or sec_name
            )
            pattern = (
                sec.get("Rules")
                or sec.get("Pattern")
                or sec.get("Filter")
                or sec.get("Mask")
                or ""
            )
            dest = (
                sec.get("Destination")
                or sec.get("Directory")
                or sec.get("Folder")
                or sec.get("Target")
                or ""
            )
            action = sec.get("Action") or sec.get("Type") or "Move"
            extra = dict(sec.items())
            self._process_dropit_entry(
                rule_name, pattern, dest, action, result, extra_fields=extra
            )

        if not result.keyword_rules and not result.policies and not result.warnings:
            result.warnings.append(
                f"No convertible rules found in DropIt INI profile '{file_name}'."
            )

        return result

    def _parse_xml(
        self, xml_content: str, file_name: str, result: ImportResult
    ) -> ImportResult:
        """Parse DropIt XML profile representation."""
        result.source_format = "DropIt XML Profile"
        try:
            root = ET.fromstring(xml_content)
        except (ET.ParseError, DefusedXmlException, ValueError) as exc:
            logger.warning(f"Failed to parse XML in '{file_name}': {exc}")
            result.warnings.append(
                f"Failed to parse XML in DropIt profile '{file_name}': {exc}"
            )
            return result

        items = []
        for elem in root.iter():
            tag_lower = elem.tag.lower()
            if tag_lower in ("association", "rule", "item", "profileitem"):
                items.append(elem)

        if not items and root.tag.lower() in ("association", "rule", "item"):
            items = [root]

        for idx, item in enumerate(items):
            rule_name = ""
            pattern = ""
            dest = ""
            action = ""
            extra_fields: dict[str, Any] = {}

            for child in item:
                ctag = child.tag.lower()
                cval = (child.text or "").strip()
                if ctag in ("name", "title"):
                    rule_name = cval
                elif ctag in ("rules", "pattern", "filter", "mask"):
                    pattern = cval
                elif ctag in ("destination", "directory", "folder", "target"):
                    dest = cval
                elif ctag in ("action", "type"):
                    action = cval
                else:
                    extra_fields[child.tag] = cval

            if not rule_name:
                rule_name = f"DropIt Rule #{idx + 1}"

            self._process_dropit_entry(
                rule_name,
                pattern,
                dest,
                action,
                result,
                extra_fields=extra_fields,
            )

        return result

    def _parse_json(
        self, json_content: str, file_name: str, result: ImportResult
    ) -> ImportResult:
        """Parse DropIt JSON profile representation."""
        result.source_format = "DropIt JSON Profile"
        try:
            data = json.loads(json_content)
        except ValueError as exc:
            logger.warning(f"Failed to parse JSON in '{file_name}': {exc}")
            result.warnings.append(
                f"Failed to parse JSON in DropIt profile '{file_name}': {exc}"
            )
            return result

        rules_list = []
        if isinstance(data, list):
            rules_list = data
        elif isinstance(data, dict):
            rules_list = (
                data.get("rules")
                or data.get("associations")
                or data.get("profile")
                or data.get("items")
                or []
            )
            if not rules_list and (
                "Pattern" in data or "Rules" in data or "Name" in data
            ):
                rules_list = [data]

        if not isinstance(rules_list, list):
            result.warnings.append(
                f"Unrecognized DropIt JSON profile structure in '{file_name}'."
            )
            return result

        for idx, rule in enumerate(rules_list):
            if not isinstance(rule, dict):
                continue

            rule_name = str(
                rule.get("Name")
                or rule.get("title")
                or rule.get("name")
                or f"DropIt Rule #{idx + 1}"
            )

            pattern = (
                rule.get("Pattern")
                or rule.get("Rules")
                or rule.get("pattern")
                or rule.get("filter")
                or ""
            )

            dest = (
                rule.get("Destination")
                or rule.get("Directory")
                or rule.get("folder")
                or rule.get("target")
                or rule.get("destination")
                or ""
            )

            action = str(
                rule.get("Action")
                or rule.get("action")
                or rule.get("Type")
                or rule.get("type")
                or ""
            )

            self._process_dropit_entry(
                rule_name,
                str(pattern),
                str(dest),
                action,
                result,
                extra_fields=rule,
            )

        return result

    def _process_pattern_and_dest(
        self,
        rule_name: str,
        pattern: str,
        target_dir: str,
        result: ImportResult,
        *,
        is_unarchive: bool = False,
        is_trash: bool = False,
        allow_name_fallback: bool = True,
    ) -> None:
        """Convert DropIt pattern mask string into keyword rules or policy rules."""
        pattern_clean = str(pattern or "").strip()
        if not pattern_clean:
            if not allow_name_fallback:
                return
            masks = [rule_name.strip()] if rule_name.strip() else []
        else:
            inc_part = pattern_clean
            if "#" in pattern_clean:
                inc_part, *exc_chunks = pattern_clean.split("#")
                for exc_chunk in exc_chunks:
                    for exc_tok in re.split(r"[;|,]", exc_chunk):
                        exc_clean = exc_tok.strip()
                        if exc_clean:
                            result.warnings.append(
                                f"DropIt rule '{rule_name}' skipped negated/excluded pattern mask '{exc_clean}'."
                            )
                            result.unsupported.append(
                                {
                                    "rule": rule_name,
                                    "reason": f"Excluded pattern mask '{exc_clean}' omitted.",
                                }
                            )
            masks = [m.strip() for m in re.split(r"[;|,]", inc_part) if m.strip()]

        for mask in masks:
            self._emit_mask_rule(
                mask,
                target_dir,
                result,
                rule_name=rule_name,
                source_label="DropIt rule",
                mask_noun="pattern mask",
                is_unarchive=is_unarchive,
                is_trash=is_trash,
            )


class TagSpacesRuleImporter(BaseRuleImporter):
    """Importer strategy for TagSpaces taggroup.json taxonomy files and smart rules."""

    def can_import(self, file_path: str | Path) -> bool:
        """Return True if file is a valid TagSpaces taggroup.json export or tag taxonomy JSON file."""
        p = Path(file_path)
        name_lower = p.name.lower()

        if (
            name_lower in ("taggroup.json", "taggroups.json")
            or "taggroup" in name_lower
            or "tagspaces" in name_lower
        ):
            return True

        if p.suffix.lower() != ".json":
            return False

        if not p.exists():
            return False

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False

        return self._is_tagspaces_data(data)

    def _is_tagspaces_data(self, data: Any) -> bool:
        """Check if parsed JSON data matches TagSpaces structure."""
        if isinstance(data, dict):
            for key in (
                "tagGroups",
                "taggroups",
                "tagGroup",
                "taggroup",
                "taggroupVersion",
                "tagspaces",
                "smartRules",
                "smart_rules",
            ):
                if key in data:
                    return True

            if any(
                k in data for k in ("Pattern", "Destination", "Association", "Profile")
            ):
                return False

            if ("title" in data or "name" in data) and (
                "children" in data or "tags" in data or "subgroups" in data
            ):
                return True

        elif isinstance(data, list):
            if not data:
                return False
            first = data[0]
            if isinstance(first, dict):
                if any(
                    k in first
                    for k in ("Pattern", "Destination", "Association", "Profile")
                ):
                    return False
                if ("title" in first or "name" in first) and (
                    "children" in first
                    or "tags" in first
                    or "color" in first
                    or "textcolor" in first
                    or "icon" in first
                    or "isSmart" in first
                ):
                    return True
                if ("name" in first or "title" in first) and (
                    "conditions" in first or "matchers" in first
                ):
                    return True

        return False

    def parse_file(self, file_path: str | Path) -> ImportResult:
        """Parse TagSpaces taggroup.json or smart rule JSON file into Sortify rules."""
        result = ImportResult(source_format="TagSpaces (taggroup.json)")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning(
                f"Failed to parse TagSpaces JSON file '{file_path}': {exc}",
                exc_info=True,
            )
            result.warnings.append(
                f"Failed to parse TagSpaces JSON file '{p.name}': {exc}"
            )
            return result

        if not data:
            result.warnings.append(f"TagSpaces rule package '{p.name}' is empty.")
            return result

        # 1. Process Tag Groups / Taxonomy Hierarchy
        tag_groups: list[Any] = []
        if isinstance(data, dict):
            tag_groups = (
                data.get("tagGroups")
                or data.get("taggroups")
                or data.get("tagGroup")
                or data.get("taggroup")
                or data.get("groups")
                or []
            )
            if not tag_groups and (
                "children" in data
                or "tags" in data
                or "title" in data
                or "name" in data
            ):
                tag_groups = [data]
        elif isinstance(data, list):
            tag_groups = [
                item
                for item in data
                if isinstance(item, dict)
                and (
                    "children" in item
                    or "tags" in item
                    or "color" in item
                    or "textcolor" in item
                )
            ]
            if (
                not tag_groups
                and data
                and all(
                    isinstance(item, dict) and ("title" in item or "name" in item)
                    for item in data
                )
            ):
                tag_groups = data

        for group in tag_groups:
            if isinstance(group, dict):
                self._traverse_tag_group(group, [], result)

        # 2. Process Smart Rules
        smart_rules: list[Any] = []
        if isinstance(data, dict):
            smart_rules = (
                data.get("smartRules")
                or data.get("smart_rules")
                or data.get("rules")
                or []
            )
        elif isinstance(data, list):
            smart_rules = [
                item
                for item in data
                if isinstance(item, dict)
                and (
                    "conditions" in item
                    or "matchers" in item
                    or "actions" in item
                    or "destination" in item
                    or "target" in item
                )
            ]

        for idx, rule in enumerate(smart_rules):
            if isinstance(rule, dict):
                self._process_smart_rule(rule, idx, result)

        if not result.keyword_rules and not result.policies and not result.warnings:
            result.warnings.append(
                f"No convertible TagSpaces tag groups or smart rules found in '{p.name}'."
            )

        return result

    def _traverse_tag_group(
        self,
        node: dict[str, Any],
        parent_path: list[str],
        result: ImportResult,
    ) -> None:
        """Recursively traverse nested tag group node and map tags to path hierarchies."""
        node_title = str(
            node.get("title")
            or node.get("name")
            or node.get("text")
            or node.get("label")
            or ""
        ).strip()

        current_path = list(parent_path)
        if node_title and node_title.lower() not in (
            "taggroups",
            "tag_groups",
            "root",
        ):
            current_path.append(node_title)

        raw_joined = "/".join(current_path)
        if raw_joined:
            safe_dir = self._resolve_safe_destination(
                raw_joined, node_title or raw_joined, result, "TagSpaces group"
            )
            if safe_dir is None:
                return
            target_dir = safe_dir
        else:
            target_dir = ""

        raw_tags = node.get("tags") or []
        if isinstance(raw_tags, (str, dict)):
            raw_tags = [raw_tags]

        raw_children = (
            node.get("children")
            or node.get("subgroups")
            or node.get("groups")
            or node.get("items")
            or []
        )
        if isinstance(raw_children, (str, dict)):
            raw_children = [raw_children]

        self._traverse_tag_entries(raw_tags, current_path, target_dir, result)
        self._traverse_tag_entries(
            raw_children,
            current_path,
            target_dir,
            result,
            include_styled_groups=True,
        )

    def _traverse_tag_entries(
        self,
        entries: list[Any],
        current_path: list[str],
        target_dir: str,
        result: ImportResult,
        *,
        include_styled_groups: bool = False,
    ) -> None:
        """Process a list of TagSpaces tag or subgroup entries recursively."""
        group_keys = (
            ("children", "subgroups", "groups", "tags", "color", "textcolor")
            if include_styled_groups
            else ("children", "subgroups", "groups", "tags")
        )
        for item in entries:
            if isinstance(item, str):
                self._add_tag_rule(item, target_dir, result)
            elif isinstance(item, dict):
                if any(k in item for k in group_keys):
                    self._traverse_tag_group(item, current_path, result)
                else:
                    tag_name = (
                        item.get("title")
                        or item.get("name")
                        or item.get("text")
                        or item.get("tag")
                        or item.get("label")
                        or ""
                    )
                    if tag_name:
                        self._add_tag_rule(str(tag_name), target_dir, result)

    def _add_tag_rule(
        self, tag_str: str, target_dir: str, result: ImportResult
    ) -> None:
        """Map a tag string to a keyword rule or policy based on single-word vs multi-word/pattern."""
        tag_clean = tag_str.strip()
        if not tag_clean:
            return

        if not target_dir:
            result.warnings.append(
                f"Tag '{tag_clean}' in group has no target directory; omitted auto-routing."
            )
            result.unsupported.append(
                {"tag": tag_clean, "reason": "Missing target destination path"}
            )
            return

        self._emit_mask_rule(
            tag_clean,
            target_dir,
            result,
            rule_name=tag_clean,
            source_label="TagSpaces tag",
            unsupported_key="tag",
            check_inline_filters=False,
        )

    def _process_smart_rule(
        self, rule: dict[str, Any], idx: int, result: ImportResult
    ) -> None:
        """Process a TagSpaces smart rule entry, detecting condition matchers and filtering unsupported actions."""
        rule_name = str(
            rule.get("name")
            or rule.get("title")
            or rule.get("ruleName")
            or f"TagSpaces Smart Rule #{idx + 1}"
        )

        actions = rule.get("actions") or rule.get("action") or []
        if isinstance(actions, (str, dict)):
            actions = [actions]

        has_script = False
        is_unarchive = False
        saw_non_script_unsupported = False
        action_dest = ""

        for act in actions:
            supported, act_kind = self._validate_rule_action(
                act,
                rule_name,
                result,
                "TagSpaces rule",
                record_script=False,
            )
            if not supported:
                if act_kind == "script":
                    has_script = True
                else:
                    saw_non_script_unsupported = True
                continue

            if act_kind == "unarchive":
                is_unarchive = True
            if isinstance(act, dict) and not action_dest:
                cand = (
                    act.get("target")
                    or act.get("destination")
                    or act.get("folder")
                    or act.get("path")
                    or ""
                )
                if cand:
                    action_dest = str(cand)

        if not has_script and self._is_script_action(rule):
            has_script = True

        if has_script:
            result.unsupported.append(
                {
                    "rule": rule_name,
                    "reason": "External script/command action omitted.",
                }
            )
            result.warnings.append(
                f"TagSpaces rule '{rule_name}' contains command/script execution action which was omitted for security."
            )

        top_dest = (
            rule.get("destination")
            or rule.get("target")
            or rule.get("folder")
            or rule.get("directory")
            or rule.get("targetPath")
            or ""
        )
        dest = str(top_dest) if top_dest else action_dest

        if not dest:
            if saw_non_script_unsupported:
                return
            result.warnings.append(
                f"TagSpaces rule '{rule_name}' has no target directory; omitted auto-routing."
            )
            result.unsupported.append(
                {"rule": rule_name, "reason": "Missing target destination path"}
            )
            return

        if saw_non_script_unsupported and not action_dest and not top_dest:
            return

        target_dir = self._resolve_safe_destination(
            dest, rule_name, result, "TagSpaces rule"
        )
        if target_dir is None:
            return

        conditions = (
            rule.get("conditions")
            or rule.get("condition")
            or rule.get("matchers")
            or rule.get("pattern")
            or []
        )
        if isinstance(conditions, (str, dict)):
            conditions = [conditions]

        for cond in conditions:
            if isinstance(cond, str):
                self._emit_mask_rule(
                    cond,
                    target_dir,
                    result,
                    priority=85,
                    rule_name=rule_name,
                    source_label="TagSpaces rule",
                    is_unarchive=is_unarchive,
                )
            elif isinstance(cond, dict):
                raw_cond_type = str(
                    cond.get("field")
                    or cond.get("type")
                    or cond.get("attribute")
                    or cond.get("target")
                    or ""
                )
                cond_type = raw_cond_type.lower()
                raw_op = str(
                    cond.get("operator")
                    or cond.get("op")
                    or cond.get("comparator")
                    or ""
                )
                neg_flag = bool(
                    cond.get("negated") or cond.get("isNegated") or cond.get("not")
                )

                if (
                    self._validate_condition_clause(
                        raw_cond_type,
                        raw_op,
                        neg_flag,
                        rule_name,
                        result,
                        "TagSpaces rule",
                        reject_metadata_attrs=True,
                    )
                    is None
                ):
                    continue

                val = str(
                    cond.get("value")
                    or cond.get("pattern")
                    or cond.get("expression")
                    or cond.get("val")
                    or ""
                ).strip()

                if not val:
                    continue

                if "ext" in cond_type or "extension" in cond_type:
                    expr = val if val.startswith("*.") else f"*.{val.lstrip('.')}"
                    pol_dict: dict[str, Any] = {
                        "type": "unarchive" if is_unarchive else "pattern",
                        "expression": expr,
                        "target_path": target_dir,
                        "priority": 85,
                        "halting": False,
                    }
                    if is_unarchive:
                        pol_dict["action"] = "unarchive"
                    result.policies.append(pol_dict)
                else:
                    self._emit_mask_rule(
                        val,
                        target_dir,
                        result,
                        priority=85,
                        rule_name=rule_name,
                        source_label="TagSpaces rule",
                        is_unarchive=is_unarchive,
                    )


class SortifyRuleImporter(BaseRuleImporter):
    """Importer strategy for native Sortify JSON rule backup packages."""

    def can_import(self, file_path: str | Path) -> bool:
        """Return True if file is a Sortify JSON rule backup file."""
        p = Path(file_path)
        if p.suffix.lower() != ".json":
            return False
        if not p.exists():
            return "sortify" in p.name.lower()
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if (
                    "KEYWORD_RULES" in data
                    or "POLICIES" in data
                    or data.get("format") == "sortify"
                ):
                    return True
                if "keyword_rules" in data and (
                    "policies" in data or data.get("source") == "Sortify"
                ):
                    return True
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return False
        return False

    def parse_file(self, file_path: str | Path) -> ImportResult:
        """Parse Sortify JSON backup file into Sortify rules."""
        result = ImportResult(source_format="Sortify JSON")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.warning(
                f"Failed to parse Sortify JSON file '{file_path}': {exc}",
                exc_info=True,
            )
            result.warnings.append(
                f"Failed to parse Sortify JSON file '{p.name}': {exc}"
            )
            return result

        if not isinstance(data, dict):
            result.warnings.append(
                f"Invalid Sortify rule file structure in '{p.name}'."
            )
            return result

        raw_kw = data.get("KEYWORD_RULES") or data.get("keyword_rules") or {}
        if isinstance(raw_kw, dict):
            for k, v in raw_kw.items():
                k_clean = str(k).strip()
                if not k_clean:
                    continue
                v_safe = self._resolve_safe_destination(
                    str(v), k_clean, result, "Sortify keyword rule"
                )
                if v_safe:
                    result.keyword_rules[k_clean.lower()] = v_safe

        raw_pol = data.get("POLICIES") or data.get("policies") or []
        if isinstance(raw_pol, list):
            for pol in raw_pol:
                if isinstance(pol, dict):
                    p_type = str(pol.get("type", "keyword")).strip().lower()
                    p_expr = str(pol.get("expression", "")).strip()
                    if not p_expr and p_type == "duplicate":
                        p_expr = "*"
                    if not p_expr:
                        continue
                    p_act = str(pol.get("action", "")).strip().lower()
                    raw_target_val = str(pol.get("target_path", ""))
                    if not raw_target_val.strip() and (
                        p_act == "trash" or p_type == "duplicate"
                    ):
                        p_target = ""
                    else:
                        p_target = self._resolve_safe_destination(
                            raw_target_val,
                            p_expr,
                            result,
                            "Sortify policy rule",
                        )
                        if not p_target:
                            continue
                    p_prio = pol.get("priority", 75)
                    p_halt = bool(pol.get("halting", False))
                    pol_out: dict[str, Any] = {
                        "type": p_type,
                        "expression": p_expr,
                        "target_path": p_target,
                        "priority": int(p_prio) if p_prio is not None else 75,
                        "halting": p_halt,
                    }
                    if isinstance(pol.get("action"), str) and pol["action"].strip():
                        pol_out["action"] = pol["action"].strip().lower()
                    if isinstance(pol.get("enabled"), bool):
                        pol_out["enabled"] = pol["enabled"]
                    if isinstance(pol.get("post_unarchive_action"), str):
                        pol_out["post_unarchive_action"] = pol["post_unarchive_action"]
                    if isinstance(pol.get("post_unarchive_target"), str):
                        pol_out["post_unarchive_target"] = pol["post_unarchive_target"]
                    if (
                        isinstance(pol.get("detect_original_by"), str)
                        and pol["detect_original_by"].strip()
                    ):
                        pol_out["detect_original_by"] = pol[
                            "detect_original_by"
                        ].strip()
                    if (
                        isinstance(pol.get("rename_pattern"), str)
                        and pol["rename_pattern"].strip()
                    ):
                        norm_ren, ren_err = _normalize_imported_rename_pattern(
                            pol["rename_pattern"]
                        )
                        if ren_err is not None:
                            result.warnings.append(
                                f"Sortify policy rule '{p_expr}' skipped invalid rename pattern: {ren_err}"
                            )
                        elif norm_ren:
                            pol_out["rename_pattern"] = norm_ren
                    result.policies.append(pol_out)

        if not result.keyword_rules and not result.policies and not result.warnings:
            result.warnings.append(f"Sortify rule package '{p.name}' is empty.")

        return result


def get_rule_importer(file_path: str | Path) -> BaseRuleImporter:
    """Return auto-detected rule importer strategy for the given file path.

    Parameters
    ----------
    file_path : str | Path
        Path to rule package file.

    Returns
    -------
    BaseRuleImporter
        An instance of BaseRuleImporter strategy class.

    Raises
    ------
    ValueError
        If file format is not supported.
    """
    p = Path(file_path)
    sortify_importer = SortifyRuleImporter()
    tagspaces_importer = TagSpacesRuleImporter()
    hazel_importer = HazelRuleImporter()
    dropit_importer = DropItRuleImporter()

    if sortify_importer.can_import(p):
        return sortify_importer

    if tagspaces_importer.can_import(p):
        return tagspaces_importer

    if hazel_importer.can_import(p):
        return hazel_importer

    if dropit_importer.can_import(p):
        return dropit_importer

    # Fallback extension detection
    ext = p.suffix.lower()
    if (
        p.name.lower() in ("taggroup.json", "taggroups.json")
        or "taggroup" in p.name.lower()
    ):
        return tagspaces_importer
    elif ext in (".hazelrules", ".plist"):
        return hazel_importer
    elif ext in (".xml", ".json", ".dropit", ".ini"):
        return dropit_importer

    raise ValueError(
        f"Unsupported rule file format for '{p.name}'. Expected .hazelrules, .plist, .xml, .json, .dropit, or .ini."
    )
