"""Metadata pass for pre-evaluating files against rules before text extraction."""

import contextlib
import logging
import mimetypes
import os

from app.core.extractor import get_file_hash
from app.core.extractor_strategies import registry
from app.core.policy_engine import PolicyEngine, match_pattern_expression
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

        keyword_rules = getattr(settings, "KEYWORD_RULES", {})
        learned_rules = getattr(settings, "LEARNED_RULES", {})
        policies = getattr(settings, "POLICIES", [])
        raw_aqr = getattr(settings, "AUTO_QUARANTINE_RATINGS", [])
        auto_quarantine_ratings = (
            [r for r in raw_aqr if r] if isinstance(raw_aqr, (list, tuple, set)) else []
        )
        has_content_compliance_policy = bool(auto_quarantine_ratings) or any(
            isinstance(p, dict)
            and p.get("enabled", True)
            and (
                str(p.get("type", "")).lower() in ("compliance", "secret", "credential")
                or str(p.get("action", "")).lower() in ("quarantine", "redact")
                or str(p.get("expression", "")).strip().lower()
                in ("secret", "credential")
            )
            for p in (policies or [])
        )

        # Sort policies in descending order of priority (higher priority number first)
        sorted_policies = sorted(
            policies, key=lambda x: x.get("priority", 0), reverse=True
        )

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
            matched_policy_action = None
            halt_evaluation = False
            halted_definitively = False
            ignored_definitively = False

            if file_hash in hash_to_target and not sorted_policies:
                matched_target = hash_to_target[file_hash]
            else:
                filename_only = os.path.basename(rel_p).lower()
                file_path_lower = item_path.lower()
                saw_unmatched_content_policy = False
                saw_unmatched_compliance_policy = False

                for policy in sorted_policies:
                    if not policy.get("enabled", True):
                        continue
                    p_type = policy.get("type", "").lower()
                    expression = policy.get("expression", "")
                    target_path = policy.get("target_path")
                    expr_lower = expression.lower()
                    p_action = PolicyEngine.resolve_effective_action(policy)
                    is_glob_or_ext = (
                        "*" in expr_lower
                        or "?" in expr_lower
                        or ("[" in expr_lower and "]" in expr_lower)
                        or (
                            expr_lower.startswith(".")
                            and "/" not in expr_lower
                            and "\\" not in expr_lower
                        )
                    )
                    can_match_doc_text = is_supported and (
                        p_type
                        in (
                            "keyword",
                            "action",
                            "compliance",
                            "secret",
                            "credential",
                            "",
                        )
                        or (
                            p_type in ("copy", "ignore")
                            and bool(expr_lower)
                            and not is_glob_or_ext
                        )
                    )
                    is_compliance_rule = (
                        p_type in ("compliance", "secret", "credential")
                        or p_action in ("quarantine", "redact")
                        or expr_lower in ("secret", "credential")
                    )

                    is_match = False
                    if p_type == "override":
                        if (
                            expr_lower == filename_only
                            or expr_lower in filename_only
                            or expr_lower in file_path_lower
                        ):
                            is_match = True
                    elif p_type == "pattern":
                        if match_pattern_expression(expr_lower, filename_only):
                            is_match = True
                    elif p_type in ("unarchive", "copy", "ignore"):
                        if expr_lower and (
                            match_pattern_expression(expr_lower, filename_only)
                            or expr_lower in filename_only
                        ):
                            is_match = True
                    elif p_type in ("keyword", "action", "compliance", ""):
                        if expr_lower and expr_lower in filename_only:
                            is_match = True
                        elif not expr_lower and p_action:
                            is_match = True

                    if is_match:
                        if p_action == "ignore":
                            if not saw_unmatched_content_policy:
                                ignored_definitively = True
                        elif not saw_unmatched_compliance_policy:
                            matched_policy_action = p_action
                            matched_target = (
                                target_path
                                if target_path
                                else (f"__policy_{p_action}__" if p_action else None)
                            )
                        break
                    else:
                        if can_match_doc_text:
                            saw_unmatched_content_policy = True
                            if is_compliance_rule:
                                saw_unmatched_compliance_policy = True
                        if policy.get("halting", False):
                            halt_evaluation = True
                            if (
                                not can_match_doc_text
                                and not saw_unmatched_content_policy
                            ):
                                halted_definitively = True
                            break

                if (
                    not halt_evaluation
                    and not ignored_definitively
                    and not matched_target
                    and file_hash in hash_to_target
                ):
                    matched_target = hash_to_target[file_hash]

                if (
                    not halt_evaluation
                    and not ignored_definitively
                    and not matched_target
                ):
                    for keyword, target_folder in keyword_rules.items():
                        if keyword.strip() and keyword.lower() in filename_only:
                            matched_target = target_folder
                            break
                    if not matched_target:
                        for keyword, target_folder in learned_rules.items():
                            if keyword.strip() and keyword.lower() in filename_only:
                                matched_target = target_folder
                                break

                # Evaluate extension category mapping / MIME fallback rules for unregistered extensions
                if (
                    not halt_evaluation
                    and not ignored_definitively
                    and not matched_target
                    and not is_supported
                ):
                    matched_target = get_extension_category_target(item, settings)

            if ignored_definitively:
                bypassed_files.append(item)
                docs_to_upsert.append((src_root, rel_p, file_hash, "[STATUS:IGNORED]"))
                emit_progress(
                    effective_callback,
                    progress_or_update=progress_ratio,
                    stage=f"Ignored {item} via policy rule",
                    unit_count=idx + 1,
                    unit_type="files",
                )
                continue

            if halted_definitively:
                bypassed_files.append(item)
                docs_to_upsert.append((src_root, rel_p, file_hash, "[STATUS:HALTED]"))
                emit_progress(
                    effective_callback,
                    progress_or_update=progress_ratio,
                    stage=f"Halted {item} via halting policy rule",
                    unit_count=idx + 1,
                    unit_type="files",
                )
                continue

            norm_rel_p = rel_p.replace("\\", "/")
            rejected_set = rejected_by_doc.get(norm_rel_p, set())
            if matched_target and not matched_policy_action:
                mt_clean = str(matched_target).replace("\\", "/").strip("/").lower()
                if mt_clean in rejected_set or (mt_clean.split("/")[0] in rejected_set):
                    matched_target = None

            if matched_target and (
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
