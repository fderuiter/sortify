"""Metadata pass for pre-evaluating files against rules before text extraction."""

import contextlib
import logging
import mimetypes
import os
import re
from collections.abc import Sequence
from typing import Any

from app.core.extractor import get_file_hash
from app.core.extractor_strategies import registry
from app.core.policy_engine import PolicyEngine, is_pre_learned_policy
from app.core.progress import emit_progress
from app.core.rule_scopes import resolve_scoped_settings

_LEARNED_SEP_RE = re.compile(r"[_\-\s.]+")
_FILENAME_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_COUNTER_TOKEN_RE = re.compile(
    r"^(?:v|ver|rev|p|pg|pt|part|seq|num|no|copy)\d+$|^q[1-4]$"
)
_HEX_HASH_TOKEN_RE = re.compile(r"^[0-9a-f]{8,}$")
_FILE_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")

GENERIC_FILENAME_STOPWORDS: frozenset[str] = frozenset(
    {
        "scan",
        "scanned",
        "scanner",
        "scans",
        "img",
        "image",
        "images",
        "dsc",
        "dscn",
        "dcim",
        "screenshot",
        "screenshots",
        "screen",
        "photo",
        "photos",
        "pic",
        "pics",
        "picture",
        "pictures",
        "doc",
        "docs",
        "document",
        "documents",
        "file",
        "files",
        "untitled",
        "new",
        "copy",
        "final",
        "draft",
        "temp",
        "tmp",
        "download",
        "downloaded",
        "export",
        "exported",
        "output",
        "page",
        "attachment",
    }
)


def _extract_filename_stem_and_base(raw_name: str) -> tuple[str, str]:
    """Return lowercase ``(stem, basename)`` stripped of ``::`` root prefix and path dirs."""
    clean_raw = raw_name.split("::", 1)[1] if "::" in raw_name else raw_name
    base = os.path.basename(clean_raw.replace("\\", "/")).lower()
    stem, ext = os.path.splitext(base)
    if stem and _FILE_EXT_RE.match(ext):
        return stem, base
    return base, base


def tokenize_filename_for_rule(raw_name: str) -> tuple[list[str], list[str]]:
    """Extract generalized filename tokens and any stripped generic stopwords.

    Strips dates, numeric counters, version/quarter markers, hex hashes, and
    generic stopwords such as ``scan`` or ``img``.
    """
    if not raw_name or not str(raw_name).strip():
        return [], []
    stem, _ = _extract_filename_stem_and_base(str(raw_name))
    raw_tokens = [tok for tok in _FILENAME_SPLIT_RE.split(stem) if tok]
    meaningful: list[str] = []
    seen_meaningful: set[str] = set()
    stopwords_found: list[str] = []
    seen_stopwords: set[str] = set()

    for tok in raw_tokens:
        if tok.isdigit() or len(tok) < 2:
            continue
        if _COUNTER_TOKEN_RE.match(tok):
            continue
        if _HEX_HASH_TOKEN_RE.match(tok) and any(ch.isdigit() for ch in tok):
            continue
        if tok in GENERIC_FILENAME_STOPWORDS:
            if tok not in seen_stopwords:
                seen_stopwords.add(tok)
                stopwords_found.append(tok)
            continue
        if tok not in seen_meaningful:
            seen_meaningful.add(tok)
            meaningful.append(tok)

    return meaningful, stopwords_found


def derive_learned_pattern_for_files(
    file_names: Sequence[str],
) -> tuple[str, list[str], bool]:
    """Derive a generalized filename rule pattern across one or more filenames.

    Returns ``(pattern, stripped_stopwords, is_generic_only)`` where
    ``is_generic_only`` is True when tokenization produced no meaningful tokens
    because only generic stopwords/counters were present (e.g. ``scan_001.pdf``).
    """
    valid_names = [str(fn) for fn in file_names if fn and str(fn).strip()]
    if not valid_names:
        return "", [], False

    per_file_tokens: list[list[str]] = []
    all_stopwords: list[str] = []
    seen_stop: set[str] = set()

    for fn in valid_names:
        toks, stops = tokenize_filename_for_rule(fn)
        per_file_tokens.append(toks)
        for st in stops:
            if st not in seen_stop:
                seen_stop.add(st)
                all_stopwords.append(st)

    if len(per_file_tokens) == 1:
        shared = per_file_tokens[0]
    else:
        common_sets = [set(toks) for toks in per_file_tokens[1:]]
        shared = [
            tok
            for tok in per_file_tokens[0]
            if all(tok in other_set for other_set in common_sets)
        ]

    pattern = " ".join(shared)
    is_generic_only = not pattern and bool(all_stopwords)
    return pattern, all_stopwords, is_generic_only


def matches_learned_rule_pattern(
    pattern: str,
    text_to_search: str,
    norm_search: str | None = None,
) -> bool:
    """Return True if ``pattern`` matches the normalized filename in ``text_to_search``."""
    kw_low = str(pattern).lower().strip()
    if not kw_low or not text_to_search:
        return False
    stem_low, base_low = _extract_filename_stem_and_base(str(text_to_search))
    target_low = base_low if "." in kw_low else stem_low
    if not target_low:
        return False
    if kw_low in target_low:
        return True
    norm_kw = _LEARNED_SEP_RE.sub(" ", kw_low).strip()
    if len(norm_kw) < 3:
        return False
    effective_norm = (
        norm_search
        if (norm_search is not None and target_low == str(text_to_search).lower())
        else _LEARNED_SEP_RE.sub(" ", target_low).strip()
    )
    if norm_kw in effective_norm:
        return True
    kw_tokens = [tok for tok in norm_kw.split() if tok]
    if len(kw_tokens) >= 2:
        return all(tok in effective_norm for tok in kw_tokens)
    return False


DEFAULT_EXTENSION_CATEGORIES = {
    # Archives / Compressed
    ".zip": "Archives",
    ".tar": "Archives",
    ".gz": "Archives",
    ".tgz": "Archives",
    ".7z": "Archives",
    ".rar": "Archives",
    ".bz2": "Archives",
    ".xz": "Archives",
    ".iso": "Archives",
    ".dmg": "Archives",
    # Executables / Binaries / Installers
    ".exe": "Executables",
    ".msi": "Executables",
    ".apk": "Executables",
    ".deb": "Executables",
    ".rpm": "Executables",
    ".appimage": "Executables",
    ".bin": "Executables",
    # Design / CAD / Graphics
    ".cad": "Design",
    ".dwg": "Design",
    ".dxf": "Design",
    ".psd": "Design",
    ".ai": "Design",
    ".blend": "Design",
    # Media
    ".mp4": "Media",
    ".mkv": "Media",
    ".avi": "Media",
    ".mov": "Media",
    ".flv": "Media",
    ".wmv": "Media",
}


def get_extension_category_target(item: str, settings) -> str | None:
    """Evaluate extension category mapping rules or MIME fallback categories for a file."""
    ext = os.path.splitext(item)[1].lower()
    if not ext:
        return None

    # 1. User-configured EXTENSION_CATEGORIES setting
    ext_categories = getattr(settings, "EXTENSION_CATEGORIES", {})
    if isinstance(ext_categories, dict):
        if ext in ext_categories and isinstance(ext_categories[ext], str):
            return ext_categories[ext]
        ext_no_dot = ext.lstrip(".")
        if ext_no_dot in ext_categories and isinstance(ext_categories[ext_no_dot], str):
            return ext_categories[ext_no_dot]
        for cat_folder, ext_list in ext_categories.items():
            if isinstance(cat_folder, str) and isinstance(ext_list, (list, tuple, set)):
                for raw_ext in ext_list:
                    clean_ext = str(raw_ext).strip().lower()
                    if clean_ext in (ext, ext_no_dot):
                        return cat_folder

    # 2. Built-in default extension mapping
    if ext in DEFAULT_EXTENSION_CATEGORIES:
        return DEFAULT_EXTENSION_CATEGORIES[ext]

    # 3. Fallback MIME-type evaluation
    mime_type, _ = mimetypes.guess_type(item)
    if mime_type:
        main_type = mime_type.split("/")[0].lower()
        if main_type == "image":
            return "Images"
        elif main_type == "audio":
            return "Audio"
        elif main_type == "video":
            return "Media"
        elif main_type == "application":
            if any(
                k in mime_type
                for k in (
                    "zip",
                    "tar",
                    "compressed",
                    "archive",
                    "7z",
                    "rar",
                    "iso",
                    "diskimage",
                )
            ):
                return "Archives"
            if any(
                k in mime_type
                for k in ("executable", "x-msdownload", "x-msi", "x-deb", "x-rpm")
            ):
                return "Executables"

    return None


def evaluate_static_file_routing(
    file_path: str,
    settings: object,
    *,
    filename_only: str | None = None,
    check_extension_categories: bool = True,
    sorted_policies: list[dict] | None = None,
) -> tuple[str | None, bool, bool]:
    """Evaluate static policies, keyword/learned rules, and extension categories.

    Parameters
    ----------
    file_path : str
        File path to evaluate against static rules.
    settings : object
        Application settings containing ``POLICIES``, ``KEYWORD_RULES``,
        ``LEARNED_RULES``, and ``EXTENSION_CATEGORIES``.
    filename_only : str | None, optional
        Pre-computed lowercase basename of ``file_path``.
    check_extension_categories : bool, optional
        Whether to evaluate ``get_extension_category_target`` when no policy
        or keyword/learned rule matches.
    sorted_policies : list[dict] | None, optional
        Pre-sorted policy definitions to avoid re-sorting on every file.

    Returns
    -------
    tuple[str | None, bool, bool]
        ``(matched_target, matched_action, halt_evaluation)`` describing any
        matched destination folder, whether a valid policy action matched,
        and whether a halting rule stopped downstream evaluation.
    """
    fn_lower = (
        filename_only
        if filename_only is not None
        else os.path.basename(file_path).lower()
    )
    if sorted_policies is None:
        raw_policies = getattr(settings, "POLICIES", []) if settings else []
        valid_policies = (
            [
                p
                for p in (raw_policies or [])
                if isinstance(p, dict) or hasattr(p, "model_dump")
            ]
            if isinstance(raw_policies, (list, tuple))
            else []
        )
        active_policies = PolicyEngine.sort_policies(valid_policies)
    else:
        active_policies = sorted_policies

    pre_learned_policies = [p for p in active_policies if is_pre_learned_policy(p)]
    post_learned_policies = [p for p in active_policies if not is_pre_learned_policy(p)]
    abs_fp = file_path if file_path and os.path.isfile(file_path) else None

    def _apply_matched_policy(pol: Any) -> tuple[str | None, bool]:
        m_target: str | None = None
        m_action = False
        if pol is not None:
            raw_target = pol.get("target_path")
            if raw_target:
                m_target = str(raw_target)
            raw_action = pol.get("action")
            if raw_action:
                m_action = PolicyEngine.validate_policy_action(str(raw_action))
        return m_target, m_action

    eval_out = PolicyEngine.evaluate_policies(
        file_path,
        "",
        "[STATUS:STATIC]",
        pre_learned_policies,
        return_halting=True,
        is_sorted=True,
        abs_file_path=abs_fp,
    )
    matched_policy, halt_evaluation = (
        eval_out if isinstance(eval_out, tuple) else (eval_out, False)
    )
    matched_target, matched_action = _apply_matched_policy(matched_policy)

    if not halt_evaluation and not matched_target and not matched_action:
        learned_rules = getattr(settings, "LEARNED_RULES", {}) if settings else {}
        if isinstance(learned_rules, dict) and learned_rules:
            for keyword, target_folder in learned_rules.items():
                if not target_folder:
                    continue
                if matches_learned_rule_pattern(str(keyword), fn_lower):
                    matched_target = str(target_folder)
                    break

    if (
        not halt_evaluation
        and not matched_target
        and not matched_action
        and post_learned_policies
    ):
        post_out = PolicyEngine.evaluate_policies(
            file_path,
            "",
            "[STATUS:STATIC]",
            post_learned_policies,
            return_halting=True,
            is_sorted=True,
            abs_file_path=abs_fp,
        )
        post_policy, post_halt = (
            post_out if isinstance(post_out, tuple) else (post_out, False)
        )
        matched_target, matched_action = _apply_matched_policy(post_policy)
        if post_halt:
            halt_evaluation = True

    if not halt_evaluation and not matched_target and not matched_action:
        kw_rules = getattr(settings, "KEYWORD_RULES", {}) if settings else {}
        if isinstance(kw_rules, dict):
            for keyword, target_folder in kw_rules.items():
                kw_str = str(keyword)
                if kw_str.strip() and kw_str.lower() in fn_lower and target_folder:
                    matched_target = str(target_folder)
                    break

    if (
        not halt_evaluation
        and not matched_target
        and not matched_action
        and check_extension_categories
    ):
        matched_target = get_extension_category_target(file_path, settings)

    return matched_target, matched_action, halt_evaluation


class MetadataPass:
    """Component to execute static rule matching logic prior to heavy ingestion."""

    @staticmethod
    def run(
        base_dir: str,
        items_to_sort: list,
        settings,
        db,
        callback=None,
        cancel_check=None,
        progress_callback=None,
        source_dirs=None,
    ) -> list:
        """Run an initial sequential metadata pass to bypass text extraction for matching files."""
        if not base_dir and not source_dirs:
            return []

        from app.core.scanner import parse_item_identifier

        effective_callback = progress_callback or callback

        auto_quarantine_ratings = [
            r for r in (getattr(settings, "AUTO_QUARANTINE_RATINGS", []) or []) if r
        ]

        def _policy_field(pol: object, key: str, default: object = "") -> object:
            return (
                pol.get(key, default)
                if isinstance(pol, dict)
                else getattr(pol, key, default)
            )

        scope_bundle_cache: dict[str, tuple[Any, list[Any], bool]] = {}

        def _get_scope_bundle(root_dir: str) -> tuple[Any, list[Any], bool]:
            cache_key = root_dir or ""
            cached = scope_bundle_cache.get(cache_key)
            if cached is not None:
                return cached
            scoped_cfg = (
                resolve_scoped_settings(settings, cache_key, fallback_to_global=True)
                if cache_key
                else settings
            )
            raw_policies = getattr(scoped_cfg, "POLICIES", []) if scoped_cfg else []
            valid_pols = (
                [
                    p
                    for p in (raw_policies or [])
                    if isinstance(p, dict) or hasattr(p, "model_dump")
                ]
                if isinstance(raw_policies, (list, tuple))
                else []
            )
            sorted_pols = PolicyEngine.sort_policies(valid_pols)
            has_compliance = bool(auto_quarantine_ratings) or any(
                bool(_policy_field(p, "enabled", True))
                and (
                    PolicyEngine.rule_inspects_content(p)
                    or str(_policy_field(p, "type", "") or "").lower()
                    in ("compliance", "action")
                    or str(_policy_field(p, "action", "") or "").lower()
                    in ("quarantine", "redact", "archive", "retain")
                    or str(_policy_field(p, "expression", "") or "").strip().lower()
                    in ("secret", "credential")
                )
                for p in sorted_pols
            )
            bundle = (scoped_cfg, sorted_pols, has_compliance)
            scope_bundle_cache[cache_key] = bundle
            return bundle

        if db and hasattr(db, "worker"):
            from app.core.db import DBWorker

            if isinstance(getattr(db, "worker", None), DBWorker):
                with contextlib.suppress(RuntimeError, OSError):
                    db.worker.execute_write(lambda: None)

        docs = db.get_all_documents(base_dir, source_dirs=source_dirs) if db else []
        doc_ratings = (
            db.get_all_document_ratings(base_dir)
            if db and hasattr(db, "get_all_document_ratings")
            else {}
        )
        if not isinstance(doc_ratings, dict):
            doc_ratings = {}
        hash_to_target = {}
        rejected_by_doc: dict[str, set[str]] = {}
        for d in docs:
            if len(d) >= 4:
                fp_key = str(d[0]).replace("\\", "/")
                raw_t = str(d[3]) if d[3] else ""
                is_neg = (
                    str(doc_ratings.get(fp_key, doc_ratings.get(d[0], "")))
                    .lower()
                    .startswith("negative")
                )
                if raw_t.startswith("!"):
                    rej = raw_t[1:].strip().replace("\\", "/").strip("/").lower()
                    if rej:
                        rejected_by_doc.setdefault(fp_key, set()).add(rej)
                    continue
                if is_neg:
                    if raw_t:
                        rej = raw_t.strip().replace("\\", "/").strip("/").lower()
                        if rej:
                            rejected_by_doc.setdefault(fp_key, set()).add(rej)
                    continue
                if d[2] and d[3]:
                    hash_to_target[d[2]] = d[3]

        bypassed_files = []
        docs_to_upsert = []

        total_items = len(items_to_sort)
        for idx, item in enumerate(items_to_sort):
            if cancel_check and cancel_check():
                break

            progress_ratio = (idx + 1) / total_items if total_items > 0 else 1.0

            src_root, rel_p = parse_item_identifier(item, default_base_dir=base_dir)
            owning_root = src_root or base_dir or ""
            scoped_settings, sorted_policies, has_content_compliance_policy = (
                _get_scope_bundle(owning_root)
            )
            item_path = os.path.join(src_root, rel_p) if src_root else item
            try:
                if not os.path.isfile(item_path):
                    continue
                # Ensure the file is readable
                with open(item_path, "rb"):
                    pass
            except (OSError, PermissionError, FileNotFoundError) as e:
                logging.warning(
                    f"Could not read/process file {item_path} due to access errors: {e}"
                )
                continue

            file_hash = get_file_hash(item_path)
            _, ext = os.path.splitext(rel_p)
            is_supported = registry.is_supported(ext)

            matched_target = None
            matched_policy_action = ""
            matched_from_policy = False
            unmatched_higher_content_policy = False
            if file_hash in hash_to_target:
                matched_target = hash_to_target[file_hash]
                matched_from_policy = True
            else:
                fn_only_low = os.path.basename(rel_p).lower()
                pre_pols = [p for p in sorted_policies if is_pre_learned_policy(p)]
                post_pols = [p for p in sorted_policies if not is_pre_learned_policy(p)]
                pre_halted = False
                for pol_cand in pre_pols:
                    if not _policy_field(pol_cand, "enabled", True):
                        continue
                    p_type = str(_policy_field(pol_cand, "type", "") or "").lower()
                    if PolicyEngine.match_policy(
                        pol_cand,
                        item_path,
                        "",
                        "[STATUS:STATIC]",
                        abs_file_path=item_path,
                    ):
                        matched_from_policy = True
                        matched_policy_action = str(
                            _policy_field(pol_cand, "action", "") or ""
                        ).lower()
                        break
                    if p_type in (
                        "keyword",
                        "action",
                        "compliance",
                        "secret",
                        "credential",
                        "",
                    ) or PolicyEngine.rule_inspects_content(pol_cand):
                        unmatched_higher_content_policy = True
                    if _policy_field(pol_cand, "halting", False):
                        pre_halted = True
                        break

                matched_from_learned = False
                if not matched_from_policy and not pre_halted:
                    learned_cfg = (
                        getattr(scoped_settings, "LEARNED_RULES", {})
                        if scoped_settings
                        else {}
                    )
                    if isinstance(learned_cfg, dict) and learned_cfg:
                        norm_rel_chk = rel_p.replace("\\", "/")
                        rej_chk = rejected_by_doc.get(norm_rel_chk, set())
                        for l_kw, l_target in learned_cfg.items():
                            if not l_target:
                                continue
                            lt_clean = (
                                str(l_target).replace("\\", "/").strip("/").lower()
                            )
                            if lt_clean in rej_chk or (
                                lt_clean.split("/")[0] in rej_chk
                            ):
                                continue
                            if matches_learned_rule_pattern(str(l_kw), fn_only_low):
                                matched_from_learned = True
                                break

                if (
                    not matched_from_policy
                    and not pre_halted
                    and not matched_from_learned
                ):
                    for pol_cand in post_pols:
                        if not _policy_field(pol_cand, "enabled", True):
                            continue
                        p_type = str(_policy_field(pol_cand, "type", "") or "").lower()
                        if PolicyEngine.match_policy(
                            pol_cand,
                            item_path,
                            "",
                            "[STATUS:STATIC]",
                            abs_file_path=item_path,
                        ):
                            matched_from_policy = True
                            matched_policy_action = str(
                                _policy_field(pol_cand, "action", "") or ""
                            ).lower()
                            break
                        if p_type in (
                            "keyword",
                            "action",
                            "compliance",
                            "secret",
                            "credential",
                            "",
                        ) or PolicyEngine.rule_inspects_content(pol_cand):
                            unmatched_higher_content_policy = True
                        if _policy_field(pol_cand, "halting", False):
                            break

                matched_target, _matched_action, _halt_eval = (
                    evaluate_static_file_routing(
                        item_path,
                        scoped_settings,
                        filename_only=fn_only_low,
                        check_extension_categories=not is_supported,
                        sorted_policies=sorted_policies,
                    )
                )

            norm_rel_p = rel_p.replace("\\", "/")
            rejected_set = rejected_by_doc.get(norm_rel_p, set())
            if matched_target:
                mt_clean = str(matched_target).replace("\\", "/").strip("/").lower()
                if mt_clean in rejected_set or (mt_clean.split("/")[0] in rejected_set):
                    matched_target = None

            if (matched_target or matched_policy_action in ("unarchive", "trash")) and (
                not is_supported
                or (
                    not has_content_compliance_policy
                    and (matched_from_policy or not unmatched_higher_content_policy)
                )
            ):
                bypassed_files.append(item)
                docs_to_upsert.append((src_root, rel_p, file_hash, "[STATUS:BYPASSED]"))
                emit_progress(
                    effective_callback,
                    progress_or_update=progress_ratio,
                    stage=f"Bypassed {item} via rule match",
                    unit_count=idx + 1,
                    unit_type="files",
                )
            elif not is_supported:
                # Unsupported extension format without matching rules: mark as UNSUPPORTED and bypass heavy text extraction
                bypassed_files.append(item)
                docs_to_upsert.append(
                    (src_root, rel_p, file_hash, "[STATUS:UNSUPPORTED]")
                )
                emit_progress(
                    effective_callback,
                    progress_or_update=progress_ratio,
                    stage=f"Bypassed {item} (unsupported format)",
                    unit_count=idx + 1,
                    unit_type="files",
                )
            else:
                emit_progress(
                    effective_callback,
                    progress_or_update=progress_ratio,
                    stage=f"Scanning {item}",
                    unit_count=idx + 1,
                    unit_type="files",
                )

        if docs_to_upsert and db:
            db.upsert_documents(docs_to_upsert)

        return bypassed_files
