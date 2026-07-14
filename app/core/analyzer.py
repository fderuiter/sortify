"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import hashlib
import json
import logging
import os
import re

from sentence_transformers import SentenceTransformer

from app.core.analyzer_strategies import clustering_registry
from app.core.db import db


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling.

    Uses SentenceTransformer and MiniBatchKMeans to cluster documents incrementally.
    """

    def __init__(
        self, max_folders: int, stop_words: set, strategy_name: str = "default", model_path: str | None = None
    ) -> None:
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        self.stop_words = stop_words
        self.strategy = clustering_registry.get_strategy(strategy_name)
        
        # Check for side-loaded offline model package
        from app.config import get_app_dir
        
        offline_model_path = os.path.join(os.getcwd(), "offline_bundle", "model")
        manifest_path = os.path.join(os.getcwd(), "offline_bundle", "model_manifest.json")
        user_model_path = get_app_dir() / "model"
        
        if os.path.exists(offline_model_path) and os.path.exists(manifest_path):
            logging.info("Detected side-loaded model weights. Verifying integrity...")
            self._verify_offline_model(offline_model_path, manifest_path)
            logging.info("Integrity verified. Loading side-loaded model...")
            self.model = SentenceTransformer(offline_model_path)
        elif model_path is not None:
            if str(model_path) == str(user_model_path):
                hf_manifest = os.path.join(os.path.dirname(__file__), "hf_manifest.json")
                if os.path.exists(hf_manifest):
                    logging.info("Verifying user downloaded model integrity...")
                    self._verify_hf_model(str(user_model_path), hf_manifest)
            self.model = SentenceTransformer(model_path)
        else:
            # Model not downloaded yet (no consent or skipped)
            self.model = None
            
        self.corpus = {}
        self._last_reconstruction_error = 0.0

    def _verify_hf_model(self, model_dir: str, manifest_path: str) -> None:
        """Verify the checksums of critical files in the downloaded HuggingFace model."""
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to read model manifest: {e}")
            
        critical_files = ["config.json", "pytorch_model.bin", "model.safetensors", "tokenizer.json"]
        
        for rel_path, expected_hash in manifest.items():
            if rel_path.startswith(".cache"):
                continue
                
            filepath = os.path.join(model_dir, rel_path)
            if not os.path.exists(filepath):
                if rel_path in critical_files:
                    raise RuntimeError(f"Missing critical model file: {rel_path}")
                continue
                
            file_hash = hashlib.sha256()
            try:
                with open(filepath, "rb") as file_obj:
                    while chunk := file_obj.read(8192):
                        file_hash.update(chunk)
            except Exception as e:
                raise RuntimeError(f"Failed to read model file {rel_path}: {e}")
                
            if file_hash.hexdigest() != expected_hash:
                raise RuntimeError(f"Checksum mismatch for downloaded model file: {rel_path}")

    def _verify_offline_model(self, model_dir: str, manifest_path: str) -> None:
        """Verify the checksums of the offline model against the manifest."""
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to read model manifest: {e}")
            
        for rel_path, expected_hash in manifest.items():
            filepath = os.path.join(model_dir, rel_path)
            if not os.path.exists(filepath):
                raise RuntimeError(f"Missing side-loaded model file: {rel_path}")
                
            file_hash = hashlib.sha256()
            try:
                with open(filepath, "rb") as file_obj:
                    while chunk := file_obj.read(8192):
                        file_hash.update(chunk)
            except Exception as e:
                raise RuntimeError(f"Failed to read model file {rel_path}: {e}")
                
            if file_hash.hexdigest() != expected_hash:
                raise RuntimeError(f"Checksum mismatch for side-loaded model file: {rel_path}")

    @property
    def last_reconstruction_error(self):
        """Get the last reconstruction error from the underlying model.

        Returns
        -------
        float
            The reconstruction error.
        """
        return self._last_reconstruction_error

    def partial_fit(self, base_dir: str, new_corpus: dict) -> None:
        """Update the ML model incrementally with new documents."""
        try:
            # new_corpus is now dict[item_name, dict[text, hash]] or dict[item_name, text]
            # (depending on where it's called from, UI manual moves might just pass text)
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

            texts_to_encode = []
            indices_to_encode = []
            embeddings = [None] * len(filepaths)

            for i, (filepath, text, file_hash) in enumerate(
                zip(filepaths, texts, hashes)
            ):
                # If we don't have a hash, fetch existing from DB so we don't overwrite it with empty
                doc = db.get_document(base_dir, filepath)

                # compute hash if not provided
                if not file_hash:
                    file_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
                    hashes[i] = file_hash

                if (
                    doc
                    and doc["file_hash"] == file_hash
                    and doc["embedding"] is not None
                ):
                    embeddings[i] = doc["embedding"]
                elif text.startswith("[STATUS:"):
                    embeddings[i] = None
                else:
                    texts_to_encode.append(text)
                    indices_to_encode.append(i)

            if texts_to_encode:
                if self.model is None:
                    # Dummy embeddings if offline mode without model
                    new_embeddings = [None] * len(texts_to_encode)
                else:
                    new_embeddings = self.model.encode(
                        texts_to_encode, show_progress_bar=False
                    )
                for idx, new_emb in zip(indices_to_encode, new_embeddings):
                    embeddings[idx] = new_emb

            for filepath, text, file_hash, embedding in zip(
                filepaths, texts, hashes, embeddings
            ):
                db.upsert_document(base_dir, filepath, file_hash, text, embedding)

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

    def generate_sorting_plan(self, base_dir: str, runtime_settings=None) -> dict:
        """Generate a sorting plan based on the current model state.

        Returns
        -------
        dict
            A nested mapping where keys are generated folder names and values are
            either dicts (subfolders) or file metadata dicts.
        """
        try:
            docs = db.get_all_documents(base_dir)
            if not docs:
                return {}

            keyword_rules = getattr(runtime_settings, "KEYWORD_RULES", {}) if runtime_settings else {}
            learned_rules = getattr(runtime_settings, "LEARNED_RULES", {}) if runtime_settings else {}
            
            ai_filenames = []
            ai_documents = []
            ai_embeddings = []
            keyword_plan_files = []
            unsupported_files = []
            
            from app.core.extractor_strategies import registry
            supported_exts = set(registry._extractors.keys())

            for d in docs:
                f, doc, emb = d[0], d[1], d[2]
                filename_only = os.path.basename(f).lower()
                doc_lower = doc.lower() if doc else ""
                
                status_match = None
                if doc and doc.startswith("[STATUS:"):
                    status_match = doc[8:-1] # e.g. EMPTY, ENCRYPTED, UNSUPPORTED, FAILED
                    
                ext = os.path.splitext(f)[1].lower()
                if ext not in supported_exts and not status_match:
                    status_match = "UNSUPPORTED"
                    
                matched = False
                if keyword_rules:
                    for keyword, target_folder in keyword_rules.items():
                        if not keyword.strip():
                            continue
                        text_to_search = filename_only if status_match else (filename_only + " " + doc_lower)
                        if keyword.lower() in text_to_search:
                            keyword_plan_files.append((f, target_folder, keyword, "keyword", status_match))
                            matched = True
                            break
                            
                if not matched and status_match and learned_rules:
                    for keyword, target_folder in learned_rules.items():
                        if not keyword.strip():
                            continue
                        if keyword.lower() in filename_only:
                            keyword_plan_files.append((f, target_folder, keyword, "pattern", status_match))
                            matched = True
                            break

                if not matched:
                    if status_match:
                        unsupported_files.append((f, status_match))
                    else:
                        ai_filenames.append(f)
                        ai_documents.append(doc)
                        ai_embeddings.append(emb)

            self._last_reconstruction_error = 0.0

            if self.model is None:
                # If no model, just create a flat unsorted plan
                plan = {f: None for f in ai_filenames}
            elif self.strategy:
                max_depth = (
                    getattr(runtime_settings, "MAX_DEPTH", 5) if runtime_settings else 5
                )
                max_features = (
                    getattr(runtime_settings, "MAX_FEATURES", 3)
                    if runtime_settings
                    else 3
                )
                plan, error = self.strategy.generate_plan(
                    ai_filenames,
                    ai_documents,
                    ai_embeddings,
                    self.max_folders,
                    self.stop_words,
                    max_depth,
                    max_features,
                )
                self._last_reconstruction_error = error

                if runtime_settings and getattr(
                    runtime_settings, "PRESERVE_HIERARCHY", False
                ):
                    plan = self._inject_hierarchy(plan)
            else:
                plan = {}

            # Inject keyword routed files back into the plan
            for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files:
                # Support nested paths if target_folder has slashes
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = {"routed_by": routed_by, "keyword": keyword, "extraction_status": ext_status}
                    else:
                        current = current[part]

            if unsupported_files:
                if "Miscellaneous" not in plan:
                    plan["Miscellaneous"] = {}
                elif not isinstance(plan["Miscellaneous"], dict):
                    plan["Miscellaneous"] = {"_original": plan["Miscellaneous"]}
                for f, ext_status in unsupported_files:
                    plan["Miscellaneous"][f] = {"extraction_status": ext_status}

            def _annotate(node, current_path):
                for k, v in list(node.items()):
                    if v is None or (isinstance(v, dict) and (v.get("routed_by") or v.get("extraction_status"))):
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
