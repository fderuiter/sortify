"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple

import numpy as np

from app.core.analyzer_strategies import clustering_registry


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling."""

    def __init__(
        self,
        max_folders: int,
        stop_words: set,
        db,
        strategy_name: str = "generative",
        model_path: str | None = None,
    ) -> None:
        self.db = db
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        self.stop_words = stop_words
        self.strategy_name = strategy_name
        self.model_path = model_path
        self.model_name = None
        self.corpus = {}
        self._last_reconstruction_error = 0.0

    def close(self):
        """Terminate processes."""
        pass

    def __del__(self):
        """Clean up."""
        pass

    def terminate(self):
        """Terminate processes."""
        pass

    @property
    def active_model_name(self):
        """Get the name of the currently active model."""
        return self.model_name

    @property
    def active_dimension(self):
        """Get the vector dimension of the currently active model."""
        return None

    @property
    def last_reconstruction_error(self):
        """Get the last reconstruction error from the underlying model."""
        return self._last_reconstruction_error

    def partial_fit(
        self, base_dir: str, new_corpus: dict, runtime_settings=None
    ) -> None:
        """Update the ML model incrementally with new documents."""
        try:
            filepaths = []
            texts = []
            hashes = []
            for filepath, data in new_corpus.items():
                if isinstance(data, dict):
                    texts.append(data.get("text", ""))
                    hashes.append(data.get("hash", ""))
                else:
                    texts.append(data)
                    hashes.append("")
                filepaths.append(filepath)
                self.corpus[filepath] = texts[-1]

            if not texts:
                return

            keyword_rules = getattr(runtime_settings, "KEYWORD_RULES", {}) if runtime_settings else {}
            learned_rules = getattr(runtime_settings, "LEARNED_RULES", {}) if runtime_settings else {}

            documents_to_upsert = []
            for i, (filepath, text, file_hash) in enumerate(
                zip(filepaths, texts, hashes)
            ):
                if not file_hash:
                    file_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
                    hashes[i] = file_hash
                
                documents_to_upsert.append(
                    (
                        base_dir,
                        filepath,
                        file_hash,
                        text,
                    )
                )

            self.db.upsert_documents(documents_to_upsert)

        except Exception as e:
            logging.error(f"Failed during partial_fit. Error: {str(e)}", exc_info=True)

    def reload_stop_words(self, new_stop_words: set) -> None:
        """Reload stop words from config."""
        self.stop_words = new_stop_words

    def _inject_hierarchy(self, node: dict) -> dict:
        """Transform a flat mapping of files into a nested folder structure based on relative paths."""
        if not isinstance(node, dict) or node.get("__type__") == "file":
            return node

        new_node = {}
        for k, v in node.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                dirname = os.path.dirname(k)
                if not dirname:
                    new_node[k] = v
                else:
                    parts = dirname.replace("\\", "/").split("/")
                    current = new_node
                    for part in parts:
                        if (
                            part not in current
                            or current[part] is None
                            or (
                                isinstance(current[part], dict)
                                and current[part].get("__type__") == "file"
                            )
                        ):
                            current[part] = {}
                        current = current[part]
                    current[k] = v
            else:
                new_node[k] = self._inject_hierarchy(v)
        return new_node

    def generate_sorting_plan(self, base_dir: str, runtime_settings=None, locked_files: dict = None) -> dict:
        """Generate a sorting plan based on the current model state."""
        try:
            docs = self.db.get_all_documents(base_dir)
            if not docs:
                return {}

            from app.core.extractor_strategies import registry

            supported_exts = set(registry._extractors.keys())

            keyword_rules = getattr(runtime_settings, "KEYWORD_RULES", {}) if runtime_settings else {}
            learned_rules = getattr(runtime_settings, "LEARNED_RULES", {}) if runtime_settings else {}

            ai_filenames = []
            ai_documents = []
            keyword_plan_files = []
            unsupported_files = []
            historical_overrides = {}

            # Map file hashes to their historical targets
            hash_to_target = {}
            for d in docs:
                if len(d) > 4 and d[4] is not None:
                    hash_to_target[d[3]] = d[4]

            for d in docs:
                f, doc = d[0], d[1]
                file_hash = d[3] if len(d) > 3 else None
                assigned_folder = d[4] if len(d) > 4 else None

                target = (
                    assigned_folder
                    if assigned_folder is not None
                    else hash_to_target.get(file_hash)
                )

                filename_only = os.path.basename(f).lower()
                doc_lower = doc.lower() if doc else ""

                status_match = None
                if doc and doc.startswith("[STATUS:"):
                    status_match = doc[8:-1]

                ext = os.path.splitext(f)[1].lower()
                if ext not in supported_exts and not status_match:
                    status_match = "UNSUPPORTED"

                if target is not None:
                    historical_overrides[f] = (target, status_match)

                matched = False
                if keyword_rules:
                    for keyword, target_folder in keyword_rules.items():
                        if not keyword.strip():
                            continue
                        text_to_search = (
                            filename_only
                            if status_match
                            else (filename_only + " " + doc_lower)
                        )
                        if keyword.lower() in text_to_search:
                            keyword_plan_files.append(
                                (f, target_folder, keyword, "keyword", status_match)
                            )
                            matched = True
                            break

                if not matched and status_match and learned_rules:
                    for keyword, target_folder in learned_rules.items():
                        if not keyword.strip():
                            continue
                        if keyword.lower() in filename_only:
                            keyword_plan_files.append(
                                (f, target_folder, keyword, "pattern", status_match)
                            )
                            matched = True
                            break

                if not matched:
                    if status_match:
                        unsupported_files.append((f, status_match))
                    else:
                        ai_filenames.append(f)
                        ai_documents.append(doc)

            self._last_reconstruction_error = 0.0

            if self.strategy_name:
                max_depth = (
                    getattr(runtime_settings, "MAX_DEPTH", 5) if runtime_settings else 5
                )
                max_features = (
                    getattr(runtime_settings, "MAX_FEATURES", 3)
                    if runtime_settings
                    else 3
                )

                strategy = clustering_registry.get_strategy(self.strategy_name)
                if strategy:
                    plan, error = strategy.generate_plan(
                        ai_filenames,
                        ai_documents,
                        self.max_folders,
                        self.stop_words,
                        max_depth,
                        max_features,
                    )
                    self._last_reconstruction_error = error
                else:
                    plan = {}

                if runtime_settings and getattr(
                    runtime_settings, "PRESERVE_HIERARCHY", False
                ):
                    plan = self._inject_hierarchy(plan)
            else:
                plan = {}

            # Inject keyword routed files back into the plan
            for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files:
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = {
                            "routed_by": routed_by,
                            "keyword": keyword,
                            "extraction_status": ext_status,
                        }
                    else:
                        current = current[part]

            if unsupported_files:
                if "Miscellaneous" not in plan:
                    plan["Miscellaneous"] = {}
                elif not isinstance(plan["Miscellaneous"], dict):
                    plan["Miscellaneous"] = {"_original": plan["Miscellaneous"]}
                for f, ext_status in unsupported_files:
                    plan["Miscellaneous"][f] = {"extraction_status": ext_status}

            def remove_from_plan(node, target_f):
                for k, v in list(node.items()):
                    if k == target_f:
                        if v is None or (
                            isinstance(v, dict)
                            and (v.get("routed_by") or v.get("extraction_status"))
                        ):
                            return node.pop(k)
                        elif isinstance(v, dict) and "_original" in v:
                            val = v.pop("_original")
                            if not v:
                                node.pop(k)
                            return val
                    if isinstance(v, dict):
                        res = remove_from_plan(v, target_f)
                        if res is not None:
                            if not v:
                                node.pop(k)
                            return res
                return None

            if locked_files is None:
                locked_files = {}

            compliance_targets = {
                f: target_folder
                for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files
            }

            for f, override_data in historical_overrides.items():
                target_folder, ext_status = override_data

                is_conflicted = False
                compliance_path = None

                if f in compliance_targets and compliance_targets[f] != target_folder:
                    compliance_path = compliance_targets[f]

                    if f in locked_files and locked_files[f] in (
                        target_folder,
                        compliance_path,
                    ):
                        # User already resolved this conflict
                        target_folder = locked_files[f]
                    else:
                        is_conflicted = True

                remove_from_plan(plan, f)

                info_dict = {"routed_by": "historical"}
                if ext_status:
                    info_dict["extraction_status"] = ext_status

                if is_conflicted:
                    info_dict["is_conflicted"] = True
                    info_dict["compliance_path"] = compliance_path
                    info_dict["historical_path"] = target_folder

                if not target_folder:
                    plan[f] = info_dict
                    continue

                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = info_dict
                    else:
                        current = current[part]

            def _annotate(node, current_path):
                for k, v in list(node.items()):
                    if v is None or (
                        isinstance(v, dict)
                        and (v.get("routed_by") or v.get("extraction_status"))
                    ):
                        filename = os.path.basename(k)
                        target_filename = filename

                        contextual_renaming = False
                        if runtime_settings:
                            contextual_renaming = getattr(
                                runtime_settings, "CONTEXTUAL_RENAMING", False
                            )
                        else:
                            contextual_renaming = False

                        if contextual_renaming:
                            parent_dir = os.path.dirname(k)
                            if parent_dir:
                                parent_folder = os.path.basename(parent_dir)
                                if parent_folder:
                                    safe_parent = re.sub(
                                        r"[^A-Za-z0-9]", "_", parent_folder
                                    )
                                    target_filename = f"{safe_parent}_{filename}"

                        target_path = os.path.join(current_path, target_filename)

                        norm_source = os.path.normpath(k)
                        norm_target = os.path.normpath(target_path)

                        status = (
                            "Already Sorted"
                            if norm_source == norm_target
                            else "Pending Move"
                        )

                        file_dict = {
                            "__type__": "file",
                            "status": status,
                            "source_path": k,
                            "target_filename": target_filename,
                        }
                        if isinstance(v, dict) and v.get("routed_by"):
                            file_dict.update(v)

                        node[k] = file_dict
                    elif isinstance(v, dict):
                        _annotate(v, os.path.join(current_path, k))

            _annotate(plan, "")
            return plan
        except Exception as e:
            logging.error(
                f"Failed during generate_sorting_plan. Error: {str(e)}", exc_info=True
            )
            return {}
