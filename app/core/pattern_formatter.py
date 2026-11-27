"""Pattern token formatter for dynamic file renaming and destination folder paths.

Parses pattern template strings containing metadata tokens such as
{date}, {year}, {month}, {day}, {category}, {original}, {stem}, {keywords},
{extension}, {seq}, and {sequence}, substitutes values, resolves disk modification
timestamps lazily, and validates/sanitizes output filenames and relative folder paths.
"""

import logging
import os
import re
from datetime import datetime
from typing import Any

from app.core.path_utils import sanitize_name, validate_target_path

logger = logging.getLogger(__name__)

# Standard recognized tokens
SUPPORTED_TOKENS = {
    "date",
    "year",
    "month",
    "day",
    "category",
    "original",
    "stem",
    "keywords",
    "extension",
    "seq",
    "sequence",
}

DEFAULT_TOKEN_FALLBACKS: dict[str, Any] = {
    "date": lambda: datetime.now().strftime("%Y-%m-%d"),
    "year": lambda: datetime.now().strftime("%Y"),
    "month": lambda: datetime.now().strftime("%m"),
    "day": lambda: datetime.now().strftime("%d"),
    "category": "uncategorized",
    "original": "file",
    "stem": "file",
    "keywords": "",
    "extension": "",
    "seq": "1",
    "sequence": "1",
}


def resolve_disk_file_date(
    file_path: str | os.PathLike[str] | None,
    base_dir: str | os.PathLike[str] | None = None,
) -> str | None:
    """Read a file's modification timestamp from disk and format as YYYY-MM-DD.

    Returns None when no path is given or the file cannot be stated on disk.
    """
    if not file_path:
        return None
    try:
        raw_path = os.fspath(file_path)
    except TypeError:
        return None
    if not raw_path or not raw_path.strip():
        return None

    candidates: list[str] = [raw_path]
    if base_dir and not os.path.isabs(raw_path):
        try:
            base_str = os.fspath(base_dir)
            if base_str:
                candidates.insert(0, os.path.join(base_str, raw_path))
        except TypeError:
            pass

    for candidate in candidates:
        try:
            st = os.stat(candidate)
            return datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            continue
    return None


def _split_date_parts(date_str: str) -> tuple[str, str, str]:
    """Extract (YYYY, MM, DD) parts from an ISO or compact date string."""
    cleaned = str(date_str or "").strip()
    m = re.match(r"^(\d{4})[\-/](\d{1,2})[\-/](\d{1,2})", cleaned)
    if m:
        return m.group(1), f"{int(m.group(2)):02d}", f"{int(m.group(3)):02d}"
    m_compact = re.match(r"^(\d{4})(\d{2})(\d{2})", cleaned)
    if m_compact:
        return m_compact.group(1), m_compact.group(2), m_compact.group(3)
    now = datetime.now()
    return now.strftime("%Y"), now.strftime("%m"), now.strftime("%d")


class PatternTokenFormatter:
    """Parses and formats filename and folder pattern templates using metadata tokens."""

    @classmethod
    def parse_tokens(cls, pattern: str) -> list[str]:
        """Extract token placeholder names enclosed in braces from pattern string.

        Args:
            pattern: Template pattern string, e.g. '{date}_{category}_{original}'.

        Returns
        -------
            List of token names found in the pattern.
        """
        if not pattern:
            return []
        return re.findall(r"\{([a-zA-Z0-9_]+)\}", pattern)

    @classmethod
    def _prepare_metadata(
        cls,
        metadata: dict[str, Any] | None = None,
        seq: int = 1,
        fallback_original: str = "",
        file_path: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        """Normalize metadata dictionary and resolve stem, extension, date, year/month/day, and sequence."""
        meta = dict(metadata or {})

        fallback_stem = ""
        fallback_ext = ""
        name_hint = (
            fallback_original
            or file_path
            or str(meta.get("source_path") or meta.get("filepath") or "")
        )
        if name_hint:
            fallback_stem, fallback_ext = os.path.splitext(os.path.basename(name_hint))

        raw_stem = meta.get("stem")
        raw_orig = meta.get("original")
        has_stem = raw_stem is not None and str(raw_stem).strip() != ""
        has_orig = raw_orig is not None and str(raw_orig).strip() != ""

        if has_stem and not has_orig:
            meta["original"] = str(raw_stem)
            meta["stem"] = str(raw_stem)
        elif has_orig and not has_stem:
            orig_str = str(raw_orig)
            meta["original"] = orig_str
            orig_stem, orig_ext = os.path.splitext(orig_str)
            meta["stem"] = (
                orig_stem
                if orig_stem
                and orig_ext
                and re.match(r"^\.[a-zA-Z0-9]{1,8}$", orig_ext)
                else orig_str
            )
        elif not has_stem and not has_orig:
            resolved_stem = (
                fallback_stem if fallback_stem else DEFAULT_TOKEN_FALLBACKS["original"]
            )
            meta["original"] = resolved_stem
            meta["stem"] = resolved_stem
        else:
            meta["original"] = str(raw_orig)
            meta["stem"] = str(raw_stem)

        if "extension" not in meta or meta["extension"] is None:
            meta["extension"] = (
                fallback_ext if fallback_ext else DEFAULT_TOKEN_FALLBACKS["extension"]
            )

        raw_date = meta.get("date")
        if raw_date is None or str(raw_date).strip() == "":
            raw_date = meta.get("file_date")

        if raw_date is None or str(raw_date).strip() == "":
            disk_date = resolve_disk_file_date(
                file_path or meta.get("source_path") or meta.get("filepath"),
                base_dir=meta.get("base_dir"),
            )
            if disk_date:
                meta["date"] = disk_date
            else:
                date_prov = DEFAULT_TOKEN_FALLBACKS["date"]
                meta["date"] = date_prov() if callable(date_prov) else str(date_prov)
        elif isinstance(raw_date, datetime):
            meta["date"] = raw_date.strftime("%Y-%m-%d")
        else:
            meta["date"] = str(raw_date).strip()

        year_str, month_str, day_str = _split_date_parts(str(meta["date"]))
        if not meta.get("year") or str(meta.get("year")).strip() == "":
            meta["year"] = year_str
        if not meta.get("month") or str(meta.get("month")).strip() == "":
            meta["month"] = month_str
        if not meta.get("day") or str(meta.get("day")).strip() == "":
            meta["day"] = day_str

        if (
            "category" not in meta
            or meta["category"] is None
            or str(meta["category"]).strip() == ""
        ):
            meta["category"] = DEFAULT_TOKEN_FALLBACKS["category"]

        raw_keywords = meta.get("keywords")
        meta["keywords"] = "" if raw_keywords is None else str(raw_keywords).strip()

        raw_seq = meta.get("seq")
        if raw_seq is None:
            raw_seq = meta.get("sequence")
        if raw_seq is None:
            raw_seq = seq

        if isinstance(raw_seq, int):
            formatted_seq = f"{raw_seq:02d}" if raw_seq < 10 else str(raw_seq)
        else:
            formatted_seq = str(raw_seq)
        meta["seq"] = formatted_seq
        meta["sequence"] = formatted_seq

        ext_val = str(meta.get("extension", ""))
        if ext_val and not ext_val.startswith("."):
            ext_val = f".{ext_val}"

        return meta, fallback_ext, ext_val

    @classmethod
    def format_pattern(
        cls,
        pattern: str,
        metadata: dict[str, Any] | None = None,
        seq: int = 1,
        fallback_original: str = "",
        file_path: str | None = None,
    ) -> str:
        """Format a pattern template string with provided metadata values.

        Args:
            pattern: Pattern template containing tokens like '{date}_{category}_{original}'.
            metadata: Dict of metadata key-value pairs (date, year, month, day, category, original/stem, extension, seq).
            seq: Sequence index number (default 1).
            fallback_original: Original filename stem or full filename to fall back on error.
            file_path: Optional filesystem path used to resolve disk modification date lazily.

        Returns
        -------
            Sanitized formatted filename string.
        """
        if not pattern or not isinstance(pattern, str) or not pattern.strip():
            if fallback_original:
                return sanitize_name(fallback_original)
            return ""

        meta, fallback_ext, ext_val = cls._prepare_metadata(
            metadata=metadata,
            seq=seq,
            fallback_original=fallback_original,
            file_path=file_path,
        )

        try:
            tokens_in_pattern = cls.parse_tokens(pattern)

            # If no tokens present in pattern string, treat pattern as static string
            if not tokens_in_pattern:
                safe_stem = sanitize_name(pattern)
                if fallback_ext and not safe_stem.endswith(fallback_ext):
                    return f"{safe_stem}{fallback_ext}"
                return safe_stem

            has_extension_token = "extension" in [t.lower() for t in tokens_in_pattern]
            has_empty_keywords = "keywords" in [
                t.lower() for t in tokens_in_pattern
            ] and not meta.get("keywords")

            formatted = pattern

            for token in set(tokens_in_pattern):
                token_lower = token.lower()
                placeholder = f"{{{token}}}"

                if token_lower == "extension":
                    if f".{placeholder}" in formatted:
                        clean_ext = ext_val.lstrip(".")
                        formatted = formatted.replace(
                            f".{placeholder}", f".{clean_ext}"
                        )
                    else:
                        formatted = formatted.replace(placeholder, ext_val)
                elif token_lower in meta and meta[token_lower] is not None:
                    val_str = str(meta[token_lower])
                    formatted = formatted.replace(placeholder, val_str)
                else:
                    # Token missing in metadata -> substitute clean default
                    fallback_val = DEFAULT_TOKEN_FALLBACKS.get(token_lower, token_lower)
                    fallback_str = (
                        str(fallback_val())
                        if callable(fallback_val)
                        else str(fallback_val)
                    )
                    formatted = formatted.replace(placeholder, fallback_str)

            # Sanitize output name against invalid path characters, PII, and secret patterns
            if has_extension_token:
                # Pattern explicitly controlled extension
                base, ext = os.path.splitext(formatted)
                if has_empty_keywords:
                    base = re.sub(r"([_\-\s])\1+", r"\1", base).strip("_- ")
                    if not base:
                        base = str(meta.get("stem") or DEFAULT_TOKEN_FALLBACKS["stem"])
                safe_base = sanitize_name(base)
                safe_ext = ext if ext else ext_val
                return f"{safe_base}{safe_ext}"
            else:
                # Pattern formatted file stem; preserve extension
                stem_candidate = formatted
                if has_empty_keywords:
                    stem_candidate = re.sub(
                        r"([_\-\s])\1+", r"\1", stem_candidate
                    ).strip("_- ")
                    if not stem_candidate:
                        stem_candidate = str(
                            meta.get("stem") or DEFAULT_TOKEN_FALLBACKS["stem"]
                        )
                safe_stem = sanitize_name(stem_candidate)
                if ext_val and not safe_stem.endswith(ext_val):
                    return f"{safe_stem}{ext_val}"
                return safe_stem

        except Exception as err:
            logger.warning(
                f"Pattern token formatting failed for pattern '{pattern}': {err}. "
                f"Falling back to original name.",
                exc_info=True,
            )
            if fallback_original:
                return sanitize_name(fallback_original)
            return pattern

    @classmethod
    def format_target_path(
        cls,
        target_path: str,
        metadata: dict[str, Any] | None = None,
        seq: int = 1,
        fallback_original: str = "",
        file_path: str | None = None,
    ) -> str:
        """Expand metadata placeholders inside a relative destination folder path and validate safety.

        Supports {date}, {year}, {month}, {day}, {category}, {seq}, {sequence},
        {extension}, {stem}, and {original}, sanitizing dynamic token values with
        sanitize_name and validating the final relative path with validate_target_path.
        """
        if not isinstance(target_path, str):
            raise ValueError("Target path must be a string.")
        if not target_path.strip():
            validate_target_path(target_path)
            return target_path

        validate_target_path(target_path)
        if "{" not in target_path:
            return target_path

        meta, _, ext_val = cls._prepare_metadata(
            metadata=metadata,
            seq=seq,
            fallback_original=fallback_original,
            file_path=file_path,
        )
        clean_ext = ext_val.lstrip(".") or "unknown"

        raw_segments = target_path.replace("\\", "/").split("/")
        expanded_segments: list[str] = []

        for segment in raw_segments:
            if not segment:
                continue
            seg_tokens = cls.parse_tokens(segment)
            if not seg_tokens:
                expanded_segments.append(segment)
                continue

            rendered_seg = segment
            for token in set(seg_tokens):
                token_lower = token.lower()
                placeholder = f"{{{token}}}"
                if token_lower == "extension":
                    raw_val = clean_ext
                elif token_lower in meta and meta[token_lower] is not None:
                    raw_val = str(meta[token_lower])
                else:
                    fallback_val = DEFAULT_TOKEN_FALLBACKS.get(token_lower, token_lower)
                    raw_val = (
                        str(fallback_val())
                        if callable(fallback_val)
                        else str(fallback_val)
                    )
                safe_token_val = sanitize_name(raw_val) if raw_val else "unknown"
                rendered_seg = rendered_seg.replace(placeholder, safe_token_val)

            safe_seg = sanitize_name(rendered_seg)
            expanded_segments.append(safe_seg)

        expanded = "/".join(expanded_segments)
        validate_target_path(expanded)
        return expanded


def format_pattern(
    pattern: str,
    metadata: dict[str, Any] | None = None,
    seq: int = 1,
    fallback_original: str = "",
    file_path: str | None = None,
) -> str:
    """Format pattern string using PatternTokenFormatter."""
    return PatternTokenFormatter.format_pattern(
        pattern=pattern,
        metadata=metadata,
        seq=seq,
        fallback_original=fallback_original,
        file_path=file_path,
    )
