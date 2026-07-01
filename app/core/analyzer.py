"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import logging
import os
from collections import defaultdict

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import MiniBatchKMeans
from sklearn.feature_extraction.text import TfidfVectorizer

from app.config import settings
from app.core.db import db


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling.
    
    Uses SentenceTransformer and MiniBatchKMeans to cluster documents incrementally.
    """

    def __init__(self, max_folders: int) -> None:
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        # Use a small, fast model for embeddings
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
        self.corpus = {}

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

    def generate_sorting_plan(self, base_dir: str) -> dict:
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
            
            plan = self._cluster_recursive(filenames, documents, embeddings, depth=1)
            
            def _annotate(node, current_path):
                for k, v in list(node.items()):
                    if v is None:
                        filename = os.path.basename(k)
                        target_path = os.path.join(current_path, filename)
                        
                        norm_source = os.path.normpath(k)
                        norm_target = os.path.normpath(target_path)
                        
                        status = "Already Sorted" if norm_source == norm_target else "Pending Move"
                        
                        node[k] = {
                            "__type__": "file",
                            "status": status,
                            "source_path": k
                        }
                    elif isinstance(v, dict):
                        _annotate(v, os.path.join(current_path, k))
            
            _annotate(plan, "")
            return plan
        except Exception as e:
            logging.error(f"Failed during generate_sorting_plan. Error: {str(e)}", exc_info=True)
            return {}

    def _get_cluster_keywords(self, documents: list) -> str:
        if not documents:
            return "Miscellaneous"
        try:
            vectorizer = TfidfVectorizer(stop_words=list(settings.STOP_WORDS), max_features=3)
            X = vectorizer.fit_transform(documents)
            feature_names = vectorizer.get_feature_names_out()
            if len(feature_names) == 0:
                return "Miscellaneous"
            
            scores = np.asarray(X.sum(axis=0)).ravel()
            top_indices = scores.argsort()[::-1][:2]
            top_terms = [feature_names[i].capitalize() for i in top_indices]
            return "-".join(top_terms)
        except Exception:
            return "Miscellaneous"

    def _cluster_recursive(self, filenames: list, documents: list, embeddings: list, depth: int) -> dict:
        plan = {}
        
        # Base case
        if depth >= 5 or len(documents) < 3:
            for f in filenames:
                plan[f] = None
            return {"Miscellaneous": plan} if depth == 1 else plan

        X = np.array(embeddings)
        actual_k = min(self.max_folders, len(documents) // 2)
        if actual_k < 2:
            actual_k = 2

        kmeans = MiniBatchKMeans(n_clusters=actual_k, random_state=42, n_init="auto")
        labels = kmeans.fit_predict(X)

        topic_groups = defaultdict(list)
        for i, label in enumerate(labels):
            topic_groups[label].append((filenames[i], documents[i], embeddings[i]))

        for topic_idx, group in topic_groups.items():
            sub_filenames = [item[0] for item in group]
            sub_documents = [item[1] for item in group]
            sub_embeddings = [item[2] for item in group]
            
            folder_name = self._get_cluster_keywords(sub_documents)

            # Prevent infinite loop if a group captures all documents
            if len(group) == len(documents):
                for f in sub_filenames:
                    if folder_name not in plan:
                        plan[folder_name] = {}
                    plan[folder_name][f] = None
            else:
                sub_plan = self._cluster_recursive(sub_filenames, sub_documents, sub_embeddings, depth + 1)
                
                def deep_update(d, u):
                    for k, v in u.items():
                        if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                            deep_update(d[k], v)
                        else:
                            d[k] = v
                            
                if folder_name not in plan:
                    plan[folder_name] = sub_plan
                else:
                    deep_update(plan[folder_name], sub_plan)

        return plan
