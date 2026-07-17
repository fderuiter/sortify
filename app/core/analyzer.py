"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import hashlib
import json
import logging
import multiprocessing as mp
import os
import re

import numpy as np

from app.core.analyzer_strategies import clustering_registry
from app.core.db import db


def gguf_worker_process(model_path: str, q_in: mp.Queue, q_out: mp.Queue):
    """Subprocess for handling GGUF inference in isolation."""
    try:
        import os

        from llama_cpp import Llama
        cpu_count = os.cpu_count() or 4
        n_threads = max(1, int(cpu_count * 0.75))
        
        model = Llama(
            model_path=model_path,
            n_ctx=512,
            n_batch=128,
            n_threads=n_threads,
            embedding=True,
            verbose=False
        )
        
        while True:
            task = q_in.get()
            if task is None:
                break
                
            texts_to_encode = task
            embeddings = []
            for text in texts_to_encode:
                res = model.create_embedding(text)
                if 'data' in res and len(res['data']) > 0:
                    embeddings.append(res['data'][0]['embedding'])
                else:
                    embeddings.append(None)
            q_out.put(embeddings)
    except Exception as e:
        import logging
        logging.error(f"GGUF worker crashed: {e}")
        q_out.put(None)

class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling.

    Uses SentenceTransformer and MiniBatchKMeans to cluster documents incrementally.
    """

    def __init__(
        self, max_folders: int, stop_words: set, strategy_name: str = "generative", model_path: str | None = None
    ) -> None:
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        self.stop_words = stop_words
        self.strategy = clustering_registry.get_strategy(strategy_name)
        
        # Check for side-loaded offline model package
        import sys
        
        from app.config import get_app_dir
        
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            
        offline_model_path = os.path.join(base_path, "offline_bundle", "model")
        manifest_path = os.path.join(base_path, "offline_bundle", "model_manifest.json")
        user_model_path = get_app_dir() / "model"
        
        self.model = None
        self.model_name = None
        self._gguf_process = None
        self._q_in = None
        self._q_out = None
        self.corpus = {}
        self._last_reconstruction_error = 0.0
        self._active_dimension = None
        
        if os.path.exists(offline_model_path) and os.path.exists(manifest_path):
            logging.info("Detected side-loaded model weights. Verifying integrity...")
            self._verify_offline_model(offline_model_path, manifest_path)
            logging.info("Integrity verified. Loading side-loaded model...")
            backend = self._detect_backend(offline_model_path)
            if backend == "gguf":
                self._start_gguf_worker(offline_model_path)
            else:
                self._load_torch_model(offline_model_path, backend)
            self.model_name = "offline_model"
        elif model_path is not None:
            if str(model_path) == str(user_model_path):
                hf_manifest = os.path.join(os.path.dirname(__file__), "hf_manifest.json")
                if os.path.exists(hf_manifest):
                    logging.info("Verifying user downloaded model integrity...")
                    self._verify_hf_model(str(user_model_path), hf_manifest)
            backend = self._detect_backend(model_path)
            if backend == "gguf":
                self._start_gguf_worker(model_path)
            else:
                self._load_torch_model(model_path, backend)
            self.model_name = str(model_path)

    def _load_torch_model(self, path, backend):
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(2)
        self.model = SentenceTransformer(path, backend=backend)

    def _start_gguf_worker(self, path):
        gguf_file = path
        if os.path.isdir(path):
            for root, _, files in os.walk(path):
                for f in files:
                    if f.endswith(".gguf"):
                        gguf_file = os.path.join(root, f)
                        break
        
        self._q_in = mp.Queue()
        self._q_out = mp.Queue()
        self._gguf_process = mp.Process(
            target=gguf_worker_process,
            args=(gguf_file, self._q_in, self._q_out),
            daemon=True
        )
        self._gguf_process.start()

    def terminate(self):
        """Terminate the background GGUF inference process safely."""
        if self._gguf_process and self._gguf_process.is_alive():
            self._gguf_process.terminate()
            self._gguf_process.join()

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
    def active_model_name(self):
        """Get the name of the currently active model."""
        return self.model_name

    @property
    def active_dimension(self):
        """Get the vector dimension of the currently active model."""
        if self.model:
            if hasattr(self.model, "get_embedding_dimension"):
                return self.model.get_embedding_dimension()
            elif hasattr(self.model, "get_sentence_embedding_dimension"):
                return self.model.get_sentence_embedding_dimension()
        return None

    @property
    def last_reconstruction_error(self):
        """Get the last reconstruction error from the underlying model.

        Returns
        -------
        float
            The reconstruction error.
        """
        return self._last_reconstruction_error

    def partial_fit(self, base_dir: str, new_corpus: dict, runtime_settings=None) -> None:
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
            
            keyword_rules = getattr(runtime_settings, "KEYWORD_RULES", {}) if runtime_settings else {}
            learned_rules = getattr(runtime_settings, "LEARNED_RULES", {}) if runtime_settings else {}

            for i, (filepath, text, file_hash) in enumerate(
                zip(filepaths, texts, hashes)
            ):
                # If we don't have a hash, fetch existing from DB so we don't overwrite it with empty
                doc = db.get_document(base_dir, filepath)

                # compute hash if not provided
                if not file_hash:
                    file_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
                    hashes[i] = file_hash

                is_lexical_match = False
                filename_only = os.path.basename(filepath).lower()
                doc_lower = text.lower() if text else ""
                text_to_search = filename_only + " " + doc_lower

                for keyword in keyword_rules.keys():
                    if keyword.strip() and keyword.lower() in text_to_search:
                        is_lexical_match = True
                        break

                if not is_lexical_match and learned_rules:
                    for keyword in learned_rules.keys():
                        if keyword.strip() and keyword.lower() in filename_only:
                            is_lexical_match = True
                            break
                            
                has_historical_target = doc and doc.get("user_verified_target_path")

                if (
                    doc
                    and doc["file_hash"] == file_hash
                    and doc["embedding"] is not None
                    and doc.get("model_name") == self.active_model_name
                    and doc.get("vector_dimension") == self.active_dimension
                ):
                    embeddings[i] = doc["embedding"]
                elif text.startswith("[STATUS:"):
                    embeddings[i] = None
                elif is_lexical_match or has_historical_target:
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

            documents_to_upsert = []
            for filepath, text, file_hash, embedding in zip(
                filepaths, texts, hashes, embeddings
            ):
                documents_to_upsert.append(
                    (base_dir, filepath, file_hash, text, embedding, self.active_model_name, self.active_dimension)
                )
                
            db.upsert_documents(documents_to_upsert)

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

            keyword_rules = getattr(runtime_settings, "KEYWORD_RULES", {}) if runtime_settings else {}
            learned_rules = getattr(runtime_settings, "LEARNED_RULES", {}) if runtime_settings else {}
            
            ai_filenames = []
            ai_documents = []
            ai_embeddings = []
            keyword_plan_files = []
            unsupported_files = []
            historical_overrides = {}
            folder_profiles = {}

            # Map file hashes to their historical targets
            hash_to_target = {}
            for d in docs:
                if len(d) > 4 and d[4] is not None:
                    hash_to_target[d[3]] = d[4]

            for d in docs:
                f, doc, emb = d[0], d[1], d[2]
                file_hash = d[3] if len(d) > 3 else None
                assigned_folder = d[4] if len(d) > 4 else None
                
                target = assigned_folder if assigned_folder is not None else hash_to_target.get(file_hash)
                
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
                    if emb is not None:
                        if target not in folder_profiles:
                            folder_profiles[target] = []
                        folder_profiles[target].append(emb)
                    
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

            folder_centroids = {}
            for folder, embs in folder_profiles.items():
                if embs:
                    centroid = np.mean(embs, axis=0)
                    norm = np.linalg.norm(centroid)
                    if norm > 0:
                        centroid = centroid / norm
                    folder_centroids[folder] = centroid

            SIMILARITY_THRESHOLD = 0.65
            if folder_centroids and ai_embeddings:
                remaining_filenames = []
                remaining_documents = []
                remaining_embeddings = []
                
                for f, doc_text, emb in zip(ai_filenames, ai_documents, ai_embeddings):
                    routed = False
                    if emb is not None:
                        best_folder = None
                        best_score = -1.0
                        
                        norm_emb = np.linalg.norm(emb)
                        emb_normalized = emb / norm_emb if norm_emb > 0 else emb
                        
                        for folder, centroid in folder_centroids.items():
                            score = np.dot(emb_normalized, centroid)
                            if score > best_score:
                                best_score = score
                                best_folder = folder
                                
                        if best_folder and best_score >= SIMILARITY_THRESHOLD:
                            keyword_plan_files.append((f, best_folder, "similarity", "heuristic", None))
                            routed = True
                            
                    if not routed:
                        remaining_filenames.append(f)
                        remaining_documents.append(doc_text)
                        remaining_embeddings.append(emb)
                        
                ai_filenames = remaining_filenames
                ai_documents = remaining_documents
                ai_embeddings = remaining_embeddings

            self._last_reconstruction_error = 0.0

            if self.model is None:
                plan = {f: None for f in ai_filenames}
            elif self.strategy:
                max_depth = getattr(runtime_settings, "MAX_DEPTH", 5) if runtime_settings else 5
                max_features = getattr(runtime_settings, "MAX_FEATURES", 3) if runtime_settings else 3
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

                if runtime_settings and getattr(runtime_settings, "PRESERVE_HIERARCHY", False):
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

            def remove_from_plan(node, target_f):
                for k, v in list(node.items()):
                    if k == target_f:
                        if v is None or (isinstance(v, dict) and (v.get("routed_by") or v.get("extraction_status"))):
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

            from app.core.cache import load_cache
            _, locked_files, _, _ = load_cache(base_dir)
            if locked_files is None:
                locked_files = {}

            compliance_targets = {f: target_folder for f, target_folder, keyword, routed_by, ext_status in keyword_plan_files}

            for f, override_data in historical_overrides.items():
                target_folder, ext_status = override_data
                
                is_conflicted = False
                compliance_path = None
                
                if f in compliance_targets and compliance_targets[f] != target_folder:
                    compliance_path = compliance_targets[f]
                    
                    if f in locked_files and locked_files[f] in (target_folder, compliance_path):
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

    def find_similar(self, base_dir: str, query_text: str, top_k: int = 5) -> list[dict]:
        """Find the most similar documents to a query string using vector search.
        
        Retrieves stored embeddings from the local SQLite database (decrypting them in memory),
        computes pairwise similarity against the query vector, and returns the top matches.
        """
        if not self.model or not query_text.strip():
            return []
            
        try:
            # Generate vector for the search query
            query_embedding = self.model.encode([query_text], show_progress_bar=False)[0]
            
            docs = db.get_all_documents(base_dir)
            if not docs:
                return []
                
            results = []
            
            # Extract and filter records with valid compatible embeddings
            for doc in docs:
                # db.get_all_documents returns: (filepath, extracted_text, embedding, file_hash, user_verified_target_path, model_name, vector_dimension)
                filepath, extracted_text, embedding, file_hash, user_verified_target, model_name, vector_dimension = doc
                
                if (embedding is not None 
                    and model_name == self.active_model_name 
                    and vector_dimension == self.active_dimension):
                    
                    # Compute Cosine Similarity
                    dot_product = np.dot(query_embedding, embedding)
                    norm_q = np.linalg.norm(query_embedding)
                    norm_e = np.linalg.norm(embedding)
                    
                    if norm_q > 0 and norm_e > 0:
                        similarity = dot_product / (norm_q * norm_e)
                    else:
                        similarity = 0.0
                        
                    results.append({
                        "filepath": filepath,
                        "file_hash": file_hash,
                        "similarity": float(similarity),
                        "extracted_text": extracted_text,
                        "assigned_folder": user_verified_target
                    })
                    
            # Sort by highest similarity
            results.sort(key=lambda x: x["similarity"], reverse=True)
            
            return results[:top_k]
            
        except Exception as e:
            logging.error(f"Failed during vector search. Error: {str(e)}", exc_info=True)
            return []
