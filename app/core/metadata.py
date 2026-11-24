"""Metadata pass for pre-evaluating files against rules before text extraction."""

import contextlib
import logging
import mimetypes
import os

from app.core.extractor import get_file_hash
from app.core.extractor_strategies import registry
from app.core.policy_engine import PolicyEngine
from app.core.progress import emit_progress

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
        if ext in ext_categories:
            return ext_categories[ext]
        ext_no_dot = ext.lstrip(".")
        if ext_no_dot in ext_categories:
            return ext_categories[ext_no_dot]

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


def _evaluate_static_routing_detail(
    file_path: str,
    settings: object,
    *,
    filename_only: str | None = None,
    check_extension_categories: bool = True,
    sorted_policies: list[dict] | None = None,
) -> tuple[str | None, bool, str | None, bool]:
    """Evaluate static routing and return ``(matched_target, has_valid_action, action_name, halt_evaluation)``."""
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

    eval_out = PolicyEngine.evaluate_policies(
        file_path,
        "",
        "[STATUS:STATIC]",
        active_policies,
        return_halting=True,
        is_sorted=True,
        abs_file_path=file_path if file_path and os.path.isfile(file_path) else None,
    )
    matched_policy, halt_evaluation = (
        eval_out if isinstance(eval_out, tuple) else (eval_out, False)
    )

    matched_target: str | None = None
    matched_action = False
    matched_action_name: str | None = None
    if matched_policy is not None:
        raw_target = matched_policy.get("target_path")
        if raw_target:
            matched_target = str(raw_target)
        raw_action = matched_policy.get("action")
        if raw_action:
            norm_act = str(raw_action).strip().lower()
            matched_action = PolicyEngine.validate_policy_action(norm_act)
            if matched_action:
                matched_action_name = norm_act

    if not halt_evaluation and not matched_target and not matched_action:
        for attr_name in ("KEYWORD_RULES", "LEARNED_RULES"):
            rules = getattr(settings, attr_name, {}) if settings else {}
            if isinstance(rules, dict):
                for keyword, target_folder in rules.items():
                    kw_str = str(keyword)
                    if kw_str.strip() and kw_str.lower() in fn_lower and target_folder:
                        matched_target = str(target_folder)
                        break
            if matched_target:
                break

    if (
        not halt_evaluation
        and not matched_target
        and not matched_action
        and check_extension_categories
    ):
        matched_target = get_extension_category_target(file_path, settings)

    return matched_target, matched_action, matched_action_name, halt_evaluation


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
    matched_target, matched_action, _action_name, halt_evaluation = (
        _evaluate_static_routing_detail(
            file_path,
            settings,
            filename_only=filename_only,
            check_extension_categories=check_extension_categories,
            sorted_policies=sorted_policies,
        )
    )
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

        policies = getattr(settings, "POLICIES", [])
        auto_quarantine_ratings = [
            r for r in (getattr(settings, "AUTO_QUARANTINE_RATINGS", []) or []) if r
        ]
        has_content_compliance_policy = bool(auto_quarantine_ratings) or any(
            isinstance(p, dict)
            and p.get("enabled", True)
            and (
                str(p.get("type", "")).lower() == "compliance"
                or str(p.get("action", "")).lower() in ("quarantine", "redact")
            )
            for p in (policies or [])
        )

        # Sort policies in descending order of priority (higher priority number first)
        valid_policies = (
            [
                p
                for p in (policies or [])
                if isinstance(p, dict) or hasattr(p, "model_dump")
            ]
            if isinstance(policies, (list, tuple))
            else []
        )
        sorted_policies = PolicyEngine.sort_policies(valid_policies)

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
            matched_action_name: str | None = None
            if file_hash in hash_to_target:
                matched_target = hash_to_target[file_hash]
            else:
                (
                    matched_target,
                    _matched_action,
                    matched_action_name,
                    _halt_eval,
                ) = _evaluate_static_routing_detail(
                    item_path,
                    settings,
                    filename_only=os.path.basename(rel_p).lower(),
                    check_extension_categories=not is_supported,
                    sorted_policies=sorted_policies,
                )

            norm_rel_p = rel_p.replace("\\", "/")
            rejected_set = rejected_by_doc.get(norm_rel_p, set())
            if matched_target:
                mt_clean = str(matched_target).replace("\\", "/").strip("/").lower()
                if mt_clean in rejected_set or (mt_clean.split("/")[0] in rejected_set):
                    matched_target = None

            if (matched_target or matched_action_name in ("trash", "skip")) and (
                not is_supported or not has_content_compliance_policy
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
