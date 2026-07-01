"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

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

    def __init__(self, max_folders: int, stop_words: set, strategy_name: str = "default") -> None:
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        self.stop_words = stop_words
        self.strategy = clustering_registry.get_strategy(strategy_name)
        # Use a small, fast model for embeddings
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.corpus = {}
        self._last_reconstruction_error = 0.0

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
                self.corpus[filepath] = texts[-1] # keep in-memory for UI triggers

            if not texts:
                return

            embeddings = self.model.encode(texts, show_progress_bar=False)
            
            for filepath, text, file_hash, embedding in zip(filepaths, texts, hashes, embeddings):
                # If we don't have a hash, fetch existing from DB so we don't overwrite it with empty
                if not file_hash:
                    doc = db.get_document(base_dir, filepath)
                    if doc:
                        file_hash = doc["file_hash"]
                db.upsert_document(base_dir, filepath, file_hash, text, embedding)

        except Exception as e:
            logging.error(f"Failed during partial_fit. Error: {str(e)}", exc_info=True)

    def reload_stop_words(self, new_stop_words: set) -> None:
        """Reload stop words from config."""
        self.stop_words = new_stop_words

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
                
            filenames = [d[0] for d in docs]
            documents = [d[1] for d in docs]
            embeddings = [d[2] for d in docs]
            
            self._last_reconstruction_error = 0.0
            
            if self.strategy:
                plan, error = self.strategy.generate_plan(filenames, documents, embeddings, self.max_folders, self.stop_words)
                self._last_reconstruction_error = error
            else:
                plan = {}
            
            def _annotate(node, current_path):
                for k, v in list(node.items()):
                    if v is None:
                        filename = os.path.basename(k)
                        target_filename = filename
                        
                        contextual_renaming = False
                        if runtime_settings:
                            contextual_renaming = getattr(runtime_settings, "CONTEXTUAL_RENAMING", False)
                        else:
                            contextual_renaming = False
                            
                        if contextual_renaming:
                            parent_dir = os.path.dirname(k)
                            if parent_dir:
                                parent_folder = os.path.basename(parent_dir)
                                if parent_folder:
                                    safe_parent = re.sub(r'[^A-Za-z0-9]', '_', parent_folder)
                                    target_filename = f"{safe_parent}_{filename}"

                        target_path = os.path.join(current_path, target_filename)
                        
                        norm_source = os.path.normpath(k)
                        norm_target = os.path.normpath(target_path)
                        
                        status = "Already Sorted" if norm_source == norm_target else "Pending Move"
                        
                        node[k] = {
                            "__type__": "file",
                            "status": status,
                            "source_path": k,
                            "target_filename": target_filename
                        }
                    elif isinstance(v, dict):
                        _annotate(v, os.path.join(current_path, k))
            
            _annotate(plan, "")
            return plan
        except Exception as e:
            logging.error(f"Failed during generate_sorting_plan. Error: {str(e)}", exc_info=True)
            return {}
