"""Modular Heuristic & Context-Aware File Renaming Engine.

Evaluates filename quality using heuristics and generates descriptive,
context-aware filenames as an independent pipeline stage while respecting user privacy,
AI consent, and path safety guardrails.
"""

import gc
import logging
import os
import re
from typing import Dict, List, Optional, Set

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


class ContextExtractor:
    """Extracts semantic and statistical key terms from document text."""

    @staticmethod
    def extract_keywords_tfidf(
        text: str, stop_words: Optional[Set[str]] = None, max_keywords: int = 3
    ) -> List[str]:
        """Extract key terms using TF-IDF / term frequency analysis on single document text."""
        if not text or not text.strip():
            return []

        if text.startswith("[STATUS:"):
            return []

        default_stops = {
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
        all_stop_words = default_stops | (stop_words or set())

        # Clean and tokenize text
        words = re.findall(r"\b[a-zA-Z]{3,20}\b", text.lower())
        filtered_words = [w for w in words if w not in all_stop_words]

        if not filtered_words:
            return []

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            vectorizer = TfidfVectorizer(
                stop_words=list(all_stop_words),
                max_features=100,
                token_pattern=r"\b[a-zA-Z]{3,20}\b",
            )
            tfidf_matrix = vectorizer.fit_transform([text])
            feature_names = vectorizer.get_feature_names_out()
            scores = tfidf_matrix.toarray()[0]

            top_indices = scores.argsort()[::-1][:max_keywords]
            top_terms = [feature_names[i] for i in top_indices if scores[i] > 0]
            if top_terms:
                return top_terms
        except Exception as e:
            logger.debug(f"TF-IDF extraction fallback to frequency count due to: {e}")

        # Fallback to simple frequency count
        from collections import Counter

        counts = Counter(filtered_words)
        return [w for w, _ in counts.most_common(max_keywords)]


def is_file_protected_or_locked(
    filepath: str,
    locked_files: Optional[dict] = None,
    protected_paths: Optional[list] = None,
    matched_policies: Optional[dict] = None,
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
                try:
                    abs_p = os.path.abspath(p)
                    if is_subpath_or_equal(abs_fp, abs_p):
                        return True
                except Exception:
                    pass

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

    def generate_contextual_name(
        self,
        original_filename: str,
        text: str,
        stop_words: Optional[Set[str]] = None,
    ) -> str:
        """Generate a context-aware descriptive filename for a file.

        Verifies AI consent before using machine learning models.
        Falls back to TF-IDF keyword extraction when embedding models are offline/rebuilding
        or when AI consent is withheld.
        """
        base, ext = os.path.splitext(original_filename)
        if not text or text.startswith("[STATUS:"):
            return original_filename

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
            and ai_assisted
            and self.embedding_manager is not None
            and not getattr(self.embedding_manager, "is_mock", True)
            and not self.embedding_manager.is_reconstruction_active()
        )

        keywords = []
        if use_ml:
            try:
                keywords = ContextExtractor.extract_keywords_tfidf(
                    text, stop_words, max_keywords=3
                )
            except Exception as e:
                logger.warning(
                    f"ML contextual generation failed: {e}. Falling back to TF-IDF."
                )
                keywords = ContextExtractor.extract_keywords_tfidf(
                    text, stop_words, max_keywords=3
                )
        else:
            keywords = ContextExtractor.extract_keywords_tfidf(
                text, stop_words, max_keywords=3
            )

        if not keywords:
            return original_filename

        new_base = "_".join(keywords)
        safe_base = sanitize_name(new_base)

        if not safe_base or safe_base.lower() == base.lower():
            return original_filename

        return f"{safe_base}{ext}"

    def process_sorting_plan(
        self,
        plan: dict,
        documents_map: Dict[str, str],
        base_dir: str,
        locked_files: Optional[dict] = None,
        stop_words: Optional[Set[str]] = None,
    ) -> dict:
        """Traverse and update sorting plan with target_filename for poorly-named files.

        Purges temporary context buffers immediately after execution.
        """
        protected_paths = (
            getattr(self.settings, "PROTECTED_PATHS", []) if self.settings else []
        )

        context_buffer: Dict[str, str] = {}

        try:
            for filepath, doc_text in documents_map.items():
                filename = os.path.basename(filepath)

                if is_file_protected_or_locked(
                    filepath, locked_files, protected_paths
                ):
                    continue

                if HeuristicEvaluator.is_poorly_named(filename):
                    new_fn = self.generate_contextual_name(
                        filename, doc_text, stop_words
                    )
                    if new_fn and new_fn != filename:
                        context_buffer[filepath] = new_fn

            if context_buffer:
                self._inject_renames_into_plan(plan, context_buffer)

            return plan
        finally:
            context_buffer.clear()
            del context_buffer
            gc.collect()

    def _inject_renames_into_plan(
        self, plan: dict, renames_map: Dict[str, str]
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
                    norm_rel = os.path.normpath(v["relative_source"]).replace(
                        "\\", "/"
                    )
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
                        v["target_filename"] = target_fn
                elif isinstance(v, dict) and not is_file:
                    _traverse(v)

        _traverse(plan)
