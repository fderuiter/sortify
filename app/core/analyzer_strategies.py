"""Defines clustering strategies for grouping documents."""

from collections import defaultdict
from typing import List, Protocol

import numpy as np
from sklearn.cluster import MiniBatchKMeans
from sklearn.feature_extraction.text import TfidfVectorizer


class ClusteringStrategy(Protocol):
    """Protocol for defining document clustering strategies."""

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        embeddings: List[np.ndarray],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
    ) -> tuple[dict, float]:
        """Return the clustering plan and the total reconstruction error."""
        ...


class RecursiveKMeansStrategy:
    """Strategy that uses recursive KMeans to cluster documents."""

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        embeddings: List[np.ndarray],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
    ) -> tuple[dict, float]:
        """Return a hierarchical clustering plan and error using KMeans."""
        self.stop_words = stop_words
        self.max_folders = max_folders
        self.max_depth = max_depth
        self.max_features = max_features
        self._error = 0.0
        plan = self._cluster_recursive(filenames, documents, embeddings, depth=1)
        return plan, self._error

    def _get_cluster_keywords(self, documents: list) -> str:
        if not documents:
            return "Miscellaneous"
        try:
            vectorizer = TfidfVectorizer(
                stop_words=list(self.stop_words), max_features=self.max_features
            )
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

    def _cluster_recursive(
        self, filenames: list, documents: list, embeddings: list, depth: int
    ) -> dict:
        plan = {}

        if depth >= self.max_depth or len(documents) < 3:
            for f in filenames:
                plan[f] = None
            return {"Miscellaneous": plan} if depth == 1 else plan

        X = np.array(embeddings)
        actual_k = min(self.max_folders, len(documents) // 2)
        if actual_k < 2:
            actual_k = 2

        kmeans = MiniBatchKMeans(n_clusters=actual_k, random_state=42, n_init="auto")
        labels = kmeans.fit_predict(X)
        self._error += kmeans.inertia_

        topic_groups = defaultdict(list)
        for i, label in enumerate(labels):
            topic_groups[label].append((filenames[i], documents[i], embeddings[i]))

        for topic_idx, group in topic_groups.items():
            sub_filenames = [item[0] for item in group]
            sub_documents = [item[1] for item in group]
            sub_embeddings = [item[2] for item in group]

            folder_name = self._get_cluster_keywords(sub_documents)
            
            from app.core.path_utils import sanitize_name
            folder_name = sanitize_name(folder_name)

            if len(group) == len(documents):
                for f in sub_filenames:
                    if folder_name not in plan:
                        plan[folder_name] = {}
                    plan[folder_name][f] = None
            else:
                sub_plan = self._cluster_recursive(
                    sub_filenames, sub_documents, sub_embeddings, depth + 1
                )

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


class ClusteringRegistry:
    """Registry for managing and resolving clustering strategies by name."""

    def __init__(self):
        """Initialize the clustering registry with an empty strategy map."""
        self._strategies = {}

    def register(self, name: str, strategy: ClusteringStrategy):
        """Register a new clustering strategy under the given name."""
        self._strategies[name] = strategy

    def get_strategy(self, name: str) -> ClusteringStrategy:
        """Retrieve a clustering strategy by name."""
        return self._strategies.get(name)


clustering_registry = ClusteringRegistry()
clustering_registry.register("default", RecursiveKMeansStrategy())
