"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import logging
import os
import re
from collections import defaultdict

from sklearn.decomposition import MiniBatchNMF
from sklearn.feature_extraction.text import HashingVectorizer

from app.config import STOP_WORDS


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling.
    
    Uses HashingVectorizer and MiniBatchNMF to cluster documents incrementally.
    """

    def __init__(self, max_folders: int) -> None:
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        self.n_features = 10000
        self.vectorizer = HashingVectorizer(
            stop_words=list(STOP_WORDS),
            n_features=self.n_features,
            norm=None,
            alternate_sign=False
        )
        self.corpus = {}
        self.index_to_word = {}

    def _update_vocab(self, documents: list) -> None:
        """Update the reverse lookup for HashingVectorizer indices."""
        for doc in documents:
            words = set(re.findall(r'\b[a-zA-Z]{3,}\b', doc.lower()))
            words.difference_update(STOP_WORDS)
            for word in words:
                transformed = self.vectorizer.transform([word])
                indices = transformed.indices
                if len(indices) > 0:
                    self.index_to_word[indices[0]] = word

    def partial_fit(self, new_corpus: dict) -> None:
        """Update the ML model incrementally with new documents."""
        try:
            self.corpus.update(new_corpus)
            documents = list(new_corpus.values())
            if not documents:
                return

            self._update_vocab(documents)
        except Exception as e:
            logging.error(f"Failed during partial_fit. Error: {str(e)}", exc_info=True)

    def generate_sorting_plan(self) -> dict:
        """Generate a sorting plan based on the current model state.
        
        Returns
        -------
        dict
            A nested mapping where keys are generated folder names and values are 
            either dicts (subfolders) or file metadata dicts.
        """
        try:
            filenames = list(self.corpus.keys())
            documents = list(self.corpus.values())
            if not filenames:
                return {}
            plan = self._cluster_recursive(filenames, documents, depth=1)
            
            def _annotate(node, current_path):
                import app.config as config
                for k, v in list(node.items()):
                    if v is None:
                        filename = os.path.basename(k)
                        target_filename = filename
                        
                        if getattr(config, "CONTEXTUAL_RENAMING", False):
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

    def _cluster_recursive(self, filenames: list, documents: list, depth: int) -> dict:
        plan = {}
        
        # Base case
        if depth >= 5 or len(documents) < 3:
            for f in filenames:
                plan[f] = None
            return {"Miscellaneous": plan} if depth == 1 else plan

        X = self.vectorizer.transform(documents)
        actual_k = min(self.max_folders, len(documents) // 2)
        if actual_k < 2:
            actual_k = 2

        nmf = MiniBatchNMF(n_components=actual_k, random_state=42)
        document_topic_matrix = nmf.fit_transform(X)
        topic_word_matrix = nmf.components_

        folder_names = []
        for topic in topic_word_matrix:
            top_indices = topic.argsort()[:-3:-1]
            top_terms = []
            for i in top_indices:
                word = self.index_to_word.get(i)
                if word:
                    top_terms.append(word.capitalize())
                else:
                    top_terms.append(f"Topic{i}")
            if not top_terms:
                folder_names.append("Miscellaneous")
            else:
                folder_names.append("-".join(top_terms))

        topic_groups = defaultdict(list)
        misc_files = []

        for i, filename in enumerate(filenames):
            best_topic_idx = document_topic_matrix[i].argmax()
            if document_topic_matrix[i][best_topic_idx] == 0:
                misc_files.append((filename, documents[i]))
            else:
                topic_groups[best_topic_idx].append((filename, documents[i]))

        for topic_idx, group in topic_groups.items():
            folder_name = folder_names[topic_idx]
            sub_filenames = [item[0] for item in group]
            sub_documents = [item[1] for item in group]

            # Prevent infinite loop if a group captures all documents
            if len(group) == len(documents):
                for f in sub_filenames:
                    if folder_name not in plan:
                        plan[folder_name] = {}
                    plan[folder_name][f] = None
            else:
                sub_plan = self._cluster_recursive(sub_filenames, sub_documents, depth + 1)
                if folder_name not in plan:
                    plan[folder_name] = sub_plan
                else:
                    plan[folder_name].update(sub_plan)

        if misc_files:
            if "Miscellaneous" not in plan:
                plan["Miscellaneous"] = {}
            for f, d in misc_files:
                plan["Miscellaneous"][f] = None

        return plan
