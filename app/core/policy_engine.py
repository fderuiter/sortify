"""Policy Engine module for evaluating compliance policies and validating lock paths."""

import calendar
import fnmatch
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from app.core.path_utils import validate_target_path

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


def match_pattern_expression(expression: str, filename: str) -> bool:
    """Evaluate a pattern expression against a filename using wildcards, extension masks, or substring matching.

    Parameters
    ----------
    expression : str
        Pattern rule expression (glob mask such as ``*.pdf``, extension mask such as ``.pdf``,
        or plain substring text).
    filename : str
        File path or basename to test against the pattern expression.

    Returns
    -------
    bool
        True if the filename matches the pattern expression, False otherwise.
    """
    if not expression or not filename:
        return False
    expr_lower = str(expression).strip().lower()
    if not expr_lower:
        return False
    fn_only = os.path.basename(str(filename)).lower()
    if not fn_only:
        return False

    has_wildcard = (
        "*" in expr_lower
        or "?" in expr_lower
        or ("[" in expr_lower and "]" in expr_lower)
    )
    if has_wildcard:
        return fnmatch.fnmatchcase(fn_only, expr_lower)

    if expr_lower.startswith(".") and "/" not in expr_lower and "\\" not in expr_lower:
        return fnmatch.fnmatchcase(fn_only, f"*{expr_lower}")

    return expr_lower in fn_only


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
        rule_type = (
            rule.get("type", "").lower()
            if isinstance(rule, dict)
            else getattr(rule, "type", "").lower()
        )
        raw_expression = (
            str(rule.get("expression", ""))
            if isinstance(rule, dict)
            else str(getattr(rule, "expression", ""))
        )
        expression = raw_expression.lower()

        if rule_type in METADATA_POLICY_TYPES:
            disk_path = abs_file_path or (
                file_path if file_path and os.path.isfile(file_path) else None
            )
            attrs = _attrs or _LazyFileAttributes(disk_path)
            return PolicyEngine._match_metadata_rule(
                rule_type, raw_expression, attrs, now=now
            )

        fn_only = os.path.basename(file_path).lower()
        doc_text_str = str(doc_text) if doc_text else ""
        dl_lower = doc_text_str.lower()

        if rule_type in ("secret", "credential") or expression in (
            "secret",
            "credential",
        ):
            return PolicyEngine.contains_secrets(
                file_path
            ) or PolicyEngine.contains_secrets(doc_text_str)

        if rule_type == "duplicate":
            if not is_duplicate:
                return False
            if not expression or expression == "*":
                return True
            if match_pattern_expression(expression, fn_only):
                return True
            text_to_search = fn_only if status_match else (fn_only + " " + dl_lower)
            return expression in text_to_search

        if rule_type == "unarchive":
            if expression:
                if match_pattern_expression(expression, fn_only):
                    return True
                text_to_search = fn_only if status_match else (fn_only + " " + dl_lower)
                return expression in text_to_search
            return bool(rule.get("action"))
        if rule_type in ("keyword", "action", "compliance", ""):
            if expression:
                text_to_search = fn_only if status_match else (fn_only + " " + dl_lower)
                return expression in text_to_search
            # If no expression is given but an action directive exists, match unconditionally
            return bool(rule.get("action"))
        elif rule_type == "pattern":
            return match_pattern_expression(expression, fn_only)
        elif rule_type == "override":
            if not expression:
                action_val = (
                    rule.get("action")
                    if isinstance(rule, dict)
                    else getattr(rule, "action", None)
                )
                return bool(action_val)
            return (
                expression == fn_only
                or expression in fn_only
                or expression in file_path.lower()
            )
        return False

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
        for rule in sorted_policies:
            is_enabled = (
                rule.get("enabled", True)
                if isinstance(rule, dict)
                else getattr(rule, "enabled", True)
            )
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
                is_halting = (
                    rule.get("halting", False)
                    if isinstance(rule, dict)
                    else getattr(rule, "halting", False)
                )
                if is_halting:
                    halt_evaluation = True
                    break

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


def is_masked_by(higher_rule: Any, lower_rule: Any) -> bool:
    """Check if a lower priority rule expression is masked by a higher priority rule expression."""
    ha_type = (
        higher_rule.get("type", "").lower()
        if hasattr(higher_rule, "get")
        else getattr(higher_rule, "type", "").lower()
    )
    lo_type = (
        lower_rule.get("type", "").lower()
        if hasattr(lower_rule, "get")
        else getattr(lower_rule, "type", "").lower()
    )
    ha_expr = (
        higher_rule.get("expression", "").lower().strip()
        if hasattr(higher_rule, "get")
        else getattr(higher_rule, "expression", "").lower().strip()
    )
    lo_expr = (
        lower_rule.get("expression", "").lower().strip()
        if hasattr(lower_rule, "get")
        else getattr(lower_rule, "expression", "").lower().strip()
    )

    if not ha_expr or not lo_expr:
        return False

    if ha_type in METADATA_POLICY_TYPES or lo_type in METADATA_POLICY_TYPES:
        return ha_type == lo_type and re.sub(r"\s+", "", ha_expr) == re.sub(
            r"\s+", "", lo_expr
        )

    if ha_expr in lo_expr:
        if ha_type == "keyword":
            return True
        if ha_type == "pattern":
            if lo_type in ("pattern", "override"):
                return True
        if ha_type == "override" and lo_type == "override":
            return True
    return False
