"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import hashlib
import logging
import os

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
        self.corpus = {}
        self._last_reconstruction_error = 0.0

        if not self.model_path:
            from app.config import get_app_dir
            from app.core.path_utils import get_base_path

            try:
                base_path = get_base_path(__file__)
            except Exception:
                base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local_bundle_path = os.path.join(base_path, "offline_bundle", "model")
            try:
                user_bundle_path = str(get_app_dir() / "model")
            except Exception:
                user_bundle_path = os.path.expanduser("~/.smart-autosorter/model")

            if os.path.exists(local_bundle_path):
                self.model_path = local_bundle_path
            elif os.path.exists(user_bundle_path):
                self.model_path = user_bundle_path

        from app.core.semantic_embeddings import SemanticEmbeddingManager

        self.embedding_manager = SemanticEmbeddingManager(self.db, self.model_path)

    def close(self):
        """Terminate processes."""
        self.terminate()

    def __del__(self):
        """Clean up."""
        self.terminate()

    def terminate(self):
        """Terminate processes."""
        if hasattr(self, "embedding_manager") and self.embedding_manager:
            try:
                self.embedding_manager.stop()
            except Exception:
                pass
        if getattr(self, "strategy_name", None):
            try:
                from app.core.analyzer_strategies import clustering_registry

                strategy = clustering_registry.get_strategy(self.strategy_name)
                if strategy and hasattr(strategy, "_fallback_to_pytorch"):
                    strategy._fallback_to_pytorch()
            except Exception:
                pass

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

    def generate_sorting_plan(
        self,
        base_dir: str,
        runtime_settings=None,
        locked_files: dict = None,
        cancel_check=None,
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
            policies = (
                getattr(runtime_settings, "POLICIES", []) if runtime_settings else []
            )
            sorted_policies = sorted(
                policies, key=lambda x: x.get("priority", 0), reverse=True
            )

            def match_policy(rule, file_path, doc_text, st_match) -> bool:
                rule_type = rule.get("type", "").lower()
                expression = rule.get("expression", "").lower()

                fn_only = os.path.basename(file_path).lower()
                dl_lower = doc_text.lower() if doc_text else ""

                if rule_type == "keyword":
                    text_to_search = fn_only if st_match else (fn_only + " " + dl_lower)
                    return expression in text_to_search
                elif rule_type == "pattern":
                    return expression in fn_only
                elif rule_type == "override":
                    return (
                        expression == fn_only
                        or expression in fn_only
                        or expression in file_path.lower()
                    )
                return False

            ai_filenames = []
            ai_documents = []
            policy_plan_files = []
            keyword_plan_files = []
            unsupported_files = []
            historical_overrides = {}

            # Map file hashes to their historical targets
            hash_to_target = {}
            for d in docs:
                if cancel_check and cancel_check():
                    return {}
                if len(d) > 3 and d[3] is not None:
                    hash_to_target[d[2]] = d[3]

            for d in docs:
                if cancel_check and cancel_check():
                    return {}
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

                # Check if this file has a path lock / manual override first!
                if locked_files and f in locked_files:
                    target = locked_files[f]
                    historical_overrides[f] = (target, status_match)
                    continue

                # Check against unified policies first!
                matched_policy = None
                if sorted_policies:
                    for rule in sorted_policies:
                        if match_policy(rule, f, doc, status_match):
                            matched_policy = rule
                            break

                if matched_policy:
                    policy_plan_files.append(
                        (
                            f,
                            matched_policy["target_path"],
                            matched_policy["expression"],
                            matched_policy["type"],
                            status_match,
                        )
                    )
                    continue

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

            # Document-to-Document Content Similarity Matching Phase
            historical_docs = []
            for d in docs:
                file_hash = d[2] if len(d) > 2 else None
                assigned_folder = d[3] if len(d) > 3 else None
                target = (
                    assigned_folder
                    if assigned_folder is not None
                    else hash_to_target.get(file_hash)
                )

                if target is not None and d[1]:
                    from app.core.extractor_strategies import registry

                    ext = os.path.splitext(d[0])[1].lower()
                    if (
                        ext not in {".png", ".jpg", ".jpeg"}
                        and registry.is_supported(ext)
                        and not d[1].startswith("[STATUS:")
                        and d[1].strip()
                    ):
                        historical_docs.append(
                            {"text": d[1], "target_folder": target, "filepath": d[0]}
                        )

            if historical_docs and ai_filenames:
                try:
                    import numpy as np
                    from sklearn.metrics.pairwise import cosine_similarity

                    # Check if embedding reconstruction is active
                    if self.embedding_manager.is_mock:
                        use_semantic = False
                        logging.info(
                            "Semantic engine in mock state, bypassing semantic routing and using text similarity."
                        )
                    else:
                        use_semantic = getattr(
                            self.embedding_manager, "is_model_valid", True
                        )
                        if self.embedding_manager.is_reconstruction_active():
                            use_semantic = False
                            logging.info(
                                "Background reconstruction active, falling back to standard text similarity."
                            )
                        else:
                            # Try to load vector embeddings for historical docs
                            hist_vectors = []
                            for doc in historical_docs:
                                vector = self.embedding_manager.get_vector(
                                    base_dir, doc["filepath"]
                                )
                                if (
                                    not vector
                                    or not self.embedding_manager.validate_vector_dimension(
                                        vector
                                    )
                                ):
                                    use_semantic = False
                                    logging.info(
                                        "Obsolete/missing vectors or dimension mismatch detected. Initiating cleanup and background recovery."
                                    )
                                    # Re-verify model to perform purge of outdated vectors
                                    self.embedding_manager.verify_active_model()
                                    # Trigger background reconstruction
                                    self.embedding_manager.trigger_reconstruction(
                                        base_dir
                                    )
                                    break
                                hist_vectors.append(vector)

                    if use_semantic:
                        # Query local database for existing vector embeddings first, generating and bulk-saving only missing ones
                        ai_vectors = []
                        newly_generated = []
                        try:
                            for f_name, doc_text in zip(ai_filenames, ai_documents):
                                v = self.embedding_manager.get_vector(base_dir, f_name)
                                if (
                                    v is not None
                                    and self.embedding_manager.validate_vector_dimension(
                                        v
                                    )
                                ):
                                    ai_vectors.append(v)
                                else:
                                    generated_v = self.embedding_manager.generate_embedding(doc_text)
                                    if not self.embedding_manager.validate_vector_dimension(
                                        generated_v
                                    ):
                                        raise ValueError(
                                            "Generated vector dimensions do not match the active model dimensions."
                                        )
                                    ai_vectors.append(generated_v)
                                    newly_generated.append((f_name, generated_v))

                            if newly_generated:
                                self.db.upsert_document_vectors(
                                    base_dir, newly_generated
                                )
                        except Exception as e:
                            logging.error(
                                f"Error generating or retrieving active model vectors: {e}. Falling back to standard text similarity."
                            )
                            use_semantic = False

                    if use_semantic:
                        # Calculate similarity using vector embeddings
                        similarities = cosine_similarity(
                            np.array(ai_vectors), np.array(hist_vectors)
                        )
                    else:
                        # Fallback gracefully to standard TF-IDF text similarity
                        from sklearn.feature_extraction.text import TfidfVectorizer

                        historical_texts = [doc["text"] for doc in historical_docs]
                        vectorizer = TfidfVectorizer(
                            stop_words=list(self.stop_words),
                            max_features=1000,
                            sublinear_tf=True,
                        )
                        safe_ai_documents = [d or "" for d in ai_documents]
                        vectorizer.fit(historical_texts)

                        historical_vectors = vectorizer.transform(historical_texts)
                        new_docs_vectors = vectorizer.transform(safe_ai_documents)

                        similarities = cosine_similarity(
                            new_docs_vectors, historical_vectors
                        )

                    historical_targets = [
                        doc["target_folder"] for doc in historical_docs
                    ]

                    remaining_ai_filenames = []
                    remaining_ai_documents = []

                    for i, f in enumerate(ai_filenames):
                        if len(historical_docs) > 0:
                            max_sim = np.max(similarities[i])
                            best_doc_idx = np.argmax(similarities[i])
                            if max_sim >= 0.8:
                                target_folder = historical_targets[best_doc_idx]
                                keyword_plan_files.append(
                                    (
                                        f,
                                        target_folder,
                                        f"similarity >= 0.8 ({max_sim:.2f})",
                                        "similarity",
                                        None,
                                    )
                                )
                            else:
                                remaining_ai_filenames.append(f)
                                remaining_ai_documents.append(ai_documents[i])
                        else:
                            remaining_ai_filenames.append(f)
                            remaining_ai_documents.append(ai_documents[i])

                    ai_filenames = remaining_ai_filenames
                    ai_documents = remaining_ai_documents
                except Exception as e:
                    logging.error(
                        f"Failed during document-to-document similarity matching. Error: {str(e)}",
                        exc_info=True,
                    )

            self._last_reconstruction_error = 0.0

            active_strategy_name = self.strategy_name
            if self.strategy_name and self.embedding_manager.is_mock:
                active_strategy_name = "default"

            if active_strategy_name:
                max_depth = (
                    getattr(runtime_settings, "MAX_DEPTH", 5) if runtime_settings else 5
                )
                max_features = (
                    getattr(runtime_settings, "MAX_FEATURES", 3)
                    if runtime_settings
                    else 3
                )

                strategy = clustering_registry.get_strategy(active_strategy_name)
                if strategy:
                    if hasattr(strategy, "set_db_context"):
                        strategy.set_db_context(self.db, base_dir)
                    else:
                        strategy.db = self.db
                        strategy.base_dir = base_dir

                    use_semantic = not self.embedding_manager.is_mock
                    pre_fetched_vectors = None

                    if use_semantic and ai_filenames:
                        try:
                            # Identify missing vectors, generate them on-the-fly and cache to DB sequentially in sorting thread
                            vectors = []
                            newly_generated = []
                            for f_name, doc_text in zip(ai_filenames, ai_documents):
                                v = self.embedding_manager.get_vector(base_dir, f_name)
                                if (
                                    v is not None
                                    and self.embedding_manager.validate_vector_dimension(
                                        v
                                    )
                                ):
                                    vectors.append(v)
                                else:
                                    generated_v = (
                                        self.embedding_manager.generate_embedding(
                                            doc_text
                                        )
                                    )
                                    if not self.embedding_manager.validate_vector_dimension(
                                        generated_v
                                    ):
                                        raise ValueError(
                                            "Generated vector dimensions do not match the active model dimensions."
                                        )
                                    vectors.append(generated_v)
                                    newly_generated.append((f_name, generated_v))

                            if newly_generated:
                                self.db.upsert_document_vectors(
                                    base_dir, newly_generated
                                )

                            pre_fetched_vectors = vectors
                        except Exception as e:
                            logging.error(
                                f"Lazy semantic vector generation/caching failed: {e}. Falling back to standard TF-IDF."
                            )
                            pre_fetched_vectors = None

                    plan, error = strategy.generate_plan(
                        ai_filenames,
                        ai_documents,
                        self.max_folders,
                        self.stop_words,
                        max_depth,
                        max_features,
                        pre_fetched_vectors=pre_fetched_vectors,
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

            # Inject policy routed files back into the plan
            for (
                f,
                target_folder,
                expression,
                rule_type,
                ext_status,
            ) in policy_plan_files:
                if cancel_check and cancel_check():
                    return {}
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = {
                            "routed_by": rule_type,
                            "keyword": expression,
                            "extraction_status": ext_status,
                        }
                    else:
                        current = current[part]

            # Inject keyword routed files back into the plan
            for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files:
                if cancel_check and cancel_check():
                    return {}
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

            # Phase 1: Policy, Keyword, and Learned Rule sorting
            compliance_targets = {}
            for f, target_folder, expression, rule_type, status in policy_plan_files:
                compliance_targets[f] = target_folder
            for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files:
                compliance_targets[f] = target_folder

            for f, target_folder, expression, rule_type, status in policy_plan_files:
                if cancel_check and cancel_check():
                    return {}
                if target_folder not in plan:
                    plan[target_folder] = {}
                plan[target_folder][f] = {
                    "__type__": "file",
                    "routed_by": rule_type,
                    "match": expression,
                    "status": status,
                }

            for f, target_folder, keyword, rule_type, status in keyword_plan_files:
                if cancel_check and cancel_check():
                    return {}
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
                if cancel_check and cancel_check():
                    return {}
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

            clean_plan = {}
            import ntpath

            for target_folder, files in plan.items():
                if cancel_check and cancel_check():
                    return {}
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
