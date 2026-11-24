"""Policy Engine module for evaluating compliance policies and validating lock paths."""

import calendar
import fnmatch
import functools
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from app.core.path_utils import validate_target_path

MAX_POLICY_REGEX_LENGTH = 256
MAX_CONDITION_NESTING_LEVELS = 2
MAX_CONDITION_CLAUSES = 32

METADATA_POLICY_TYPES: frozenset[str] = frozenset(
    {"age", "modified_date", "created_date", "exif_date", "size"}
)

_SIZE_UNIT_MULTIPLIERS: dict[str, int] = {
    "": 1,
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}

_AGE_UNIT_SECONDS: dict[str, float] = {
    "": 86400.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 7 * 86400.0,
    "wk": 7 * 86400.0,
    "wks": 7 * 86400.0,
    "week": 7 * 86400.0,
    "weeks": 7 * 86400.0,
    "mo": 30 * 86400.0,
    "mon": 30 * 86400.0,
    "mons": 30 * 86400.0,
    "month": 30 * 86400.0,
    "months": 30 * 86400.0,
    "y": 365 * 86400.0,
    "yr": 365 * 86400.0,
    "yrs": 365 * 86400.0,
    "year": 365 * 86400.0,
    "years": 365 * 86400.0,
}


def _parse_size_quantity(token: str) -> int:
    """Parse a byte size literal such as '50MB', '1.5 GB', or '1024' into integer bytes."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$", token)
    if not m:
        raise ValueError(f"Invalid size literal: '{token}'")
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in _SIZE_UNIT_MULTIPLIERS:
        raise ValueError(f"Unsupported size unit '{m.group(2)}' in '{token}'")
    return int(round(num * _SIZE_UNIT_MULTIPLIERS[unit]))


def _parse_size_expression(expression: str) -> tuple[str, int, Optional[int]]:
    """Parse a size rule expression into (operator, primary_bytes, secondary_bytes)."""
    cleaned = str(expression or "").strip()
    if not cleaned:
        raise ValueError("Size expression cannot be empty.")

    if ".." in cleaned:
        parts = cleaned.split("..", 1)
        low = _parse_size_quantity(parts[0])
        high = _parse_size_quantity(parts[1])
        if low > high:
            low, high = high, low
        return "range", low, high

    between_m = re.match(r"(?i)^between\s+(.+?)\s+and\s+(.+)$", cleaned)
    if between_m:
        low = _parse_size_quantity(between_m.group(1))
        high = _parse_size_quantity(between_m.group(2))
        if low > high:
            low, high = high, low
        return "range", low, high

    natural_prefixes = (
        (r"(?i)^(?:greater\s+than|larger\s+than|more\s+than|over)\s+(.+)$", ">"),
        (r"(?i)^(?:at\s+least)\s+(.+)$", ">="),
        (r"(?i)^(?:less\s+than|smaller\s+than|under)\s+(.+)$", "<"),
        (r"(?i)^(?:at\s+most)\s+(.+)$", "<="),
    )
    for pat, op in natural_prefixes:
        nm = re.match(pat, cleaned)
        if nm:
            return op, _parse_size_quantity(nm.group(1)), None

    op_m = re.match(r"^(>=|<=|==|!=|>|<|=)\s*(.+)$", cleaned)
    if op_m:
        op = "==" if op_m.group(1) == "=" else op_m.group(1)
        return op, _parse_size_quantity(op_m.group(2)), None

    return "==", _parse_size_quantity(cleaned), None


def _parse_duration_quantity(token: str) -> tuple[float, float]:
    """Parse an age/duration literal such as '30d', '2 weeks', or '12h' into (seconds, unit_seconds)."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$", token)
    if not m:
        raise ValueError(f"Invalid age/duration literal: '{token}'")
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit not in _AGE_UNIT_SECONDS:
        raise ValueError(f"Unsupported age unit '{m.group(2)}' in '{token}'")
    unit_sec = _AGE_UNIT_SECONDS[unit]
    return num * unit_sec, unit_sec


def _parse_age_expression(expression: str) -> tuple[str, float, float]:
    """Parse an age rule expression into (operator, primary_seconds, secondary_or_unit_seconds)."""
    cleaned = str(expression or "").strip()
    if not cleaned:
        raise ValueError("Age expression cannot be empty.")

    if ".." in cleaned:
        parts = cleaned.split("..", 1)
        low, _ = _parse_duration_quantity(parts[0])
        high, _ = _parse_duration_quantity(parts[1])
        if low > high:
            low, high = high, low
        return "range", low, high

    natural_prefixes = (
        (r"(?i)^(?:older\s+than|more\s+than|over)\s+(.+)$", ">"),
        (r"(?i)^(?:at\s+least)\s+(.+)$", ">="),
        (
            r"(?i)^(?:newer\s+than|younger\s+than|less\s+than|under|within|in\s+the\s+last)\s+(.+)$",
            "<=",
        ),
    )
    for pat, op in natural_prefixes:
        nm = re.match(pat, cleaned)
        if nm:
            sec, unit_sec = _parse_duration_quantity(nm.group(1))
            return op, sec, unit_sec

    op_m = re.match(r"^(>=|<=|==|!=|>|<|=)\s*(.+)$", cleaned)
    if op_m:
        op = "==" if op_m.group(1) == "=" else op_m.group(1)
        sec, unit_sec = _parse_duration_quantity(op_m.group(2))
        return op, sec, unit_sec

    sec, unit_sec = _parse_duration_quantity(cleaned)
    return "==", sec, unit_sec


def _parse_date_bounds(token: str) -> tuple[datetime, datetime]:
    """Parse a calendar date or datetime token into inclusive (start_bound, end_bound)."""
    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Date literal cannot be empty.")

    # Year only: YYYY
    m_year = re.match(r"^(\d{4})$", cleaned)
    if m_year:
        y = int(m_year.group(1))
        if not (1900 <= y <= 2200):
            raise ValueError(f"Year out of supported range: {y}")
        return datetime(y, 1, 1, 0, 0, 0), datetime(y, 12, 31, 23, 59, 59, 999999)

    # Year-month: YYYY-MM or YYYY/MM
    m_ym = re.match(r"^(\d{4})[\-/](\d{1,2})$", cleaned)
    if m_ym:
        y = int(m_ym.group(1))
        mo = int(m_ym.group(2))
        if not (1900 <= y <= 2200 and 1 <= mo <= 12):
            raise ValueError(f"Invalid year-month literal: '{cleaned}'")
        last_day = calendar.monthrange(y, mo)[1]
        return (
            datetime(y, mo, 1, 0, 0, 0),
            datetime(y, mo, last_day, 23, 59, 59, 999999),
        )

    # Date without time: YYYY-MM-DD or YYYY/MM/DD
    m_ymd = re.match(r"^(\d{4})[\-/](\d{1,2})[\-/](\d{1,2})$", cleaned)
    if m_ymd:
        y = int(m_ymd.group(1))
        mo = int(m_ymd.group(2))
        d = int(m_ymd.group(3))
        start = datetime(y, mo, d, 0, 0, 0)
        end = datetime(y, mo, d, 23, 59, 59, 999999)
        return start, end

    # Full ISO datetime
    iso_dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if iso_dt.tzinfo is not None:
        iso_dt = iso_dt.astimezone().replace(tzinfo=None)
    return iso_dt, iso_dt


def _parse_date_expression(
    expression: str,
) -> tuple[str, Any, Any]:
    """Parse a date rule expression into an evaluable specification."""
    cleaned = str(expression or "").strip()
    if not cleaned:
        raise ValueError("Date expression cannot be empty.")

    if ".." in cleaned:
        parts = cleaned.split("..", 1)
        start_low, _ = _parse_date_bounds(parts[0])
        _, end_high = _parse_date_bounds(parts[1])
        if start_low > end_high:
            start_low, end_high = (
                _parse_date_bounds(parts[1])[0],
                _parse_date_bounds(parts[0])[1],
            )
        return "range", start_low, end_high

    between_m = re.match(r"(?i)^between\s+(.+?)\s+and\s+(.+)$", cleaned)
    if between_m:
        start_low, _ = _parse_date_bounds(between_m.group(1))
        _, end_high = _parse_date_bounds(between_m.group(2))
        return "range", start_low, end_high

    rel_recent = re.match(r"(?i)^(?:in\s+the\s+last|within)\s+(.+)$", cleaned)
    if rel_recent:
        sec, _ = _parse_duration_quantity(rel_recent.group(1))
        return "rel_recent", sec, None

    rel_older = re.match(r"(?i)^(?:older\s+than|more\s+than)\s+(.+)$", cleaned)
    if rel_older:
        sec, _ = _parse_duration_quantity(rel_older.group(1))
        return "rel_older", sec, None

    natural_date_ops = (
        (r"(?i)^(?:on\s+or\s+after|since|from)\s+(.+)$", ">="),
        (r"(?i)^(?:after)\s+(.+)$", ">"),
        (r"(?i)^(?:on\s+or\s+before|until)\s+(.+)$", "<="),
        (r"(?i)^(?:before)\s+(.+)$", "<"),
        (r"(?i)^(?:on)\s+(.+)$", "=="),
    )
    for pat, op in natural_date_ops:
        nm = re.match(pat, cleaned)
        if nm:
            start_b, end_b = _parse_date_bounds(nm.group(1))
            return op, start_b, end_b

    op_m = re.match(r"^(>=|<=|==|!=|>|<|=)\s*(.+)$", cleaned)
    if op_m:
        op = "==" if op_m.group(1) == "=" else op_m.group(1)
        rhs = op_m.group(2).strip()
        try:
            start_b, end_b = _parse_date_bounds(rhs)
            return op, start_b, end_b
        except ValueError:
            # Support relative shorthand such as '< 30d' (within 30d) or '> 30d' (older than 30d)
            sec, _ = _parse_duration_quantity(rhs)
            if op in ("<", "<="):
                return "rel_recent", sec, None
            if op in (">", ">="):
                return "rel_older", sec, None
            raise

    start_b, end_b = _parse_date_bounds(cleaned)
    return "==", start_b, end_b


def validate_metadata_expression(rule_type: str, expression: str) -> None:
    """Validate a metadata policy rule expression, raising ValueError if malformed."""
    rtype = str(rule_type or "").strip().lower()
    if rtype == "size":
        _parse_size_expression(expression)
        return
    if rtype == "age":
        _parse_age_expression(expression)
        return
    if rtype in ("modified_date", "created_date", "exif_date"):
        _parse_date_expression(expression)
        return
    raise ValueError(f"Unsupported metadata policy rule type: '{rule_type}'")


def evaluate_size_expression(expression: str, size_bytes: int) -> bool:
    """Evaluate a parsed size expression against a file's byte count."""
    op, primary, secondary = _parse_size_expression(expression)
    if op == "range" and secondary is not None:
        return primary <= size_bytes <= secondary
    if op == ">":
        return size_bytes > primary
    if op == ">=":
        return size_bytes >= primary
    if op == "<":
        return size_bytes < primary
    if op == "<=":
        return size_bytes <= primary
    if op == "!=":
        return size_bytes != primary
    return size_bytes == primary


def _to_local_naive(dt: datetime) -> datetime:
    """Normalize timezone-aware datetimes to local naive timestamps for comparison."""
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def evaluate_age_expression(
    expression: str, mtime: datetime, now: Optional[datetime] = None
) -> bool:
    """Evaluate a parsed age expression against a file's modification timestamp."""
    ref_now = _to_local_naive(now) if now is not None else datetime.now()
    norm_mtime = _to_local_naive(mtime)
    age_sec = max(0.0, (ref_now - norm_mtime).total_seconds())
    op, primary, secondary = _parse_age_expression(expression)
    if op == "range":
        return primary <= age_sec <= secondary
    if op == ">":
        return age_sec > primary
    if op == ">=":
        return age_sec >= primary
    if op == "<":
        return age_sec < primary
    if op == "<=":
        return age_sec <= primary
    if op == "!=":
        if secondary >= 86400.0:
            return int(age_sec // secondary) != int(primary // secondary)
        return abs(age_sec - primary) >= 1.0
    if secondary >= 86400.0:
        return int(age_sec // secondary) == int(primary // secondary)
    return abs(age_sec - primary) < 1.0


def evaluate_date_expression(
    expression: str, target_dt: datetime, now: Optional[datetime] = None
) -> bool:
    """Evaluate a parsed date expression against a resolved datetime."""
    ref_now = _to_local_naive(now) if now is not None else datetime.now()
    norm_target = _to_local_naive(target_dt)
    op, first, second = _parse_date_expression(expression)
    if op == "range":
        return first <= norm_target <= second
    if op == "rel_recent":
        return norm_target >= (ref_now - timedelta(seconds=float(first)))
    if op == "rel_older":
        return norm_target < (ref_now - timedelta(seconds=float(first)))
    if op == ">":
        return norm_target > second
    if op == ">=":
        return norm_target >= first
    if op == "<":
        return norm_target < first
    if op == "<=":
        return norm_target <= second
    if op == "!=":
        return not (first <= norm_target <= second)
    return first <= norm_target <= second


VALID_MATCH_MODES = ("all", "any", "none")
VALID_CONDITION_FIELDS = ("name", "extension", "content", "path", "any_text")
VALID_CONDITION_OPERATORS = (
    "contains",
    "equals",
    "starts_with",
    "ends_with",
    "glob",
    "regex",
)


def _parse_brace_quantifier(pattern: str, pos: int) -> tuple[int, bool] | None:
    """Parse a ``{m}``, ``{m,}``, or ``{m,n}`` quantifier starting at ``pos``.

    Returns ``(end_index_inclusive, is_repeating_or_variable)`` when ``pattern[pos:]``
    begins with a valid regex brace quantifier, or ``None`` when ``{`` is literal.
    """
    if pos >= len(pattern) or pattern[pos] != "{":
        return None
    close = pattern.find("}", pos + 1)
    if close == -1:
        return None
    inner = pattern[pos + 1 : close]
    if not inner:
        return None
    if "," not in inner:
        if not inner.isdigit():
            return None
        count = int(inner)
        return close, count != 1
    parts = inner.split(",")
    if len(parts) != 2:
        return None
    lo_s, hi_s = parts
    if (lo_s and not lo_s.isdigit()) or (hi_s and not hi_s.isdigit()):
        return None
    if not lo_s and not hi_s:
        return None
    lo = int(lo_s) if lo_s else 0
    hi = int(hi_s) if hi_s else None
    if hi is not None and hi < lo:
        return None
    is_quant = hi is None or hi > 1 or lo != hi
    return close, is_quant


def _consume_quantifier(pattern: str, pos: int) -> tuple[int, bool] | None:
    """Consume a regex quantifier token (including lazy/possessive suffix) at ``pos``."""
    n = len(pattern)
    if pos >= n:
        return None
    ch = pattern[pos]
    if ch in ("*", "+", "?"):
        end = pos
        if end + 1 < n and pattern[end + 1] in ("?", "+"):
            end += 1
        return end, True
    brace = _parse_brace_quantifier(pattern, pos)
    if brace is not None:
        end, is_quant = brace
        if end + 1 < n and pattern[end + 1] in ("?", "+"):
            end += 1
        return end, is_quant
    return None


def _has_overlapping_alternation(branches: list[str] | None) -> bool:
    """Return True if a quantified alternation has empty or prefix-overlapping literal branches."""
    if not branches or len(branches) < 2:
        return False
    for idx, b1 in enumerate(branches):
        if b1 == "":
            return True
        for b2 in branches[idx + 1 :]:
            if b2 == "" or b1.startswith(b2) or b2.startswith(b1):
                return True
    return False


def _detect_nested_quantifiers(pattern: str) -> bool:
    """Return True if ``pattern`` contains nested repetition quantifiers or ambiguous quantified alternation (ReDoS risk)."""
    n = len(pattern)
    i = 0
    in_char_class = False
    class_start = -1
    # Each frame: [has_quantifier, branches_if_pure_literal_or_None, current_branch_chars_or_None]
    stack: list[list[Any]] = []

    while i < n:
        ch = pattern[i]
        if ch == "\\":
            esc_ch = pattern[i + 1] if i + 1 < n else ""
            if stack and stack[-1][2] is not None:
                if esc_ch and not esc_ch.isalnum():
                    stack[-1][2].append(esc_ch)
                else:
                    stack[-1][1] = None
                    stack[-1][2] = None
            i += 2
            continue

        if in_char_class:
            if ch == "]" and i > class_start:
                in_char_class = False
            i += 1
            continue

        if ch == "[":
            in_char_class = True
            class_start = i + 2 if (i + 1 < n and pattern[i + 1] == "^") else i + 1
            if stack:
                stack[-1][1] = None
                stack[-1][2] = None
            i += 1
            continue

        if ch == "(":
            if stack:
                stack[-1][1] = None
                stack[-1][2] = None
            # Advance past group header syntax like (?:, (?=, (?!, (?<=, (?<!, (?P<name>, (?#...)
            if i + 1 < n and pattern[i + 1] == "?":
                if i + 2 < n and pattern[i + 2] == "#":
                    close_comment = pattern.find(")", i + 3)
                    i = n if close_comment == -1 else close_comment + 1
                    continue
                j = i + 2
                if j < n and pattern[j] == "P" and j + 1 < n and pattern[j + 1] == "<":
                    end_name = pattern.find(">", j + 2)
                    j = (end_name + 1) if end_name != -1 else (j + 2)
                elif (
                    j < n
                    and pattern[j] == "<"
                    and j + 1 < n
                    and pattern[j + 1] in ("=", "!")
                ):
                    j += 2
                else:
                    while j < n and pattern[j] in "aiLmsux-:":
                        j += 1
                        if pattern[j - 1] == ":":
                            break
                    if j == i + 2 and j < n and pattern[j] in ("=", "!"):
                        j += 1
                stack.append([False, [], []])
                i = j
                continue
            stack.append([False, [], []])
            i += 1
            continue

        if ch == "|":
            if stack and stack[-1][1] is not None and stack[-1][2] is not None:
                stack[-1][1].append("".join(stack[-1][2]))
                stack[-1][2] = []
            i += 1
            continue

        if ch == ")":
            frame = stack.pop() if stack else [False, None, None]
            inner_has_quant = bool(frame[0])
            branches: list[str] | None = frame[1]
            if branches is not None and frame[2] is not None:
                branches.append("".join(frame[2]))
            quant = _consume_quantifier(pattern, i + 1)
            if quant is not None:
                end_q, is_quant = quant
                if is_quant and (
                    inner_has_quant or _has_overlapping_alternation(branches)
                ):
                    return True
                if stack and (inner_has_quant or is_quant):
                    stack[-1][0] = True
                i = end_q + 1
                continue
            if stack and inner_has_quant:
                stack[-1][0] = True
            i += 1
            continue

        quant = _consume_quantifier(pattern, i)
        if quant is not None:
            end_q, is_quant = quant
            if is_quant and stack:
                stack[-1][0] = True
                stack[-1][1] = None
                stack[-1][2] = None
            i = end_q + 1
            continue

        if stack and stack[-1][2] is not None:
            if ch in (".", "^", "$"):
                stack[-1][1] = None
                stack[-1][2] = None
            else:
                stack[-1][2].append(ch)

        i += 1

    return False


@functools.lru_cache(maxsize=512)
def _compile_safe_regex(pattern: str, case_sensitive: bool = False) -> re.Pattern[str]:
    """Validate and compile a bounded, ReDoS-safe regular expression."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("Policy regular expression must be a non-empty string.")
    if len(pattern) > MAX_POLICY_REGEX_LENGTH:
        raise ValueError(
            f"Policy regular expression exceeds maximum length of "
            f"{MAX_POLICY_REGEX_LENGTH} characters ({len(pattern)} > {MAX_POLICY_REGEX_LENGTH})."
        )
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(
            f"Invalid policy regular expression '{pattern}': {exc}"
        ) from exc

    if _detect_nested_quantifiers(pattern):
        raise ValueError(
            f"Unsafe policy regular expression '{pattern}': nested quantifiers are forbidden to prevent ReDoS."
        )
    return compiled


def validate_safe_regex(
    pattern: str, *, case_sensitive: bool = False
) -> re.Pattern[str]:
    """Validate a policy regular expression against length, syntax, and ReDoS bounds.

    Parameters
    ----------
    pattern : str
        Regular expression string to validate and compile.
    case_sensitive : bool, optional
        Whether to compile without ``re.IGNORECASE``.

    Returns
    -------
    re.Pattern[str]
        Compiled regular expression object.
    """
    return _compile_safe_regex(pattern, bool(case_sensitive))


def validate_policy_conditions(
    conditions: Any,
    *,
    max_depth: int = MAX_CONDITION_NESTING_LEVELS,
    max_clauses: int = MAX_CONDITION_CLAUSES,
) -> list[dict[str, Any]]:
    """Validate and normalize a ``PolicyRule`` condition tree.

    Parameters
    ----------
    conditions : Any
        List of leaf ``PolicyCondition`` clauses and/or nested condition groups.
    max_depth : int, optional
        Maximum allowed tree depth (defaults to 2 levels).
    max_clauses : int, optional
        Maximum allowed leaf condition clauses per policy rule (defaults to 32).

    Returns
    -------
    list[dict[str, Any]]
        Normalized condition tree.
    """
    if not isinstance(conditions, list) or not conditions:
        raise ValueError("Policy conditions must be a non-empty list.")

    total_clauses = 0

    def _walk(nodes: list[Any], depth: int) -> list[dict[str, Any]]:
        nonlocal total_clauses
        if depth > max_depth:
            raise ValueError(
                f"Policy conditions exceed maximum nesting depth of {max_depth} levels."
            )
        normalized: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("Each policy condition or group must be a dictionary.")

            if "conditions" in node or (
                "match_mode" in node and "field" not in node and "value" not in node
            ):
                sub_conds = node.get("conditions")
                if not isinstance(sub_conds, list) or not sub_conds:
                    raise ValueError(
                        "Nested policy condition group must contain a non-empty 'conditions' list."
                    )
                if depth + 1 > max_depth:
                    raise ValueError(
                        f"Policy conditions exceed maximum nesting depth of {max_depth} levels."
                    )
                raw_mode = node.get("match_mode", "all")
                if (
                    not isinstance(raw_mode, str)
                    or raw_mode.strip().lower() not in VALID_MATCH_MODES
                ):
                    raise ValueError(
                        f"Invalid nested condition group match_mode: {raw_mode}. "
                        f"Must be one of {list(VALID_MATCH_MODES)}."
                    )
                grp_out: dict[str, Any] = {
                    "match_mode": raw_mode.strip().lower(),
                    "conditions": _walk(sub_conds, depth + 1),
                }
                if "negate" in node and node["negate"] is not None:
                    if not isinstance(node["negate"], bool):
                        raise ValueError(
                            "Nested condition group 'negate' property must be a boolean."
                        )
                    grp_out["negate"] = node["negate"]
                normalized.append(grp_out)
                continue

            total_clauses += 1
            if total_clauses > max_clauses:
                raise ValueError(
                    f"Policy conditions exceed maximum of {max_clauses} clauses per rule."
                )

            raw_field = node.get("field")
            if (
                not isinstance(raw_field, str)
                or raw_field.strip().lower() not in VALID_CONDITION_FIELDS
            ):
                raise ValueError(
                    f"Invalid policy condition field: {raw_field}. "
                    f"Must be one of {list(VALID_CONDITION_FIELDS)}."
                )
            field_norm = raw_field.strip().lower()

            raw_op = node.get("operator", "contains")
            if (
                not isinstance(raw_op, str)
                or raw_op.strip().lower() not in VALID_CONDITION_OPERATORS
            ):
                raise ValueError(
                    f"Invalid policy condition operator: {raw_op}. "
                    f"Must be one of {list(VALID_CONDITION_OPERATORS)}."
                )
            op_norm = raw_op.strip().lower()

            raw_val = node.get("value")
            if not isinstance(raw_val, str) or not raw_val.strip():
                raise ValueError("Policy condition 'value' must be a non-empty string.")

            clause_case = False
            if "case_sensitive" in node and node["case_sensitive"] is not None:
                if not isinstance(node["case_sensitive"], bool):
                    raise ValueError(
                        "Policy condition 'case_sensitive' property must be a boolean."
                    )
                clause_case = node["case_sensitive"]

            if "negate" in node and node["negate"] is not None:
                if not isinstance(node["negate"], bool):
                    raise ValueError(
                        "Policy condition 'negate' property must be a boolean."
                    )

            if op_norm == "regex":
                validate_safe_regex(raw_val, case_sensitive=clause_case)

            clause_out: dict[str, Any] = {
                "field": field_norm,
                "operator": op_norm,
                "value": raw_val,
            }
            if "negate" in node and node["negate"] is not None:
                clause_out["negate"] = node["negate"]
            if "case_sensitive" in node and node["case_sensitive"] is not None:
                clause_out["case_sensitive"] = node["case_sensitive"]
            normalized.append(clause_out)

        return normalized

    return _walk(conditions, 1)


def summarize_policy_conditions(
    conditions: list[dict[str, Any]],
    match_mode: str = "all",
    negate: bool = False,
) -> str:
    """Build a concise human-readable summary string for a condition tree.

    Parameters
    ----------
    conditions : list[dict[str, Any]]
        Condition clauses or nested condition groups.
    match_mode : str, optional
        Combinator mode (``'all'``, ``'any'``, or ``'none'``).
    negate : bool, optional
        Whether the outer group result is negated.

    Returns
    -------
    str
        Readable boolean summary suitable for ``PolicyRule['expression']``.
    """
    if not conditions:
        return "*"

    op_labels = {
        "equals": "==",
        "contains": "contains",
        "starts_with": "starts_with",
        "ends_with": "ends_with",
        "glob": "glob",
        "regex": "regex",
    }

    parts: list[str] = []
    for item in conditions:
        if not isinstance(item, dict):
            continue
        if "conditions" in item and isinstance(item.get("conditions"), list):
            parts.append(
                summarize_policy_conditions(
                    item["conditions"],
                    match_mode=str(item.get("match_mode", "all")).lower(),
                    negate=bool(item.get("negate", False)),
                )
            )
            continue
        fld = str(item.get("field", "name")).lower()
        op = str(item.get("operator", "contains")).lower()
        val = str(item.get("value", ""))
        op_disp = op_labels.get(op, op)
        cs_tag = " [Aa]" if item.get("case_sensitive") else ""
        prefix = "NOT " if item.get("negate") else ""
        parts.append(f"{prefix}{fld} {op_disp} '{val}'{cs_tag}")

    mode_upper = str(match_mode or "all").strip().upper()
    if mode_upper not in ("ALL", "ANY", "NONE"):
        mode_upper = "ALL"
    joined = ", ".join(parts) if parts else "*"
    summary = f"{mode_upper}({joined})"
    return f"NOT({summary})" if negate else summary


def match_pattern_expression(
    expression: str,
    filename: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
) -> bool:
    """Evaluate a pattern expression against a filename using wildcards, extension masks, regex, or substring matching.

    Parameters
    ----------
    expression : str
        Pattern rule expression (glob mask such as ``*.pdf``, extension mask such as ``.pdf``,
        bounded regular expression when ``regex=True``, or plain substring text).
    filename : str
        File path or basename to test against the pattern expression.
    regex : bool, optional
        When True, evaluate ``expression`` as a bounded ReDoS-safe regular expression.
    case_sensitive : bool, optional
        When True, preserve character casing during matching.

    Returns
    -------
    bool
        True if the filename matches the pattern expression, False otherwise.
    """
    if not expression or not filename:
        return False
    raw_expr = str(expression)
    expr_clean = raw_expr.strip()
    if not expr_clean:
        return False
    clean_fp = (
        str(filename).split("::", 1)[1] if "::" in str(filename) else str(filename)
    )
    fn_base = os.path.basename(clean_fp)
    if not fn_base:
        return False

    if regex:
        compiled = validate_safe_regex(raw_expr, case_sensitive=case_sensitive)
        return bool(compiled.search(fn_base))

    target_fn = fn_base if case_sensitive else fn_base.lower()
    expr_cmp = expr_clean if case_sensitive else expr_clean.lower()

    has_wildcard = (
        "*" in expr_cmp or "?" in expr_cmp or ("[" in expr_cmp and "]" in expr_cmp)
    )
    if has_wildcard:
        return fnmatch.fnmatchcase(target_fn, expr_cmp)

    if expr_cmp.startswith(".") and "/" not in expr_cmp and "\\" not in expr_cmp:
        return fnmatch.fnmatchcase(target_fn, f"*{expr_cmp}")

    return expr_cmp in target_fn


class PolicyEvaluationResult(BaseModel):
    """Structured Pydantic model for policy evaluation result."""

    type: str = ""
    expression: str = ""
    target_path: str = ""
    priority: int = 0
    halting: bool = False
    halt_evaluation: bool = False
    rename_pattern: str = ""
    resolved_date: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Backward compatibility method for legacy Pydantic v1 callers."""
        return self.model_dump(*args, **kwargs)

    def __getitem__(self, item: str) -> Any:
        """Support item lookup via bracket syntax for dictionary compatibility."""
        if hasattr(self, item):
            return getattr(self, item)
        extra = getattr(self, "__pydantic_extra__", None)
        if extra and item in extra:
            return extra[item]
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Support dictionary get method."""
        try:
            return self[item]
        except KeyError:
            return default

    def __contains__(self, item: str) -> bool:
        """Check if key exists in attributes or extra fields."""
        extra = getattr(self, "__pydantic_extra__", None) or {}
        return hasattr(self, item) or item in extra


def _rule_attr(rule: Any, key: str, default: Any = None) -> Any:
    """Safely read an attribute or dict key from a policy rule object."""
    if isinstance(rule, dict):
        return rule.get(key, default)
    if hasattr(rule, "get"):
        return rule.get(key, default)
    return getattr(rule, key, default)


class _LazyFileAttributes:
    """Per-evaluation lazy cache for os.stat and embedded header metadata."""

    def __init__(self, disk_path: Optional[str]) -> None:
        self.disk_path = disk_path
        self._stat_loaded = False
        self._stat: Optional[os.stat_result] = None
        self._embedded_loaded = False
        self._embedded_dt: Optional[datetime] = None

    def get_stat(self) -> Optional[os.stat_result]:
        if not self._stat_loaded:
            self._stat_loaded = True
            if self.disk_path and os.path.isfile(self.disk_path):
                try:
                    self._stat = os.stat(self.disk_path)
                except OSError:
                    self._stat = None
        return self._stat

    def get_mtime(self) -> Optional[datetime]:
        st = self.get_stat()
        if st is None:
            return None
        try:
            return datetime.fromtimestamp(st.st_mtime)
        except (OSError, OverflowError, ValueError):
            return None

    def get_ctime(self) -> Optional[datetime]:
        st = self.get_stat()
        if st is None:
            return None
        if sys.platform == "win32":
            raw_ts = st.st_birthtime
        else:
            raw_ts = getattr(st, "st_birthtime", None)
            if raw_ts is None:
                raw_ts = st.st_ctime
        try:
            return datetime.fromtimestamp(raw_ts)
        except (OSError, OverflowError, ValueError):
            return None

    def get_embedded_date(self) -> Optional[datetime]:
        if not self._embedded_loaded:
            self._embedded_loaded = True
            if self.disk_path:
                from app.core.extractor_strategies import extract_embedded_date

                self._embedded_dt = extract_embedded_date(self.disk_path)
        return self._embedded_dt

    def resolve_date_str(self, rule_type: str) -> Optional[str]:
        rtype = str(rule_type or "").lower()
        dt: Optional[datetime] = None
        if rtype == "exif_date":
            dt = self.get_embedded_date()
        elif rtype == "created_date":
            dt = self.get_ctime()
        if dt is None:
            dt = self.get_mtime()
        return dt.strftime("%Y-%m-%d") if dt is not None else None


class PolicyEngine:
    """Standalone evaluation engine for enforcing compliance overrides on folder locks and lifecycle policy actions."""

    SUPPORTED_ACTIONS = {
        "archive",
        "retain",
        "quarantine",
        "redact",
        "unarchive",
        "trash",
    }
    METADATA_RULE_TYPES = METADATA_POLICY_TYPES

    @staticmethod
    def validate_policy_action(action: str) -> bool:
        """Validate whether an action directive is supported by the extended policy engine."""
        if not action or not isinstance(action, str):
            return False
        return action.lower() in PolicyEngine.SUPPORTED_ACTIONS

    @staticmethod
    def validate_metadata_expression(rule_type: str, expression: str) -> None:
        """Validate expression syntax for age, date, EXIF date, and size rules."""
        validate_metadata_expression(rule_type, expression)

    @staticmethod
    def contains_secrets(text: str) -> bool:
        """Check whether text contains secret tokens or credentials."""
        from app.core.text_utils import contains_secrets

        return contains_secrets(text)

    @staticmethod
    def is_static_status(status_match: str | None) -> bool:
        """Return True when ``status_match`` marks a pre-extraction static pass."""
        if not status_match:
            return False
        return str(status_match).strip().upper() in ("[STATUS:STATIC]", "STATIC")

    @classmethod
    def _conditions_inspect_content(
        cls,
        conditions: Any,
        *,
        under_negation: bool = False,
        require_static_defer: bool = False,
    ) -> bool:
        """Check whether a condition tree inspects document text content."""
        if not isinstance(conditions, list):
            return False
        for node in conditions:
            if not isinstance(node, dict):
                continue
            sub = node.get("conditions")
            if isinstance(sub, list):
                grp_mode = str(node.get("match_mode", "all")).strip().lower()
                grp_neg = (
                    under_negation
                    or bool(node.get("negate", False))
                    or grp_mode == "none"
                )
                if cls._conditions_inspect_content(
                    sub,
                    under_negation=grp_neg,
                    require_static_defer=require_static_defer,
                ):
                    return True
                continue
            fld = str(node.get("field", "")).strip().lower()
            clause_neg = under_negation or bool(node.get("negate", False))
            if fld in ("content", "any_text"):
                if not require_static_defer or clause_neg:
                    return True
        return False

    @classmethod
    def rule_inspects_content(cls, rule: Any) -> bool:
        """Return True if an active policy rule inspects extracted text or enforces content compliance."""
        if not (
            isinstance(rule, dict) or hasattr(rule, "get") or hasattr(rule, "type")
        ):
            return False
        enabled = _rule_attr(rule, "enabled", True)
        if enabled is False:
            return False

        rule_type = str(_rule_attr(rule, "type", "") or "").strip().lower()
        action = str(_rule_attr(rule, "action", "") or "").strip().lower()
        expr_lower = str(_rule_attr(rule, "expression", "") or "").strip().lower()

        if rule_type in METADATA_POLICY_TYPES:
            conditions = _rule_attr(rule, "conditions", None)
            if not (isinstance(conditions, list) and conditions):
                return False

        if (
            rule_type in ("compliance", "secret", "credential")
            or action in ("quarantine", "redact")
            and rule_type not in METADATA_POLICY_TYPES
            or expr_lower in ("secret", "credential")
        ):
            return True

        conditions = _rule_attr(rule, "conditions", None)
        match_mode = str(_rule_attr(rule, "match_mode", "all") or "all").strip().lower()
        negate = bool(_rule_attr(rule, "negate", False))
        top_neg = negate or match_mode == "none"

        if isinstance(conditions, list) and conditions:
            return cls._conditions_inspect_content(
                conditions, under_negation=top_neg, require_static_defer=False
            )

        if top_neg and rule_type in (
            "keyword",
            "action",
            "compliance",
            "unarchive",
            "",
        ):
            return True

        return False

    @staticmethod
    def _match_text_operator(
        operator: str,
        value: str,
        candidate: str,
        *,
        case_sensitive: bool = False,
        is_filename: bool = False,
        is_extension: bool = False,
    ) -> bool:
        """Evaluate a single string comparison operator against ``candidate``."""
        if operator == "regex":
            compiled = validate_safe_regex(value, case_sensitive=case_sensitive)
            if (
                is_extension
                and value.startswith(r"\.")
                and not candidate.startswith(".")
            ):
                return bool(
                    compiled.search(f".{candidate}") or compiled.search(candidate)
                )
            return bool(compiled.search(candidate))

        if is_extension:
            cand_ext = candidate.lstrip(".")
            val_ext = value.strip()
            if operator in ("equals", "contains", "starts_with", "ends_with"):
                val_ext = val_ext.lstrip(".")
                lhs = cand_ext if case_sensitive else cand_ext.lower()
                rhs = val_ext if case_sensitive else val_ext.lower()
                if operator == "equals":
                    return lhs == rhs
                if operator == "contains":
                    return bool(rhs) and rhs in lhs
                if operator == "starts_with":
                    return bool(rhs) and lhs.startswith(rhs)
                return bool(rhs) and lhs.endswith(rhs)
            if operator == "glob":
                pat = (
                    val_ext.lstrip(".") if not val_ext.startswith("*.") else val_ext[2:]
                )
                lhs = cand_ext if case_sensitive else cand_ext.lower()
                rhs = pat if case_sensitive else pat.lower()
                return bool(lhs) and fnmatch.fnmatchcase(lhs, rhs)

        if operator == "glob":
            if is_filename:
                return match_pattern_expression(
                    value, candidate, regex=False, case_sensitive=case_sensitive
                )
            lhs = candidate if case_sensitive else candidate.lower()
            rhs = value.strip() if case_sensitive else value.strip().lower()
            return bool(rhs) and fnmatch.fnmatchcase(lhs, rhs)

        lhs = candidate if case_sensitive else candidate.lower()
        rhs = value if case_sensitive else value.lower()
        if not rhs:
            return False
        if operator == "equals":
            return lhs == rhs
        if operator == "starts_with":
            return lhs.startswith(rhs)
        if operator == "ends_with":
            return lhs.endswith(rhs)
        return rhs in lhs

    @classmethod
    def _evaluate_condition_node(
        cls,
        node: dict[str, Any],
        *,
        basename_raw: str,
        ext_bare: str,
        path_norm: str,
        full_path_norm: str,
        doc_text_str: str,
        has_extracted_text: bool,
        default_case_sensitive: bool,
    ) -> bool:
        """Evaluate a leaf ``PolicyCondition`` or nested ``PolicyConditionGroup``."""
        sub = node.get("conditions")
        if isinstance(sub, list):
            grp_mode = str(node.get("match_mode", "all") or "all").strip().lower()
            grp_negate = bool(node.get("negate", False))
            res = cls._evaluate_condition_list(
                sub,
                match_mode=grp_mode,
                basename_raw=basename_raw,
                ext_bare=ext_bare,
                path_norm=path_norm,
                full_path_norm=full_path_norm,
                doc_text_str=doc_text_str,
                has_extracted_text=has_extracted_text,
                default_case_sensitive=default_case_sensitive,
            )
            return (not res) if grp_negate else res

        fld = str(node.get("field", "name")).strip().lower()
        op = str(node.get("operator", "contains")).strip().lower()
        val = str(node.get("value", ""))
        clause_negate = bool(node.get("negate", False))
        eff_case = (
            bool(node["case_sensitive"])
            if "case_sensitive" in node and node["case_sensitive"] is not None
            else default_case_sensitive
        )

        if fld == "name":
            matched = cls._match_text_operator(
                op,
                val,
                basename_raw,
                case_sensitive=eff_case,
                is_filename=True,
            )
        elif fld == "extension":
            matched = bool(ext_bare) and cls._match_text_operator(
                op,
                val,
                ext_bare,
                case_sensitive=eff_case,
                is_extension=True,
            )
        elif fld == "content":
            if not has_extracted_text:
                matched = False
            else:
                matched = cls._match_text_operator(
                    op,
                    val,
                    doc_text_str,
                    case_sensitive=eff_case,
                )
        elif fld == "path":
            matched = cls._match_text_operator(
                op,
                val,
                path_norm,
                case_sensitive=eff_case,
            ) or (
                full_path_norm != path_norm
                and cls._match_text_operator(
                    op,
                    val,
                    full_path_norm,
                    case_sensitive=eff_case,
                )
            )
        elif fld == "any_text":
            matched = (
                cls._match_text_operator(
                    op,
                    val,
                    basename_raw,
                    case_sensitive=eff_case,
                    is_filename=True,
                )
                or cls._match_text_operator(
                    op,
                    val,
                    path_norm,
                    case_sensitive=eff_case,
                )
                or (
                    has_extracted_text
                    and cls._match_text_operator(
                        op,
                        val,
                        doc_text_str,
                        case_sensitive=eff_case,
                    )
                )
            )
        else:
            matched = False

        return (not matched) if clause_negate else matched

    @classmethod
    def _evaluate_condition_list(
        cls,
        conditions: list[dict[str, Any]],
        *,
        match_mode: str,
        basename_raw: str,
        ext_bare: str,
        path_norm: str,
        full_path_norm: str,
        doc_text_str: str,
        has_extracted_text: bool,
        default_case_sensitive: bool,
    ) -> bool:
        """Combine child condition results using ``all``, ``any``, or ``none``."""
        mode = match_mode if match_mode in VALID_MATCH_MODES else "all"
        if mode == "all":
            return all(
                cls._evaluate_condition_node(
                    item,
                    basename_raw=basename_raw,
                    ext_bare=ext_bare,
                    path_norm=path_norm,
                    full_path_norm=full_path_norm,
                    doc_text_str=doc_text_str,
                    has_extracted_text=has_extracted_text,
                    default_case_sensitive=default_case_sensitive,
                )
                for item in conditions
            )
        if mode == "any":
            return any(
                cls._evaluate_condition_node(
                    item,
                    basename_raw=basename_raw,
                    ext_bare=ext_bare,
                    path_norm=path_norm,
                    full_path_norm=full_path_norm,
                    doc_text_str=doc_text_str,
                    has_extracted_text=has_extracted_text,
                    default_case_sensitive=default_case_sensitive,
                )
                for item in conditions
            )
        # mode == "none"
        return not any(
            cls._evaluate_condition_node(
                item,
                basename_raw=basename_raw,
                ext_bare=ext_bare,
                path_norm=path_norm,
                full_path_norm=full_path_norm,
                doc_text_str=doc_text_str,
                has_extracted_text=has_extracted_text,
                default_case_sensitive=default_case_sensitive,
            )
            for item in conditions
        )

    @staticmethod
    def _match_metadata_rule(
        rule_type: str,
        raw_expression: str,
        attrs: _LazyFileAttributes,
        now: Optional[datetime] = None,
    ) -> bool:
        """Evaluate a metadata-based policy rule lazily using cached file attributes."""
        if not raw_expression or not raw_expression.strip():
            return False
        try:
            if rule_type == "size":
                st = attrs.get_stat()
                if st is None:
                    return False
                return evaluate_size_expression(raw_expression, int(st.st_size))
            if rule_type == "age":
                mtime = attrs.get_mtime()
                if mtime is None:
                    return False
                return evaluate_age_expression(raw_expression, mtime, now=now)
            if rule_type == "modified_date":
                mtime = attrs.get_mtime()
                if mtime is None:
                    return False
                return evaluate_date_expression(raw_expression, mtime, now=now)
            if rule_type == "created_date":
                ctime = attrs.get_ctime()
                if ctime is None:
                    return False
                return evaluate_date_expression(raw_expression, ctime, now=now)
            if rule_type == "exif_date":
                emb = attrs.get_embedded_date()
                if emb is None:
                    return False
                return evaluate_date_expression(raw_expression, emb, now=now)
        except ValueError:
            return False
        return False

    @staticmethod
    def match_policy(
        rule: dict,
        file_path: str,
        doc_text: str,
        status_match: str | None,
        abs_file_path: str | None = None,
        now: datetime | None = None,
        _attrs: _LazyFileAttributes | None = None,
        *,
        is_duplicate: bool = False,
    ) -> bool:
        """Evaluate a single compliance or routing policy rule against a file."""
        rule_type = str(_rule_attr(rule, "type", "") or "").lower()
        raw_expression = str(_rule_attr(rule, "expression", "") or "")
        expr_lower = raw_expression.lower()

        negate = bool(_rule_attr(rule, "negate", False))
        is_regex = bool(_rule_attr(rule, "regex", False))
        case_sensitive = bool(_rule_attr(rule, "case_sensitive", False))
        raw_match_mode = (
            str(_rule_attr(rule, "match_mode", "all") or "all").strip().lower()
        )
        match_mode = raw_match_mode if raw_match_mode in VALID_MATCH_MODES else "all"
        raw_conditions = _rule_attr(rule, "conditions", None)

        if rule_type in METADATA_POLICY_TYPES and not (
            isinstance(raw_conditions, list) and raw_conditions
        ):
            disk_path = abs_file_path or (
                file_path if file_path and os.path.isfile(file_path) else None
            )
            attrs = _attrs or _LazyFileAttributes(disk_path)
            meta_matched = PolicyEngine._match_metadata_rule(
                rule_type, raw_expression, attrs, now=now
            )
            eff_neg = negate ^ (match_mode == "none")
            return (not meta_matched) if eff_neg else meta_matched

        fp_str = str(file_path or "")
        clean_fp = fp_str.split("::", 1)[1] if "::" in fp_str else fp_str
        basename_raw = os.path.basename(clean_fp)
        fn_only = basename_raw.lower()
        ext_bare = os.path.splitext(basename_raw)[1].lstrip(".")
        path_norm = clean_fp.replace("\\", "/")
        full_path_norm = fp_str.replace("\\", "/")

        doc_text_str = str(doc_text) if doc_text else ""
        dl_lower = doc_text_str.lower()
        is_static = PolicyEngine.is_static_status(status_match)
        has_extracted_text = not bool(status_match)

        if rule_type == "duplicate" and not is_duplicate:
            return False

        # Compound condition tree evaluation
        if isinstance(raw_conditions, list) and raw_conditions:
            normalized_conditions = validate_policy_conditions(raw_conditions)
            top_neg = negate or match_mode == "none"
            if not has_extracted_text and PolicyEngine._conditions_inspect_content(
                normalized_conditions,
                under_negation=top_neg,
                require_static_defer=True,
            ):
                return False

            tree_matched = PolicyEngine._evaluate_condition_list(
                normalized_conditions,
                match_mode=match_mode,
                basename_raw=basename_raw,
                ext_bare=ext_bare,
                path_norm=path_norm,
                full_path_norm=full_path_norm,
                doc_text_str=doc_text_str,
                has_extracted_text=has_extracted_text,
                default_case_sensitive=case_sensitive,
            )
            return (not tree_matched) if negate else tree_matched

        # Validate regex upfront when regex=True
        compiled_regex: re.Pattern[str] | None = None
        if is_regex and raw_expression:
            compiled_regex = validate_safe_regex(
                raw_expression, case_sensitive=case_sensitive
            )

        effective_negate = negate ^ (match_mode == "none")

        if not is_regex and (
            rule_type in ("secret", "credential")
            or expr_lower in ("secret", "credential")
        ):
            if is_static and effective_negate:
                return False
            sec_match = PolicyEngine.contains_secrets(fp_str) or (
                has_extracted_text and PolicyEngine.contains_secrets(doc_text_str)
            )
            return (not sec_match) if effective_negate else sec_match

        if (
            is_static
            and effective_negate
            and rule_type
            in (
                "keyword",
                "action",
                "compliance",
                "unarchive",
                "",
            )
        ):
            return False

        matched = False
        if rule_type == "duplicate":
            if not raw_expression or raw_expression == "*":
                matched = True
            elif compiled_regex is not None:
                matched = bool(compiled_regex.search(basename_raw)) or (
                    has_extracted_text and bool(compiled_regex.search(doc_text_str))
                )
            elif match_pattern_expression(
                raw_expression,
                basename_raw,
                regex=False,
                case_sensitive=case_sensitive,
            ):
                matched = True
            else:
                haystack = (
                    (basename_raw if case_sensitive else fn_only)
                    if status_match
                    else (
                        f"{basename_raw} {doc_text_str}"
                        if case_sensitive
                        else f"{fn_only} {dl_lower}"
                    )
                )
                needle = raw_expression if case_sensitive else expr_lower
                matched = needle in haystack
        elif rule_type == "unarchive":
            if raw_expression:
                if compiled_regex is not None:
                    matched = bool(compiled_regex.search(basename_raw)) or (
                        has_extracted_text and bool(compiled_regex.search(doc_text_str))
                    )
                elif match_pattern_expression(
                    raw_expression,
                    basename_raw,
                    regex=False,
                    case_sensitive=case_sensitive,
                ):
                    matched = True
                else:
                    haystack = (
                        (basename_raw if case_sensitive else fn_only)
                        if status_match
                        else (
                            f"{basename_raw} {doc_text_str}"
                            if case_sensitive
                            else f"{fn_only} {dl_lower}"
                        )
                    )
                    needle = raw_expression if case_sensitive else expr_lower
                    matched = needle in haystack
            else:
                matched = bool(_rule_attr(rule, "action"))
        elif rule_type in ("keyword", "action", "compliance", ""):
            if raw_expression:
                if compiled_regex is not None:
                    matched = bool(compiled_regex.search(basename_raw)) or (
                        has_extracted_text and bool(compiled_regex.search(doc_text_str))
                    )
                else:
                    haystack = (
                        (basename_raw if case_sensitive else fn_only)
                        if status_match
                        else (
                            f"{basename_raw} {doc_text_str}"
                            if case_sensitive
                            else f"{fn_only} {dl_lower}"
                        )
                    )
                    needle = raw_expression if case_sensitive else expr_lower
                    matched = needle in haystack
            else:
                matched = bool(_rule_attr(rule, "action"))
        elif rule_type == "pattern":
            matched = match_pattern_expression(
                raw_expression,
                basename_raw,
                regex=is_regex,
                case_sensitive=case_sensitive,
            )
        elif rule_type == "override":
            if not raw_expression:
                matched = bool(_rule_attr(rule, "action"))
            elif compiled_regex is not None:
                matched = bool(
                    compiled_regex.search(basename_raw)
                    or compiled_regex.search(path_norm)
                    or compiled_regex.search(full_path_norm)
                )
            elif case_sensitive:
                matched = (
                    raw_expression == basename_raw
                    or raw_expression in basename_raw
                    or raw_expression in fp_str
                    or raw_expression in path_norm
                )
            else:
                matched = (
                    expr_lower == fn_only
                    or expr_lower in fn_only
                    or expr_lower in fp_str.lower()
                    or expr_lower in path_norm.lower()
                )

        return (not matched) if effective_negate else matched

    @staticmethod
    def sort_policies(policies: list[dict] | None) -> list[dict]:
        """Sort policy rule list by priority descending."""
        if not policies:
            return []
        return sorted(
            policies,
            key=lambda x: (
                x.get("priority", 0)
                if isinstance(x, dict)
                else getattr(x, "priority", 0)
            ),
            reverse=True,
        )

    @classmethod
    def evaluate_policies(
        cls,
        file_path: str,
        doc_text: str,
        status_match: str | None,
        policies: list[dict],
        return_halting: bool = False,
        is_sorted: bool = False,
        abs_file_path: str | None = None,
        now: datetime | None = None,
        *,
        is_duplicate: bool = False,
    ) -> PolicyEvaluationResult | None | tuple[PolicyEvaluationResult | None, bool]:
        """Find the highest priority matching compliance or routing policy rule for a file."""
        if not policies:
            return (None, False) if return_halting else None
        if is_sorted:
            sorted_policies = policies
        else:
            sorted_policies = cls.sort_policies(policies)

        disk_path = abs_file_path or (
            file_path if file_path and os.path.isfile(file_path) else None
        )
        attrs = _LazyFileAttributes(disk_path)

        matched_policy = None
        halt_evaluation = False
        is_static = cls.is_static_status(status_match)
        for rule in sorted_policies:
            is_enabled = _rule_attr(rule, "enabled", True)
            if not is_enabled:
                continue
            if cls.match_policy(
                rule,
                file_path,
                doc_text,
                status_match,
                abs_file_path=disk_path,
                now=now,
                _attrs=attrs,
                is_duplicate=is_duplicate,
            ):
                matched_policy = rule
                break
            else:
                is_halting = bool(_rule_attr(rule, "halting", False))
                if is_halting:
                    # During static pre-pass, if a rule deferred solely because it inspects
                    # content, still halt static fallback so lower-priority rules cannot bypass.
                    halt_evaluation = True
                    break
                if is_static and cls.rule_inspects_content(rule):
                    # Do not allow lower-priority static rules to shadow a higher-priority
                    # content-inspecting rule before text extraction occurs.
                    continue

        matched_result = None
        if matched_policy:
            raw_data = (
                dict(matched_policy)
                if isinstance(matched_policy, dict)
                else matched_policy.model_dump()
            )
            raw_data["halt_evaluation"] = halt_evaluation
            if "rename_pattern" not in raw_data or raw_data["rename_pattern"] is None:
                raw_data["rename_pattern"] = ""
            resolved_dt = attrs.resolve_date_str(str(raw_data.get("type", "")))
            if resolved_dt:
                raw_data["resolved_date"] = resolved_dt
            matched_result = PolicyEvaluationResult.model_validate(raw_data)

        if return_halting:
            return matched_result, halt_evaluation
        return matched_result

    @staticmethod
    def validate_lock_path_safe(
        lock_path: Any, file_path: str | None = None
    ) -> tuple[bool, str | None]:
        """Validate a folder lock path safely without raising exceptions.

        Returns
        -------
            tuple[bool, str | None]: (is_valid, error_message)
            (True, None) if valid, (False, error_message) if invalid.
        """
        if not isinstance(lock_path, str):
            return False, f"Lock path must be a string. Got {type(lock_path)}"
        try:
            validate_target_path(
                lock_path, keyword=os.path.basename(file_path) if file_path else None
            )
            return True, None
        except ValueError as err:
            return False, str(err)

    @staticmethod
    def validate_lock_path(lock_path: str, file_path: str | None = None) -> None:
        """Validate a folder lock path against the established policy schema (path rules).

        Raises ValueError if invalid.
        """
        is_valid, error_msg = PolicyEngine.validate_lock_path_safe(
            lock_path, file_path=file_path
        )
        if not is_valid:
            raise ValueError(error_msg)


def _canonicalize_condition_node(
    node: dict[str, Any], default_case: bool = False
) -> tuple[Any, ...]:
    """Build a hashable canonical representation of a condition node."""
    sub = node.get("conditions")
    if isinstance(sub, list):
        mode = str(node.get("match_mode", "all") or "all").strip().lower()
        neg = bool(node.get("negate", False))
        children = tuple(
            sorted(
                _canonicalize_condition_node(c, default_case)
                for c in sub
                if isinstance(c, dict)
            )
        )
        return ("group", mode, neg, children)

    fld = str(node.get("field", "name")).strip().lower()
    op = str(node.get("operator", "contains")).strip().lower()
    eff_case = (
        bool(node["case_sensitive"])
        if "case_sensitive" in node and node["case_sensitive"] is not None
        else default_case
    )
    raw_val = str(node.get("value", ""))
    val = raw_val if (eff_case or op == "regex") else raw_val.lower()
    if fld == "extension" and op in ("equals", "contains", "starts_with", "ends_with"):
        val = val.lstrip(".")
    neg = bool(node.get("negate", False))
    return ("leaf", fld, op, val, neg, eff_case)


def canonicalize_condition_node(
    node: dict[str, Any], default_case: bool = False
) -> tuple[Any, ...]:
    """Return a hashable canonical tuple for a condition clause or group."""
    return _canonicalize_condition_node(node, default_case=default_case)


def _type_subsumes(ha_type: str, lo_type: str) -> bool:
    """Return True if ``ha_type`` covers at least the same match surface as ``lo_type``."""
    if ha_type == lo_type:
        return True
    if ha_type == "keyword":
        return True
    if ha_type == "pattern" and lo_type in ("pattern", "override"):
        return True
    return False


def is_masked_by(higher_rule: Any, lower_rule: Any) -> bool:
    """Check if a lower priority policy rule is masked by a higher priority policy rule."""
    ha_type = str(_rule_attr(higher_rule, "type", "") or "").lower()
    lo_type = str(_rule_attr(lower_rule, "type", "") or "").lower()
    if not _type_subsumes(ha_type, lo_type):
        return False

    ha_conds = _rule_attr(higher_rule, "conditions", None)
    lo_conds = _rule_attr(lower_rule, "conditions", None)
    ha_has_conds = isinstance(ha_conds, list) and len(ha_conds) > 0
    lo_has_conds = isinstance(lo_conds, list) and len(lo_conds) > 0

    ha_mode = str(_rule_attr(higher_rule, "match_mode", "all") or "all").strip().lower()
    lo_mode = str(_rule_attr(lower_rule, "match_mode", "all") or "all").strip().lower()
    ha_neg = bool(_rule_attr(higher_rule, "negate", False))
    lo_neg = bool(_rule_attr(lower_rule, "negate", False))
    ha_regex = bool(_rule_attr(higher_rule, "regex", False))
    lo_regex = bool(_rule_attr(lower_rule, "regex", False))
    ha_case = bool(_rule_attr(higher_rule, "case_sensitive", False))
    lo_case = bool(_rule_attr(lower_rule, "case_sensitive", False))

    if ha_has_conds or lo_has_conds:
        if not (ha_has_conds and lo_has_conds):
            return False
        ha_set = {
            _canonicalize_condition_node(c, ha_case)
            for c in ha_conds
            if isinstance(c, dict)
        }
        lo_set = {
            _canonicalize_condition_node(c, lo_case)
            for c in lo_conds
            if isinstance(c, dict)
        }
        if not ha_set or not lo_set:
            return False
        if ha_mode == lo_mode and ha_neg == lo_neg and ha_set == lo_set:
            return True
        if not ha_neg and not lo_neg:
            if ha_mode == "all" and lo_mode == "all" and ha_set.issubset(lo_set):
                return True
            if ha_mode == "any" and lo_mode == "any" and lo_set.issubset(ha_set):
                return True
        return False

    ha_raw = str(_rule_attr(higher_rule, "expression", "") or "")
    lo_raw = str(_rule_attr(lower_rule, "expression", "") or "")
    if not ha_raw or not lo_raw:
        return False

    ha_eff_neg = ha_neg ^ (ha_mode == "none")
    lo_eff_neg = lo_neg ^ (lo_mode == "none")

    if ha_type in METADATA_POLICY_TYPES or lo_type in METADATA_POLICY_TYPES:
        return (
            ha_type == lo_type
            and ha_eff_neg == lo_eff_neg
            and re.sub(r"\s+", "", ha_raw.lower().strip())
            == re.sub(r"\s+", "", lo_raw.lower().strip())
        )

    if ha_regex or lo_regex:
        if ha_regex != lo_regex or ha_eff_neg != lo_eff_neg:
            return False
        if ha_case and not lo_case:
            return False
        return (ha_raw == lo_raw) if ha_case else (ha_raw.lower() == lo_raw.lower())

    if ha_eff_neg != lo_eff_neg:
        return False

    if ha_case and not lo_case:
        return False

    ha_cmp = ha_raw if ha_case else ha_raw.lower()
    lo_cmp = lo_raw if ha_case else lo_raw.lower()

    if ha_eff_neg:
        return ha_cmp == lo_cmp

    return ha_cmp in lo_cmp
