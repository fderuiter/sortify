"""Pattern token formatter for dynamic file renaming.

Parses pattern template strings containing metadata tokens such as
{date}, {category}, {original}, {extension}, and {seq}, substitutes values,
handles fallbacks for missing metadata, and sanitizes output names.
"""

import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.path_utils import sanitize_name

logger = logging.getLogger(__name__)

# Standard recognized tokens
SUPPORTED_TOKENS = {"date", "category", "original", "extension", "seq"}

DEFAULT_TOKEN_FALLBACKS: Dict[str, Any] = {
    "date": lambda: datetime.now().strftime("%Y-%m-%d"),
    "category": "uncategorized",
    "original": "file",
    "extension": "",
    "seq": "1",
}


class PatternTokenFormatter:
    """Parses and formats filename pattern templates using metadata tokens."""

    @classmethod
    def parse_tokens(cls, pattern: str) -> List[str]:
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
    def format_pattern(
        cls,
        pattern: str,
        metadata: Optional[Dict[str, Any]] = None,
        seq: int = 1,
        fallback_original: str = "",
    ) -> str:
        """Format a pattern template string with provided metadata values.

        Args:
            pattern: Pattern template containing tokens like '{date}_{category}_{original}'.
            metadata: Dict of metadata key-value pairs (date, category, original, extension, seq).
            seq: Sequence index number (default 1).
            fallback_original: Original filename stem or full filename to fall back on error.

        Returns
        -------
            Sanitized formatted filename string.
        """
        if not pattern or not isinstance(pattern, str) or not pattern.strip():
            if fallback_original:
                return sanitize_name(fallback_original)
            return ""

        meta = dict(metadata or {})

        # Resolve original stem and extension from fallback_original if not explicitly in metadata
        fallback_stem = ""
        fallback_ext = ""
        if fallback_original:
            fallback_stem, fallback_ext = os.path.splitext(
                os.path.basename(fallback_original)
            )

        if "original" not in meta or meta["original"] is None or str(meta["original"]).strip() == "":
            meta["original"] = fallback_stem if fallback_stem else DEFAULT_TOKEN_FALLBACKS["original"]

        if "extension" not in meta or meta["extension"] is None:
            meta["extension"] = fallback_ext if fallback_ext else DEFAULT_TOKEN_FALLBACKS["extension"]

        if "date" not in meta or meta["date"] is None or str(meta["date"]).strip() == "":
            date_prov = DEFAULT_TOKEN_FALLBACKS["date"]
            meta["date"] = date_prov() if callable(date_prov) else str(date_prov)
        elif isinstance(meta["date"], (datetime,)):
            meta["date"] = meta["date"].strftime("%Y-%m-%d")

        if (
            "category" not in meta
            or meta["category"] is None
            or str(meta["category"]).strip() == ""
        ):
            meta["category"] = DEFAULT_TOKEN_FALLBACKS["category"]

        if "seq" not in meta or meta["seq"] is None:
            meta["seq"] = seq

        seq_val = meta["seq"]
        if isinstance(seq_val, int):
            meta["seq"] = f"{seq_val:02d}" if seq_val < 10 else str(seq_val)
        else:
            meta["seq"] = str(seq_val)

        # Normalize extension formatting
        ext_val = str(meta.get("extension", ""))
        if ext_val and not ext_val.startswith("."):
            ext_val = f".{ext_val}"

        try:
            tokens_in_pattern = cls.parse_tokens(pattern)
            
            # If no tokens present in pattern string, treat pattern as static string
            if not tokens_in_pattern:
                safe_stem = sanitize_name(pattern)
                if fallback_ext and not safe_stem.endswith(fallback_ext):
                    return f"{safe_stem}{fallback_ext}"
                return safe_stem

            has_extension_token = "extension" in [t.lower() for t in tokens_in_pattern]

            formatted = pattern

            for token in set(tokens_in_pattern):
                token_lower = token.lower()
                placeholder = f"{{{token}}}"

                if token_lower == "extension":
                    if f".{placeholder}" in formatted:
                        clean_ext = ext_val.lstrip(".")
                        formatted = formatted.replace(f".{placeholder}", f".{clean_ext}")
                    else:
                        formatted = formatted.replace(placeholder, ext_val)
                elif token_lower in meta and meta[token_lower] is not None:
                    val_str = str(meta[token_lower])
                    formatted = formatted.replace(placeholder, val_str)
                else:
                    # Token missing in metadata -> substitute clean default
                    fallback_val = DEFAULT_TOKEN_FALLBACKS.get(token_lower, token_lower)
                    fallback_str = (
                        str(fallback_val()) if callable(fallback_val) else str(fallback_val)
                    )
                    formatted = formatted.replace(placeholder, fallback_str)

            # Sanitize output name against invalid path characters, PII, and secret patterns
            if has_extension_token:
                # Pattern explicitly controlled extension
                base, ext = os.path.splitext(formatted)
                safe_base = sanitize_name(base)
                safe_ext = ext if ext else ext_val
                return f"{safe_base}{safe_ext}"
            else:
                # Pattern formatted file stem; preserve extension
                safe_stem = sanitize_name(formatted)
                if ext_val and not safe_stem.endswith(ext_val):
                    return f"{safe_stem}{ext_val}"
                return safe_stem

        except Exception as err:
            logger.warning(
                f"Pattern token formatting failed for pattern '{pattern}': {err}. "
                f"Falling back to original name."
            )
            if fallback_original:
                return sanitize_name(fallback_original)
            return pattern


def format_pattern(
    pattern: str,
    metadata: Optional[Dict[str, Any]] = None,
    seq: int = 1,
    fallback_original: str = "",
) -> str:
    """Format pattern string using PatternTokenFormatter."""
    return PatternTokenFormatter.format_pattern(
        pattern=pattern,
        metadata=metadata,
        seq=seq,
        fallback_original=fallback_original,
    )
