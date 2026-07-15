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
            backend = self._detect_backend(offline_model_path)
            self.model = SentenceTransformer(offline_model_path, backend=backend)
        elif model_path is not None:
            if str(model_path) == str(user_model_path):
                hf_manifest = os.path.join(os.path.dirname(__file__), "hf_manifest.json")
                if os.path.exists(hf_manifest):
                    logging.info("Verifying user downloaded model integrity...")
                    self._verify_hf_model(str(user_model_path), hf_manifest)
            backend = self._detect_backend(model_path)
            self.model = SentenceTransformer(model_path, backend=backend)
        else:
            # Model not downloaded yet (no consent or skipped)
            self.model = None
            
        self.corpus = {}
        self._last_reconstruction_error = 0.0

    def _detect_backend(self, model_dir: str) -> str:
        """Detect the appropriate backend for the model based on available weights."""
        if not os.path.exists(model_dir):
            return "torch"
            
        has_torch = os.path.exists(os.path.join(model_dir, "pytorch_model.bin")) or \
                    os.path.exists(os.path.join(model_dir, "model.safetensors"))
        if has_torch:
            return "torch"
            
        for root, _, files in os.walk(model_dir):
            if any(f.endswith(".onnx") for f in files):
                return "onnx"
                
        return "torch"

    def _verify_hf_model(self, model_dir: str, manifest_path: str) -> None:
        """Verify the checksums of critical files in the downloaded HuggingFace model."""
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to read model manifest: {e}")
            
        critical_files = ["config.json", "tokenizer.json"]
        
        valid_weight_found = False
        
        for rel_path, expected_hash in manifest.items():
            if rel_path.startswith(".cache"):
                continue
                
            filepath = os.path.join(model_dir, rel_path)
            if not os.path.exists(filepath):
                if rel_path in critical_files:
                    raise RuntimeError(f"Missing critical model file: {rel_path}")
                continue
                
            if rel_path in ["pytorch_model.bin", "model.safetensors"] or rel_path.endswith(".onnx"):
                valid_weight_found = True
                
            file_hash = hashlib.sha256()
            try:
                with open(filepath, "rb") as file_obj:
                    while chunk := file_obj.read(8192):
                        file_hash.update(chunk)
            except Exception as e:
                raise RuntimeError(f"Failed to read model file {rel_path}: {e}")
                
            if file_hash.hexdigest() != expected_hash:
                raise RuntimeError(f"Checksum mismatch for downloaded model file: {rel_path}")

        if not valid_weight_found:
            raise RuntimeError("No valid weight formats found (PyTorch, SafeTensors, or ONNX).")

    def _verify_offline_model(self, model_dir: str, manifest_path: str) -> None:
        """Verify the checksums of the offline model against the manifest."""
        try:
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
        except Exception as e:
            raise RuntimeError(f"Failed to read model manifest: {e}")
            
        critical_files = ["config.json", "tokenizer.json"]
        valid_weight_found = False
        
        for rel_path, expected_hash in manifest.items():
            filepath = os.path.join(model_dir, rel_path)
            if not os.path.exists(filepath):
                if rel_path in critical_files:
                    raise RuntimeError(f"Missing critical model file: {rel_path}")
                continue
                
            if rel_path in ["pytorch_model.bin", "model.safetensors"] or rel_path.endswith(".onnx"):
                valid_weight_found = True
                
            file_hash = hashlib.sha256()
            try:
                with open(filepath, "rb") as file_obj:
                    while chunk := file_obj.read(8192):
                        file_hash.update(chunk)
            except Exception as e:
                raise RuntimeError(f"Failed to read model file {rel_path}: {e}")
                
            if file_hash.hexdigest() != expected_hash:
                raise RuntimeError(f"Checksum mismatch for side-loaded model file: {rel_path}")
                
        if not valid_weight_found:
            raise RuntimeError("No valid weight formats found (PyTorch, SafeTensors, or ONNX).")

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

            from app.core.extractor_strategies import registry
            supported_exts = set(registry._extractors.keys())

            supported_docs = []
            unsupported_filenames = []
            historical_overrides = {}

            # Map file hashes to their historical targets
            hash_to_target = {}
            for d in docs:
                if len(d) > 4 and d[4] is not None:
                    hash_to_target[d[3]] = d[4]

            for d in docs:
                ext = os.path.splitext(d[0])[1].lower()
                if ext in supported_exts:
                    supported_docs.append(d)
                    target = d[4] if (len(d) > 4 and d[4] is not None) else hash_to_target.get(d[3])
                    if target is not None:
                        historical_overrides[d[0]] = target
                else:
                    unsupported_filenames.append(d[0])

            filenames = [d[0] for d in supported_docs]
            documents = [d[1] for d in supported_docs]
            embeddings = [d[2] for d in supported_docs]
            
            keyword_rules = getattr(runtime_settings, "KEYWORD_RULES", {}) if runtime_settings else {}
            
            ai_filenames = []
            ai_documents = []
            ai_embeddings = []
            keyword_plan_files = []
            
            for f, doc, emb in zip(filenames, documents, embeddings):
                matched = False
                if keyword_rules:
                    filename_only = os.path.basename(f).lower()
                    doc_lower = doc.lower() if doc else ""
                    for keyword, target_folder in keyword_rules.items():
                        if not keyword.strip():
                            continue
                        if keyword.lower() in filename_only or keyword.lower() in doc_lower:
                            keyword_plan_files.append((f, target_folder, keyword))
                            matched = True
                            break
                if not matched:
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
            for f, target_folder, keyword in keyword_plan_files:
                # Support nested paths if target_folder has slashes
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = {"routed_by": "keyword", "keyword": keyword}
                    else:
                        current = current[part]

            def remove_from_plan(node, target_f):
                for k, v in list(node.items()):
                    if k == target_f:
                        if v is None or (isinstance(v, dict) and v.get("routed_by")):
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

            for f, target_folder in historical_overrides.items():
                remove_from_plan(plan, f)
                if not target_folder:
                    plan[f] = {"routed_by": "historical"}
                    continue
                
                parts = target_folder.replace("\\", "/").split("/")
                current = plan
                for i, part in enumerate(parts):
                    if part not in current:
                        current[part] = {}
                    if not isinstance(current[part], dict):
                        current[part] = {"_original": current[part]}
                    if i == len(parts) - 1:
                        current[part][f] = {"routed_by": "historical"}
                    else:
                        current = current[part]

            if unsupported_filenames:
                if "Miscellaneous" not in plan:
                    plan["Miscellaneous"] = {}
                elif not isinstance(plan["Miscellaneous"], dict):
                    plan["Miscellaneous"] = {"_original": plan["Miscellaneous"]}
                for f in unsupported_filenames:
                    plan["Miscellaneous"][f] = None

            def _annotate(node, current_path):
                for k, v in list(node.items()):
                    if v is None or (isinstance(v, dict) and v.get("routed_by")):
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
