"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import hashlib
import logging
import os
from typing import Any, Dict, Optional

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
        self.max_folders = max_folders
        self.stop_words = stop_words
        self.strategy_name = strategy_name
        self.model_path = model_path
        self.model_name = None
        self.corpus: Dict[str, Any] = {}
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

        new_node: Dict[str, Any] = {}
        for k, v in node.items():
            if v is None or (isinstance(v, dict) and v.get("__type__") == "file"):
                dirname = os.path.dirname(k)
                if not dirname:
                    new_node[k] = v
                else:
                    parts = dirname.replace("\\", "/").split("/")
                    current: Dict[str, Any] = new_node
                    for part in parts:
                        val = current.get(part)
                        if val is None or (
                            isinstance(val, dict) and val.get("__type__") == "file"
                        ):
                            current[part] = {}
                            current = current[part]
                        else:
                            current = val  # type: ignore
                    current[k] = v
            else:
                new_node[k] = self._inject_hierarchy(v)
        return new_node

    def generate_sorting_plan(
        self, base_dir: str, runtime_settings=None, locked_files: Optional[dict] = None
    ) -> dict:
        """Generate a sorting plan based on the current model state."""
        try:
            docs = self.db.get_all_documents(base_dir)
            if not docs:
                return {}

            from app.core.extractor_strategies import registry

            supported_exts = set(registry._extractors.keys())

            keyword_rules = (
                getattr(runtime_settings, "KEYWORD_RULES", {})
                if runtime_settings
                else {}
            )
            learned_rules = (
                getattr(runtime_settings, "LEARNED_RULES", {})
                if runtime_settings
                else {}
            )

            ai_filenames = []
            ai_documents = []
            keyword_plan_files = []
            unsupported_files = []
            historical_overrides = {}

            # Map file hashes to their historical targets
            hash_to_target = {}
            for d in docs:
                if len(d) > 3 and d[3] is not None:
                    hash_to_target[d[2]] = d[3]

            for d in docs:
                f, doc = d[0], d[1]
                file_hash = d[2] if len(d) > 2 else None
                assigned_folder = d[3] if len(d) > 3 else None

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

            if locked_files is None:
                locked_files = {}

            def remove_from_plan(node, target_f):
                for k, v in list(node.items()):
                    if k == target_f:
                        val = node.pop(k)
                        if isinstance(val, dict) and "_original" in val:
                            return val.get("_original")
                        return val
                    if isinstance(v, dict):
                        res = remove_from_plan(v, target_f)
                        if res is not None:
                            if not v:
                                node.pop(k)
                            return res
                return None

            # Phase 1: Keyword, and Learned Rule sorting
            compliance_targets = {
                f: target_folder
                for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files
            }

            for f, target_folder, keyword, rule_type, status in keyword_plan_files:
                remove_from_plan(plan, f)
                if target_folder not in plan:
                    plan[target_folder] = {}
                plan[target_folder][f] = {
                    "__type__": "file",
                    "routed_by": rule_type,
                    "match": keyword,
                    "status": status,
                }

            # Inject Historical Assignments and handle conflicts
            for f, (target_folder, status) in historical_overrides.items():
                is_conflicted = False
                compliance_path = None

                if f in compliance_targets and compliance_targets[f] != target_folder:
                    compliance_path = compliance_targets[f]

                    if (
                        locked_files
                        and f in locked_files
                        and locked_files[f]
                        in (
                            target_folder,
                            compliance_path,
                        )
                    ):
                        target_folder = locked_files[f]
                    else:
                        is_conflicted = True

                # Remove from other locations if present
                remove_from_plan(plan, f)

                if target_folder not in plan:
                    plan[target_folder] = {}

                info = {
                    "__type__": "file",
                    "routed_by": "historical",
                    "match": "user assignment",
                    "status": status,
                }

                if is_conflicted:
                    info["is_conflicted"] = True
                    info["compliance_path"] = compliance_path
                    info["historical_path"] = target_folder

                plan[target_folder][f] = info

            # Phase 3: Route unsupported files safely
            if unsupported_files:
                if "Miscellaneous" not in plan:
                    plan["Miscellaneous"] = {}
                for f, status in unsupported_files:
                    if f not in plan["Miscellaneous"]:
                        plan["Miscellaneous"][f] = {
                            "__type__": "file",
                            "routed_by": "fallback",
                            "match": "none",
                            "status": status,
                        }

            clean_plan: Dict[str, Any] = {}
            import ntpath

            for target_folder, files in plan.items():
                if not isinstance(files, dict) or not files:
                    continue

                if os.path.isabs(target_folder) or ntpath.isabs(target_folder):
                    if target_folder not in clean_plan:
                        clean_plan[target_folder] = {}
                    for f, info in files.items():
                        clean_plan[target_folder][f] = info
                    continue

                parts = target_folder.replace("\\", "/").split("/")
                current = clean_plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        for f, info in files.items():
                            current[part][f] = info
                    else:
                        current = current[part]

            return self._inject_hierarchy(clean_plan)

        except Exception as e:
            logging.error(
                f"Failed during generate_sorting_plan. Error: {str(e)}", exc_info=True
            )
            return {}
