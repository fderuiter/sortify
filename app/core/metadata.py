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

        docs = db.get_all_documents(base_dir)
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
                if callback:
                    callback()

        if docs_to_upsert:
            db.upsert_documents(docs_to_upsert)

        return bypassed_files
