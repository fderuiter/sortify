"""Metadata pass for pre-evaluating files against rules before text extraction."""

import logging
import os

from app.core.extractor import get_file_hash


class MetadataPass:
    """Component to execute static rule matching logic prior to heavy ingestion."""

    @staticmethod
    def run(
        base_dir: str, items_to_sort: list, settings, db, callback, cancel_check
    ) -> list:
        """Run an initial sequential metadata pass to bypass text extraction for matching files."""
        if not base_dir:
            return []

        keyword_rules = getattr(settings, "KEYWORD_RULES", {})
        learned_rules = getattr(settings, "LEARNED_RULES", {})
        policies = getattr(settings, "POLICIES", [])

        # Sort policies in descending order of priority (higher priority number first)
        sorted_policies = sorted(
            policies, key=lambda x: x.get("priority", 0), reverse=True
        )

        docs = db.get_all_documents(base_dir) if db else []
        hash_to_target = {}
        for d in docs:
            if len(d) >= 4 and d[2] and d[3]:
                hash_to_target[d[2]] = d[3]

        bypassed_files = []
        docs_to_upsert = []

        for item in items_to_sort:
            if cancel_check and cancel_check():
                break

            item_path = os.path.join(base_dir, item)
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

            matched_target = None
            if file_hash in hash_to_target:
                matched_target = hash_to_target[file_hash]
            else:
                filename_only = os.path.basename(item).lower()
                file_path_lower = item_path.lower()
                halt_evaluation = False

                for policy in sorted_policies:
                    p_type = policy.get("type", "").lower()
                    expression = policy.get("expression", "")
                    target_path = policy.get("target_path")
                    expr_lower = expression.lower()

                    is_match = False
                    if p_type == "override":
                        if (
                            expr_lower == filename_only
                            or expr_lower in filename_only
                            or expr_lower in file_path_lower
                        ):
                            is_match = True
                    elif p_type == "pattern":
                        if expr_lower in filename_only:
                            is_match = True
                    elif p_type == "keyword":
                        if expr_lower in filename_only:
                            is_match = True

                    if is_match:
                        matched_target = target_path
                        break
                    else:
                        if policy.get("halting", False):
                            halt_evaluation = True
                            break

                if not halt_evaluation and not matched_target:
                    for keyword, target_folder in keyword_rules.items():
                        if keyword.strip() and keyword.lower() in filename_only:
                            matched_target = target_folder
                            break
                    if not matched_target:
                        for keyword, target_folder in learned_rules.items():
                            if keyword.strip() and keyword.lower() in filename_only:
                                matched_target = target_folder
                                break

            if matched_target:
                bypassed_files.append(item)
                docs_to_upsert.append((base_dir, item, file_hash, "[STATUS:BYPASSED]"))
                if db:
                    db.set_user_verified_target_path(base_dir, item, matched_target)
                if callback:
                    callback()

        if docs_to_upsert and db:
            db.upsert_documents(docs_to_upsert)

        return bypassed_files
