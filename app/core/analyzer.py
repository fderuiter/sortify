"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import re
from collections import defaultdict

from sklearn.decomposition import MiniBatchNMF
from sklearn.feature_extraction.text import HashingVectorizer

from config import MAX_DF, MIN_DF, STOP_WORDS


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
        self.nmf_model = None
        self.corpus = {}
        self.index_to_word = {}
        self.is_fitted = False

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
        self.corpus.update(new_corpus)
        documents = list(new_corpus.values())
        if not documents:
            return

        self._update_vocab(documents)
        X = self.vectorizer.transform(documents)

        if not self.is_fitted:
            actual_k = min(self.max_folders, len(documents) // 2)
            if actual_k < 2:
                actual_k = 2
            self.nmf_model = MiniBatchNMF(n_components=actual_k, random_state=42)
            self.nmf_model.partial_fit(X)
            self.is_fitted = True
        else:
            self.nmf_model.partial_fit(X)

    def generate_sorting_plan(self) -> dict:
        """Generate a sorting plan based on the current model state.
        
        Returns
        -------
        dict
            A mapping where keys are generated folder names and values are lists
            of filenames belonging to that folder.
        """
        plan = defaultdict(list)
        filenames = list(self.corpus.keys())
        documents = list(self.corpus.values())

        if len(documents) < 3 or not self.is_fitted or self.nmf_model is None:
            for f in filenames:
                plan["Miscellaneous"].append(f)
            return dict(plan)

        X = self.vectorizer.transform(documents)
        document_topic_matrix = self.nmf_model.transform(X)
        topic_word_matrix = self.nmf_model.components_

        folder_names = []
        for topic in topic_word_matrix:
            top_indices = topic.argsort()[:-3:-1]
            top_terms = []
            for i in top_indices:
                word = self.index_to_word.get(i, f"Topic{i}")
                top_terms.append(word.capitalize())
            if not top_terms:
                folder_names.append("Miscellaneous")
            else:
                folder_names.append("-".join(top_terms))

        for i, filename in enumerate(filenames):
            best_topic_idx = document_topic_matrix[i].argmax()
            if document_topic_matrix[i][best_topic_idx] == 0:
                plan["Miscellaneous"].append(filename)
            else:
                folder = folder_names[best_topic_idx]
                plan[folder].append(filename)

        return dict(plan)
