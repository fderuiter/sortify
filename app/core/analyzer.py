"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import hashlib
import logging
import os
from typing import Any, Dict, List, Tuple

from app.core.analyzer_strategies import clustering_registry


def _worker_generate_plan(
    strategy_name: str,
    filenames: List[str],
    documents: List[str],
    max_folders: int,
    stop_words: set,
    max_depth: int,
    max_features: int,
) -> Tuple[Dict[str, Any], float]:
    strategy = clustering_registry.get_strategy(strategy_name)
    if not strategy:
        return {}, 0.0

    try:
        return strategy.generate_plan(
            filenames,
            documents,
            max_folders,
            stop_words,
            max_depth,
            max_features,
        )
    except Exception as e:
        logging.error(f"Error generating plan in worker: {e}")
        raise e


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling.

    Uses TF-IDF and MiniBatchKMeans to cluster documents incrementally.
    """

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
        self.corpus = {}
        self._last_reconstruction_error = 0.0

        import concurrent.futures
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        self.executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=context,
        )

    def close(self):
        """Terminate the background processes and close queues cleanly."""
        self.terminate()

    def __del__(self):
        """Ensure background processes and queues are cleaned up on garbage collection."""
        self.terminate()

    def terminate(self):
        """Terminate background processes."""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)


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
                self.corpus[filepath] = texts[-1]  # keep in-memory for UI triggers

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
            import concurrent.futures.process
            if isinstance(e, concurrent.futures.process.BrokenProcessPool):
                raise RuntimeError(
                    "Background worker process crashed or ran out of memory."
                ) from e

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

            plan = {}

            
            # Phase 1: Keyword, and Learned Rule sorting
            compliance_targets = {
                f: target_folder
                for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files
            }

            for f, target_folder, keyword, rule_type, status in keyword_plan_files:
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

                    if locked_files and f in locked_files and locked_files[f] in (
                        target_folder,
                        compliance_path,
                    ):
                        target_folder = locked_files[f]
                    else:
                        is_conflicted = True
                
                # Remove from other locations if present
                for t in plan.values():
                    if isinstance(t, dict) and f in t:
                        del t[f]

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
# Phase 2: Generative/Clustering assignment for remaining files
            if ai_filenames:
                future = self.executor.submit(
                    _worker_generate_plan,
                    self.strategy_name,
                    ai_filenames,
                    ai_documents,
                    self.max_folders,
                    self.stop_words,
                    max_depth=5,
                    max_features=3,
                )
                ai_plan, error = future.result()
                self._last_reconstruction_error = error

                def _merge_plan(src, dst):
                    for k, v in src.items():
                        if isinstance(v, dict) and k in dst and isinstance(dst[k], dict):
                            _merge_plan(v, dst[k])
                        elif v is None:
                            dst[k] = {
                                "__type__": "file",
                                "routed_by": "generative",
                                "match": "cluster",
                                "status": None,
                            }
                        else:
                            dst[k] = v

                _merge_plan(ai_plan, plan)

            # Phase 3: Route unsupported files safely
            if unsupported_files:
                if "Miscellaneous" not in plan:
                    plan["Miscellaneous"] = {}
                for f, status in unsupported_files:
                    plan["Miscellaneous"][f] = {
                        "__type__": "file",
                        "routed_by": "fallback",
                        "match": "none",
                        "status": status,
                    }

            clean_plan = {}
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
            logging.error(f"Failed to generate sorting plan: {e}", exc_info=True)
            import concurrent.futures.process
            if isinstance(e, concurrent.futures.process.BrokenProcessPool):
                raise RuntimeError(
                    "Background worker process crashed or ran out of memory."
                ) from e
            return {}

    @property
    def last_reconstruction_error(self):
        """Mock property for guardrail compatibility."""
        return getattr(self, "_last_reconstruction_error", 0.05)
