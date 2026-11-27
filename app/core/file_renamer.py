"""Modular Heuristic & Context-Aware File Renaming Engine.

Evaluates filename quality using heuristics and generates descriptive,
context-aware filenames as an independent pipeline stage while respecting user privacy,
AI consent, and path safety guardrails.
"""

import contextlib
import gc
import logging
import os
import re
from collections import Counter

from app.core.mover import is_subpath_or_equal
from app.core.path_utils import sanitize_name

logger = logging.getLogger(__name__)

# Patterns indicative of non-descriptive / poorly-named files
GENERIC_BASE_PATTERNS = [
    r"^scan",
    r"^scanned",
    r"^image",
    r"^img",
    r"^photo",
    r"^doc",
    r"^document",
    r"^file",
    r"^untitled",
    r"^new_doc",
    r"^new_document",
    r"^copy",
    r"^download",
    r"^output",
    r"^draft",
    r"^temp",
    r"^tmp",
    r"^page",
    r"^paper",
    r"^item",
    r"^data",
    r"^text",
    r"^receipt",
    r"^invoice",
    r"^statement",
]

# Exact generic words for multi-token compound stem checks (e.g. scan_001_invoice)
_GENERIC_EXACT_WORDS = {
    "scan",
    "scanned",
    "image",
    "img",
    "photo",
    "doc",
    "document",
    "file",
    "untitled",
    "new",
    "copy",
    "download",
    "output",
    "draft",
    "temp",
    "tmp",
    "page",
    "paper",
    "item",
    "data",
    "text",
    "receipt",
    "invoice",
    "statement",
}

# Patterns for numeric suffixes / timestamps / copy counters
NUMERIC_SUFFIX_PATTERNS = [
    r"[-_\s]?\d+$",
    r"[-_\s]?\(\d+\)$",
    r"[-_\s]?v\d+(?:\.\d+)?$",
    r"[-_\s]?(?:19|20)\d{6,8}(?:[-_\s]?\d+)?$",
    r"[-_\s]?[a-f0-9]{8,32}$",
]


class HeuristicEvaluator:
    """Evaluates filename quality to identify non-descriptive or poorly-named files."""

    @classmethod
    def is_poorly_named(cls, filename: str) -> bool:
        """Evaluate if a filename is non-descriptive or poorly named.

        Checks pattern matching, generic words, numeric suffixes, timestamps,
        hex sequences, or short non-descriptive bases.
        """
        if not filename:
            return False

        base, _ = os.path.splitext(os.path.basename(filename))
        base_clean = base.strip().lower()

        if not base_clean:
            return True

        # Rule 1: Very short base filename (<= 3 characters) or only digits/punctuation
        if len(base_clean) <= 3:
            return True

        if re.match(r"^[\d\s\-_\.,\(\)]+$", base_clean):
            return True

        # Rule 2: Matches generic word patterns with optional numeric/copy/generic suffix
        for gen_pat in GENERIC_BASE_PATTERNS:
            if re.match(gen_pat, base_clean, re.IGNORECASE):
                rest = re.sub(gen_pat, "", base_clean, flags=re.IGNORECASE).strip(
                    " -_()[]"
                )
                if (
                    not rest
                    or re.match(r"^[\d\s\-_\.,\(\)v]+$", rest)
                    or re.match(r"^(?:copy|\d+)+$", rest)
                    or any(
                        re.match(gp, rest, re.IGNORECASE)
                        for gp in GENERIC_BASE_PATTERNS
                    )
                    or len(rest) <= 3
                ):
                    return True

        # Rule 3: Check for explicit numeric/hex suffix patterns on short or generic stems
        for num_pat in NUMERIC_SUFFIX_PATTERNS:
            if re.search(num_pat, base_clean, re.IGNORECASE):
                prefix = re.sub(num_pat, "", base_clean, flags=re.IGNORECASE).strip(
                    " -_()[]"
                )
                if (
                    not prefix
                    or len(prefix) <= 4
                    or any(
                        re.match(gp, prefix, re.IGNORECASE)
                        for gp in GENERIC_BASE_PATTERNS
                    )
                ):
                    return True

        # Rule 4: Compound stem composed solely of generic words and numeric/version tokens
        parts = [p for p in re.split(r"[\s\-_\.,\(\)\[\]]+", base_clean) if p]
        if parts and all(
            p in _GENERIC_EXACT_WORDS
            or bool(
                re.match(
                    r"^(?:v?\d+(?:\.\d+)?|(?:19|20)\d{6,8}|[a-f0-9]{8,32})$",
                    p,
                    re.IGNORECASE,
                )
            )
            for p in parts
        ):
            return True

        return False

    @classmethod
    def evaluate_filename(cls, filename: str) -> dict:
        """Return structured evaluation report for a filename."""
        poorly_named = cls.is_poorly_named(filename)
        return {
            "filename": filename,
            "is_poorly_named": poorly_named,
            "reason": (
                "Pattern match / numeric suffix or generic term"
                if poorly_named
                else "Descriptive filename"
            ),
        }


_DEFAULT_STOP_WORDS = {
    "the",
    "and",
    "for",
    "this",
    "that",
    "with",
    "from",
    "inc",
    "com",
    "pdf",
    "docx",
    "txt",
    "xlsx",
    "png",
    "jpg",
    "jpeg",
    "file",
    "document",
    "page",
    "date",
    "total",
    "amount",
    "name",
    "type",
    "status",
    "version",
    "subject",
    "dear",
    "sir",
    "madam",
    "regards",
    "sincerely",
    "http",
    "https",
    "www",
    "org",
    "net",
    "email",
    "mail",
    "tel",
    "fax",
    "phone",
    "address",
}

_MONTH_NAME_TO_NUM = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _coerce_numeric_vector(raw_vec: object):
    """Validate and convert an embedding vector into a unit-normalized 1D float32 numpy array."""
    import numpy as np

    if not isinstance(raw_vec, (list, tuple, np.ndarray)):
        return None
    if len(raw_vec) < 2:
        return None
    first = raw_vec[0]
    if isinstance(first, bool) or not isinstance(first, (int, float, np.number)):
        return None
    try:
        arr = np.asarray(raw_vec, dtype=np.float32)
    except (TypeError, ValueError):
        return None
    if arr.ndim != 1 or arr.size < 2 or not np.all(np.isfinite(arr)):
        return None
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-8:
        return None
    return arr / norm


class ContextExtractor:
    """Extracts semantic and statistical key terms and dates from document text."""

    @staticmethod
    def extract_document_date(text: str) -> str | None:
        """Extract an explicit calendar date from document text as YYYY-MM-DD."""
        if not text or not isinstance(text, str) or text.startswith("[STATUS:"):
            return None
        from datetime import datetime

        snippet = text[:4000]
        # 1. ISO / slash dates: YYYY-MM-DD or YYYY/MM/DD
        for m in re.finditer(
            r"\b((?:19|20)\d{2})[\-/](0?[1-9]|1[0-2])[\-/](0?[1-9]|[12]\d|3[01])\b",
            snippet,
        ):
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

        # 2. Month DD, YYYY (e.g. March 15, 2024)
        for m in re.finditer(
            r"(?i)\b([a-z]{3,9})\.?\s+(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})\b",
            snippet,
        ):
            mo = _MONTH_NAME_TO_NUM.get(m.group(1).lower())
            if mo:
                try:
                    dt = datetime(int(m.group(3)), mo, int(m.group(2)))
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue

        # 3. DD Month YYYY (e.g. 15 March 2024)
        for m in re.finditer(
            r"(?i)\b(0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?\s+((?:19|20)\d{2})\b",
            snippet,
        ):
            mo = _MONTH_NAME_TO_NUM.get(m.group(2).lower())
            if mo:
                try:
                    dt = datetime(int(m.group(3)), mo, int(m.group(1)))
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue

        return None

    @staticmethod
    def extract_keywords_tfidf(
        text: str, stop_words: set[str] | None = None, max_keywords: int = 3
    ) -> list[str]:
        """Extract key terms using TF-IDF / term frequency analysis on single document text."""
        if not text or not text.strip():
            return []

        if text.startswith("[STATUS:"):
            return []

        from app.core.text_utils import (
            _is_high_entropy_token,
            contains_secrets,
            sanitize_secret_patterns,
        )

        text = sanitize_secret_patterns(text)
        if not text or not text.strip():
            return []

        all_stop_words = (
            (_DEFAULT_STOP_WORDS | stop_words) if stop_words else _DEFAULT_STOP_WORDS
        )

        # Clean and tokenize text
        words = re.findall(r"\b[a-zA-Z]{3,20}\b", text.lower())
        filtered_words = [
            w
            for w in words
            if w not in all_stop_words and not _is_high_entropy_token(w)
        ]

        if not filtered_words or max_keywords <= 0:
            return []

        counts = Counter(filtered_words)
        keywords: list[str] = []
        for word, _ in counts.most_common():
            if not _is_high_entropy_token(word) and not contains_secrets(word):
                keywords.append(word)
                if len(keywords) >= max_keywords:
                    break
        return keywords

    @staticmethod
    def extract_keyphrases_semantic(
        text: str,
        embedding_manager: object,
        stop_words: set[str] | None = None,
        max_keywords: int = 3,
    ) -> list[str] | None:
        """Rank candidate phrases and terms by semantic cosine similarity against the document embedding.

        Returns None when embeddings are unavailable or non-numeric so callers fall back to TF-IDF.
        """
        if (
            not text
            or not text.strip()
            or text.startswith("[STATUS:")
            or max_keywords <= 0
        ):
            return []
        if embedding_manager is None or not hasattr(
            embedding_manager, "generate_embedding"
        ):
            return None

        import numpy as np

        from app.core.text_utils import (
            _is_high_entropy_token,
            contains_secrets,
            sanitize_secret_patterns,
        )

        clean_text = sanitize_secret_patterns(text)
        if not clean_text or not clean_text.strip():
            return []

        all_stop_words = (
            (_DEFAULT_STOP_WORDS | stop_words) if stop_words else _DEFAULT_STOP_WORDS
        )

        raw_tokens = re.findall(r"\b[a-zA-Z]{3,20}\b", clean_text.lower())
        content_words = [
            w
            for w in raw_tokens
            if w not in all_stop_words
            and not _is_high_entropy_token(w)
            and not contains_secrets(w)
        ]
        if not content_words:
            return []

        gen_fn = getattr(embedding_manager, "generate_embedding")
        doc_vec = _coerce_numeric_vector(gen_fn(clean_text[:2000]))
        if doc_vec is None or getattr(embedding_manager, "is_degraded", False) is True:
            return None

        # Build candidate phrases: adjacent 2-word phrases plus unique content words
        candidates: list[tuple[str, list[str]]] = []
        seen_phrases: set[str] = set()

        for i in range(len(raw_tokens) - 1):
            w1, w2 = raw_tokens[i], raw_tokens[i + 1]
            if (
                w1 != w2
                and w1 not in all_stop_words
                and w2 not in all_stop_words
                and not _is_high_entropy_token(w1)
                and not _is_high_entropy_token(w2)
                and not contains_secrets(w1)
                and not contains_secrets(w2)
            ):
                phrase = f"{w1} {w2}"
                if phrase not in seen_phrases:
                    seen_phrases.add(phrase)
                    candidates.append((phrase, [w1, w2]))
                    if len(candidates) >= 16:
                        break

        for w in content_words:
            if w not in seen_phrases:
                seen_phrases.add(w)
                candidates.append((w, [w]))
                if len(candidates) >= 36:
                    break

        scored: list[tuple[float, list[str]]] = []
        for phrase, parts in candidates:
            cand_vec = _coerce_numeric_vector(gen_fn(phrase))
            if cand_vec is None or cand_vec.shape != doc_vec.shape:
                continue
            sim = float(np.dot(cand_vec, doc_vec))
            scored.append((sim, parts))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        selected_terms: list[str] = []
        used_roots: set[str] = set()
        for _, parts in scored:
            if len(selected_terms) + len(parts) > max_keywords:
                continue
            if any(p in used_roots for p in parts):
                continue
            selected_terms.extend(parts)
            used_roots.update(parts)
            if len(selected_terms) >= max_keywords:
                break

        if not selected_terms and scored:
            selected_terms = scored[0][1][:max_keywords]
        return selected_terms


def is_file_protected_or_locked(
    filepath: str,
    locked_files: dict | None = None,
    protected_paths: list | None = None,
    matched_policies: dict | None = None,
) -> bool:
    """Check if a file path is protected or manually locked."""
    # 1. Manual user lock check
    if locked_files and filepath in locked_files and locked_files[filepath]:
        return True

    # 2. Protected paths check
    if protected_paths:
        abs_fp = os.path.abspath(filepath)
        for p in protected_paths:
            if p:
                # A path that cannot be resolved protects nothing.
                with contextlib.suppress(OSError, TypeError, ValueError):
                    if is_subpath_or_equal(abs_fp, os.path.abspath(p)):
                        return True

    # 3. Policy match lock check
    if matched_policies and filepath in matched_policies:
        pol = matched_policies[filepath]
        if pol and pol.get("lock_path"):
            return True

    return False


class FileRenamerEngine:
    """Independent file renaming engine that evaluates and renames poorly-named files."""

    def __init__(self, runtime_settings=None, db=None, embedding_manager=None):
        self.settings = runtime_settings
        self.db = db
        self.embedding_manager = embedding_manager

    def _resolve_lazy_text(
        self,
        raw_text: str | None,
        file_path: str | None,
        base_dir: str | None = None,
    ) -> str:
        """Lazily read offline file text when contextual keywords are needed for bypassed files."""
        if raw_text and not raw_text.startswith("[STATUS:"):
            return raw_text
        if not file_path or not os.path.isfile(file_path):
            return ""
        try:
            from app.core.extractor import _read_preview_text_offline

            lazy_text, _ = _read_preview_text_offline(
                file_path,
                settings=self.settings,
                db=self.db,
                base_dir=base_dir,
            )
            if lazy_text and not lazy_text.startswith("[STATUS:"):
                return lazy_text
        except Exception as err:
            logger.debug(
                f"Lazy text extraction skipped for '{file_path}': {err}",
                exc_info=True,
            )
        return ""

    def _extract_contextual_keywords(
        self,
        text: str,
        stop_words: set[str] | None = None,
    ) -> tuple[str, bool]:
        """Extract sanitized keyword stem and semantic-model indicator from document text."""
        if not text or text.startswith("[STATUS:"):
            return "", False

        ai_consent = (
            getattr(self.settings, "AI_CONSENT_GRANTED", None)
            if self.settings
            else None
        )
        ai_assisted = (
            getattr(self.settings, "AI_ASSISTED_NAMING", False)
            if self.settings
            else False
        )

        use_ml = (
            ai_consent is True
            and bool(ai_assisted)
            and self.embedding_manager is not None
            and getattr(self.embedding_manager, "is_mock", True) is False
            and getattr(self.embedding_manager, "is_degraded", False) is not True
            and not bool(self.embedding_manager.is_reconstruction_active())
        )

        keywords: list[str] = []
        used_semantic = False
        if use_ml:
            try:
                semantic_terms = ContextExtractor.extract_keyphrases_semantic(
                    text, self.embedding_manager, stop_words, max_keywords=3
                )
                if semantic_terms is not None:
                    keywords = semantic_terms
                    used_semantic = bool(keywords)
                else:
                    keywords = ContextExtractor.extract_keywords_tfidf(
                        text, stop_words, max_keywords=3
                    )
            except Exception as e:
                logger.warning(
                    f"ML contextual generation failed: {e}. Falling back to TF-IDF.",
                    exc_info=True,
                )
                keywords = ContextExtractor.extract_keywords_tfidf(
                    text, stop_words, max_keywords=3
                )
        else:
            keywords = ContextExtractor.extract_keywords_tfidf(
                text, stop_words, max_keywords=3
            )

        if not keywords:
            return "", False
        safe_kw = sanitize_name("_".join(keywords))
        return safe_kw, (used_semantic and bool(safe_kw))

    def _generate_rename_proposal(
        self,
        original_filename: str,
        text: str,
        stop_words: set[str] | None = None,
        file_date: str | None = None,
        category: str = "",
        seq: int = 1,
        file_path: str | None = None,
        *,
        allow_lazy_read: bool = False,
        base_dir: str | None = None,
    ) -> tuple[str, str | None]:
        """Generate ``(proposed_filename, rename_source)`` honoring literal stems and consent."""
        from app.core.pattern_formatter import (
            PatternTokenFormatter,
            resolve_disk_file_date,
        )

        orig_basename = os.path.basename(original_filename)
        orig_stem, ext = os.path.splitext(orig_basename)
        safe_orig_full = sanitize_name(orig_basename)

        configured_template = (
            str(getattr(self.settings, "RENAME_PATTERN", "") or "").strip()
            if self.settings
            else ""
        )
        tokens_lower = {
            t.lower() for t in PatternTokenFormatter.parse_tokens(configured_template)
        }
        has_keywords_token = "keywords" in tokens_lower

        contextual_enabled = (
            True
            if self.settings is None
            else bool(
                getattr(self.settings, "CONTEXTUAL_RENAMING", False)
                or getattr(self.settings, "CLINICAL_SMART_RENAMING", False)
            )
        )
        poorly_named = HeuristicEvaluator.is_poorly_named(orig_basename)
        replace_stem_with_keywords = (
            contextual_enabled and (not has_keywords_token) and poorly_named
        )
        need_keywords = has_keywords_token or replace_stem_with_keywords

        if not need_keywords:
            if not configured_template:
                return safe_orig_full, None
            in_memory_date = (
                ContextExtractor.extract_document_date(text)
                if (contextual_enabled and text and not text.startswith("[STATUS:"))
                else None
            )
            effective_date = (
                in_memory_date
                or file_date
                or resolve_disk_file_date(file_path, base_dir=base_dir)
            )
            formatted_name = PatternTokenFormatter.format_pattern(
                configured_template,
                metadata={
                    "date": effective_date,
                    "category": category or "uncategorized",
                    "stem": orig_stem,
                    "original": orig_stem,
                    "keywords": "",
                    "extension": ext,
                    "base_dir": base_dir,
                },
                seq=seq,
                fallback_original=orig_basename,
                file_path=file_path,
            )
            if formatted_name and formatted_name != orig_basename:
                return formatted_name, "Template"
            return safe_orig_full, None

        effective_text = ""
        if text and not text.startswith("[STATUS:"):
            effective_text = text
        elif allow_lazy_read and file_path:
            effective_text = self._resolve_lazy_text(text, file_path, base_dir=base_dir)

        safe_kw, used_semantic = self._extract_contextual_keywords(
            effective_text, stop_words=stop_words
        )
        doc_date = (
            ContextExtractor.extract_document_date(effective_text)
            if effective_text
            else None
        )
        effective_date = (
            doc_date
            or file_date
            or resolve_disk_file_date(file_path, base_dir=base_dir)
        )
        kw_source = (
            "AI Keyphrases"
            if used_semantic
            else ("Contextual Keywords" if safe_kw else "Template")
        )

        if has_keywords_token:
            formatted_name = PatternTokenFormatter.format_pattern(
                configured_template,
                metadata={
                    "date": effective_date,
                    "category": category or "uncategorized",
                    "stem": orig_stem,
                    "original": orig_stem,
                    "keywords": safe_kw,
                    "extension": ext,
                    "base_dir": base_dir,
                },
                seq=seq,
                fallback_original=orig_basename,
                file_path=file_path,
            )
            if formatted_name and formatted_name != orig_basename:
                return formatted_name, kw_source
            return safe_orig_full, None

        # replace_stem_with_keywords is True (CONTEXTUAL_RENAMING=True, no {keywords}, poorly_named=True)
        if not safe_kw:
            if configured_template:
                formatted_fallback = PatternTokenFormatter.format_pattern(
                    configured_template,
                    metadata={
                        "date": effective_date,
                        "category": category or "uncategorized",
                        "stem": orig_stem,
                        "original": orig_stem,
                        "keywords": "",
                        "extension": ext,
                        "base_dir": base_dir,
                    },
                    seq=seq,
                    fallback_original=orig_basename,
                    file_path=file_path,
                )
                if formatted_fallback and formatted_fallback != orig_basename:
                    return formatted_fallback, "Template"
            return safe_orig_full, None

        active_template = configured_template
        if not active_template and used_semantic and effective_date:
            active_template = "{date}_{stem}"

        if active_template:
            formatted_name = PatternTokenFormatter.format_pattern(
                active_template,
                metadata={
                    "date": effective_date,
                    "category": category or "uncategorized",
                    "stem": safe_kw,
                    "original": safe_kw,
                    "keywords": safe_kw,
                    "extension": ext,
                    "base_dir": base_dir,
                },
                seq=seq,
                fallback_original=orig_basename,
                file_path=file_path,
            )
            if (
                formatted_name
                and os.path.splitext(formatted_name)[0].lower() != orig_stem.lower()
            ):
                return formatted_name, kw_source

        if safe_kw.lower() == orig_stem.lower():
            return safe_orig_full, None

        return f"{safe_kw}{ext}", kw_source

    def generate_contextual_name(
        self,
        original_filename: str,
        text: str,
        stop_words: set[str] | None = None,
        file_date: str | None = None,
        category: str = "",
        seq: int = 1,
        file_path: str | None = None,
    ) -> str:
        """Generate a context-aware descriptive filename for a file.

        Verifies AI consent before using machine learning models.
        Ranks candidate keyphrases semantically via ONNX embeddings when AI_ASSISTED_NAMING
        and AI_CONSENT_GRANTED are active, falling back to TF-IDF when offline or withheld,
        and applies date/rename templates when configured or when assisted naming resolves a date.
        """
        proposed_name, _ = self._generate_rename_proposal(
            original_filename=original_filename,
            text=text,
            stop_words=stop_words,
            file_date=file_date,
            category=category,
            seq=seq,
            file_path=file_path,
            allow_lazy_read=bool(file_path),
        )
        return proposed_name

    @staticmethod
    def _is_same_disk_path(path_a: str, path_b: str | None) -> bool:
        """Return True when ``path_a`` and ``path_b`` refer to the exact same filesystem entry."""
        if not path_b:
            return False
        if os.path.lexists(path_a) and os.path.lexists(path_b):
            with contextlib.suppress(OSError):
                if os.path.samefile(path_a, path_b):
                    return True
        return os.path.normcase(os.path.abspath(path_a)) == os.path.normcase(
            os.path.abspath(path_b)
        )

    @classmethod
    def _disambiguate_folder_target(
        cls,
        proposed_fn: str,
        occupied_lower: set[str],
        dest_folder_disk: str | None,
        src_abs_path: str | None,
    ) -> str:
        """Disambiguate ``proposed_fn`` against sibling plan entries and destination disk paths."""
        stem, ext = os.path.splitext(proposed_fn)
        candidate = proposed_fn
        counter = 1
        while True:
            in_plan = candidate.lower() in occupied_lower
            on_disk = False
            if dest_folder_disk:
                cand_disk = os.path.normpath(os.path.join(dest_folder_disk, candidate))
                if os.path.lexists(cand_disk) and not cls._is_same_disk_path(
                    cand_disk, src_abs_path
                ):
                    on_disk = True
            if not in_plan and not on_disk:
                return candidate
            candidate = f"{stem}_{counter}{ext}"
            counter += 1

    def process_sorting_plan(
        self,
        plan: dict,
        documents_map: dict[str, str],
        base_dir: str,
        locked_files: dict | None = None,
        stop_words: set[str] | None = None,
    ) -> dict:
        """Traverse and update sorting plan with target_filename for eligible files.

        Applies standalone ``RENAME_PATTERN`` templates or contextual keyword renames,
        lazy-loads bypassed document text only when contextual keywords are needed,
        scopes ``{seq}`` per destination folder, and disambiguates collisions against
        both sibling plan entries and destination disk paths.
        Purges temporary context buffers immediately after execution.
        """
        from app.core.path_utils import _is_absolute_path, safe_relpath
        from app.core.pattern_formatter import resolve_disk_file_date

        protected_paths = (
            getattr(self.settings, "PROTECTED_PATHS", []) if self.settings else []
        )
        configured_template = (
            str(getattr(self.settings, "RENAME_PATTERN", "") or "").strip()
            if self.settings
            else ""
        )
        contextual_enabled = (
            True
            if self.settings is None
            else bool(
                getattr(self.settings, "CONTEXTUAL_RENAMING", False)
                or getattr(self.settings, "CLINICAL_SMART_RENAMING", False)
            )
        )

        doc_by_norm: dict[str, str] = {}
        doc_by_base_counts: Counter[str] = Counter()
        doc_by_base: dict[str, str] = {}

        for raw_fp, raw_txt in (documents_map or {}).items():
            norm_fp = os.path.normpath(str(raw_fp)).replace("\\", "/")
            doc_by_norm[norm_fp] = raw_txt
            if "::" in str(raw_fp):
                _, after_sep = str(raw_fp).split("::", 1)
                norm_after = os.path.normpath(after_sep).replace("\\", "/")
                doc_by_norm.setdefault(norm_after, raw_txt)
            if base_dir and os.path.isabs(str(raw_fp)):
                with contextlib.suppress(Exception):
                    rel_fp = safe_relpath(str(raw_fp), base_dir).replace("\\", "/")
                    if not rel_fp.startswith(".."):
                        doc_by_norm.setdefault(
                            os.path.normpath(rel_fp).replace("\\", "/"), raw_txt
                        )
            base_low = os.path.basename(norm_fp).lower()
            doc_by_base_counts[base_low] += 1
            doc_by_base[base_low] = raw_txt

        def _lookup_doc_text(
            key_str: str,
            src_rel_str: str,
            src_root_str: str,
            abs_cand_str: str,
        ) -> str:
            candidates = [
                f"{src_root_str}::{src_rel_str}" if src_root_str else "",
                src_rel_str,
                key_str,
                abs_cand_str,
            ]
            for cand in candidates:
                if not cand:
                    continue
                norm_c = os.path.normpath(cand).replace("\\", "/")
                if norm_c in doc_by_norm:
                    return doc_by_norm[norm_c]
            base_low = os.path.basename(src_rel_str or key_str).lower()
            if doc_by_base_counts.get(base_low, 0) == 1:
                return doc_by_base.get(base_low, "")
            return ""

        def _is_file_leaf(val: object) -> bool:
            return val is None or (
                isinstance(val, dict)
                and (
                    val.get("__type__") == "file"
                    or "relative_source" in val
                    or "source_path" in val
                    or "status" in val
                    or "routed_by" in val
                )
            )

        def _process_folder(curr_node: dict, folder_chain: str = "") -> None:
            if not isinstance(curr_node, dict):
                return

            leaf_cat = (
                folder_chain.replace("\\", "/").strip("/").split("/")[-1]
                if folder_chain
                else "uncategorized"
            )
            dest_folder_disk: str | None = None
            if folder_chain and _is_absolute_path(folder_chain):
                dest_folder_disk = folder_chain
            elif base_dir:
                dest_folder_disk = (
                    os.path.join(base_dir, folder_chain) if folder_chain else base_dir
                )

            occupied_lower: set[str] = set()
            pending_proposals: list[tuple[str, object, str, str, str | None]] = []
            folder_seq = 0

            for k, v in list(curr_node.items()):
                if not _is_file_leaf(v):
                    if isinstance(v, dict):
                        next_chain = f"{folder_chain}/{k}" if folder_chain else k
                        _process_folder(v, next_chain)
                    continue

                v_dict = v if isinstance(v, dict) else {}
                src_root = str(v_dict.get("source_root") or base_dir or "")
                src_rel = str(v_dict.get("source_path") or k)
                if "::" in src_rel:
                    parsed_root, src_rel = src_rel.split("::", 1)
                    if not v_dict.get("source_root") and parsed_root:
                        src_root = parsed_root

                abs_candidate = (
                    os.path.join(src_root, src_rel)
                    if src_root and not os.path.isabs(src_rel)
                    else src_rel
                )
                orig_basename = os.path.basename(src_rel or k)
                existing_target = v_dict.get("target_filename")

                is_locked_or_protected = (
                    is_file_protected_or_locked(k, locked_files, protected_paths)
                    or is_file_protected_or_locked(
                        src_rel, locked_files, protected_paths
                    )
                    or (
                        bool(abs_candidate)
                        and is_file_protected_or_locked(
                            abs_candidate, locked_files, protected_paths
                        )
                    )
                )
                is_ineligible = (
                    is_locked_or_protected
                    or bool(v_dict.get("is_excluded"))
                    or bool(v_dict.get("is_locked") or v_dict.get("confirmed"))
                    or v_dict.get("duplicate_action") in ("stage", "trash", "skip")
                    or v_dict.get("policy_action")
                    in ("trash", "quarantine", "unarchive")
                    or (bool(existing_target) and str(existing_target) != orig_basename)
                )
                if is_ineligible:
                    eff_existing = str(existing_target or os.path.basename(k))
                    occupied_lower.add(eff_existing.lower())
                    continue

                if not (
                    bool(configured_template)
                    or (
                        contextual_enabled
                        and HeuristicEvaluator.is_poorly_named(orig_basename)
                    )
                ):
                    eff_existing = str(existing_target or os.path.basename(k))
                    occupied_lower.add(eff_existing.lower())
                    continue

                folder_seq += 1
                doc_text = _lookup_doc_text(k, src_rel, src_root, abs_candidate)
                f_date = (
                    str(v_dict.get("file_date"))
                    if v_dict.get("file_date")
                    else resolve_disk_file_date(abs_candidate, base_dir=src_root)
                )
                proposed_fn, rename_source = self._generate_rename_proposal(
                    original_filename=orig_basename,
                    text=doc_text,
                    stop_words=stop_words,
                    file_date=f_date,
                    category=leaf_cat,
                    seq=folder_seq,
                    file_path=abs_candidate,
                    allow_lazy_read=True,
                    base_dir=src_root or base_dir,
                )
                if proposed_fn and proposed_fn != orig_basename:
                    pending_proposals.append(
                        (k, v, abs_candidate, proposed_fn, rename_source)
                    )
                else:
                    occupied_lower.add(os.path.basename(k).lower())

            for k, v, abs_candidate, proposed_fn, rename_source in pending_proposals:
                final_fn = self._disambiguate_folder_target(
                    proposed_fn=proposed_fn,
                    occupied_lower=occupied_lower,
                    dest_folder_disk=dest_folder_disk,
                    src_abs_path=abs_candidate,
                )
                occupied_lower.add(final_fn.lower())
                if v is None:
                    node_payload: dict[str, object] = {
                        "__type__": "file",
                        "relative_source": k,
                        "target_filename": final_fn,
                    }
                    if rename_source:
                        node_payload["rename_source"] = rename_source
                    curr_node[k] = node_payload
                elif isinstance(v, dict):
                    v["target_filename"] = final_fn
                    if rename_source:
                        v["rename_source"] = rename_source

        try:
            _process_folder(plan)
            return plan
        finally:
            doc_by_norm.clear()
            doc_by_base_counts.clear()
            doc_by_base.clear()
            gc.collect()

    def _inject_renames_into_plan(
        self, plan: dict, renames_map: dict[str, str]
    ) -> None:
        """Inject target_filename attributes into sorting plan file leaf nodes."""
        if not isinstance(plan, dict) or not renames_map:
            return

        norm_map = {}
        base_map = {}
        for path_key, target_name in renames_map.items():
            norm_key = os.path.normpath(path_key).replace("\\", "/")
            norm_map[norm_key] = target_name
            base_map[os.path.basename(path_key).lower()] = target_name

        def _traverse(curr_node):
            if not isinstance(curr_node, dict):
                return

            for k, v in list(curr_node.items()):
                norm_k = os.path.normpath(k).replace("\\", "/")
                base_k = os.path.basename(k).lower()

                target_fn = norm_map.get(norm_k)
                if not target_fn and isinstance(v, dict) and "relative_source" in v:
                    norm_rel = os.path.normpath(v["relative_source"]).replace("\\", "/")
                    target_fn = norm_map.get(norm_rel)

                if not target_fn:
                    target_fn = base_map.get(base_k)

                is_file = v is None or (
                    isinstance(v, dict)
                    and (
                        v.get("__type__") == "file"
                        or "relative_source" in v
                        or "status" in v
                        or "routed_by" in v
                    )
                )

                if target_fn and is_file:
                    if v is None:
                        curr_node[k] = {
                            "__type__": "file",
                            "relative_source": k,
                            "target_filename": target_fn,
                        }
                    elif isinstance(v, dict):
                        if (
                            not v.get("is_excluded")
                            and v.get("duplicate_action")
                            not in ("stage", "trash", "skip")
                            and v.get("policy_action")
                            not in ("trash", "quarantine", "unarchive")
                        ):
                            v["target_filename"] = target_fn
                elif isinstance(v, dict) and not is_file:
                    _traverse(v)

        _traverse(plan)
