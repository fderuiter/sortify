"""Defines clustering strategies for grouping documents."""

import logging
import os
import socket
import sys
from collections import defaultdict
from contextlib import contextmanager
from typing import List, Protocol

import numpy as np


@contextmanager
def block_external_network():
    """Block outgoing non-localhost network traffic during naming generation."""
    original_connect = socket.socket.connect

    def safe_connect(self, address):
        if isinstance(address, tuple):
            host = address[0]
            if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
                raise PermissionError(
                    f"External network connections are blocked during folder naming: {host}"
                )
        return original_connect(self, address)

    socket.socket.connect = safe_connect
    try:
        yield
    finally:
        socket.socket.connect = original_connect


class ClusteringStrategy(Protocol):
    """Protocol for defining document clustering strategies."""

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
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
        plan = self._cluster_recursive(filenames, documents, depth=1)
        return plan, self._error

    def _get_cluster_keywords(self, documents: list) -> str:
        if not documents:
            return "Miscellaneous"
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

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

    def _cluster_recursive(self, filenames: list, documents: list, depth: int) -> dict:
        plan = {}

        if depth >= self.max_depth or len(documents) < 3:
            for f in filenames:
                plan[f] = None
            return {"Miscellaneous": plan} if depth == 1 else plan

        try:
            from sklearn.feature_extraction.text import TfidfVectorizer

            vectorizer = TfidfVectorizer(
                stop_words=list(self.stop_words), max_features=1000
            )
            X = vectorizer.fit_transform(documents)
        except Exception:
            for f in filenames:
                plan[f] = None
            return {"Miscellaneous": plan} if depth == 1 else plan

        actual_k = min(self.max_folders, len(documents) // 2)
        if actual_k < 2:
            actual_k = 2

        from sklearn.cluster import MiniBatchKMeans

        kmeans = MiniBatchKMeans(n_clusters=actual_k, random_state=42, n_init="auto")
        labels = kmeans.fit_predict(X)
        self._error += kmeans.inertia_

        topic_groups = defaultdict(list)
        for i, label in enumerate(labels):
            topic_groups[label].append((filenames[i], documents[i]))

        for topic_idx, group in topic_groups.items():
            sub_filenames = [item[0] for item in group]
            sub_documents = [item[1] for item in group]

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
                    sub_filenames, sub_documents, depth + 1
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


class GenerativeNamingStrategy(RecursiveKMeansStrategy):
    """Strategy that uses a generative model to create descriptive folder names."""

    def generate_plan(
        self,
        filenames: List[str],
        documents: List[str],
        max_folders: int,
        stop_words: set,
        max_depth: int = 5,
        max_features: int = 3,
    ) -> tuple[dict, float]:
        """Generate a hierarchical plan of folder names using generative modeling."""
        plan, error = super().generate_plan(
            filenames, documents, max_folders, stop_words, max_depth, max_features
        )

        if not getattr(self, "_model_initialized", False):
            self._init_model()

        if self.generator is None:
            return plan, error

        doc_map = dict(zip(filenames, documents))

        def filter_plan(node, path_name=""):
            new_node = {}
            low_confidence_files = {}
            for k, v in node.items():
                if v is None:
                    doc_text = doc_map.get(k, "")[:1000]
                    prompt = f"Does this document about '{doc_text}' belong in a folder for '{path_name}'? Reply YES or NO."
                    try:
                        import torch

                        torch.set_num_threads(2)
                        if self.task == "text-generation":
                            res = self.generator(
                                prompt,
                                max_new_tokens=5,
                                num_return_sequences=1,
                                return_full_text=False,
                            )
                        else:
                            res = self.generator(
                                prompt, max_new_tokens=5, num_return_sequences=1
                            )
                        answer = res[0]["generated_text"].strip().upper()

                        if "NO" in answer:
                            low_confidence_files[k] = None
                        else:
                            new_node[k] = None
                    except Exception as e:
                        logging.error(f"Coherence check failed: {e}")
                        new_node[k] = None
                elif isinstance(v, dict):
                    folder_name = k if not path_name else f"{path_name} {k}"
                    filtered_v, lc_v = filter_plan(v, path_name=folder_name)
                    if filtered_v:
                        new_node[k] = filtered_v
                    low_confidence_files.update(lc_v)
            return new_node, low_confidence_files

        with block_external_network():
            new_plan, lc_files = filter_plan(plan)

        if lc_files:
            if "Low Confidence" not in new_plan:
                new_plan["Low Confidence"] = {}
            new_plan["Low Confidence"].update(lc_files)

        return new_plan, error

    def __init__(self, model_path: str = None):
        self.generator = None
        self.task = None

        if getattr(sys, "frozen", False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )

        local_bundle_path = os.path.join(base_path, "offline_bundle", "model")

        from app.config import get_app_dir

        user_bundle_path = str(get_app_dir() / "model")

        self.model_path = model_path
        if not self.model_path:
            if os.path.exists(local_bundle_path):
                self.model_path = local_bundle_path
            elif os.path.exists(user_bundle_path):
                self.model_path = user_bundle_path
            else:
                self.model_path = None

        self._model_initialized = False

    def _init_model(self):
        self._model_initialized = True
        if not self.model_path or not os.path.exists(self.model_path):
            logging.warning(
                "Offline model bundle not found in either the local project directory or the user configuration directory."
            )
            return

        try:
            with block_external_network():
                import torch
                from transformers import (
                    AutoModelForCausalLM,
                    AutoModelForSeq2SeqLM,
                    AutoTokenizer,
                    pipeline,
                )

                torch.set_num_threads(2)

                tokenizer = AutoTokenizer.from_pretrained(
                    self.model_path, local_files_only=True
                )
                try:
                    model = AutoModelForSeq2SeqLM.from_pretrained(
                        self.model_path, local_files_only=True
                    )
                    self.task = "text2text-generation"
                except Exception:
                    model = AutoModelForCausalLM.from_pretrained(
                        self.model_path, local_files_only=True
                    )
                    self.task = "text-generation"

                quantized_model = torch.quantization.quantize_dynamic(
                    model, {torch.nn.Linear}, dtype=torch.qint8
                )

                self.generator = pipeline(
                    self.task, model=quantized_model, tokenizer=tokenizer, device=-1
                )
        except Exception as e:
            logging.error(f"Failed to load generative model: {e}")
            self.generator = None

    def _get_cluster_keywords(self, documents: list) -> str:
        if not documents:
            return "Miscellaneous"

        if not getattr(self, "_model_initialized", False):
            self._init_model()

        if self.generator is None:
            return super()._get_cluster_keywords(documents)

        try:
            doc_text = " ".join(documents)[:1000]
            prompt = f"Generate a short, descriptive natural language folder name (1 to 4 words) for a folder containing these documents. Do not use hyphens. Return only the name.\nDocuments: {doc_text}\nFolder Name:"

            with block_external_network():
                import torch

                torch.set_num_threads(2)

                if self.task == "text-generation":
                    res = self.generator(
                        prompt,
                        max_new_tokens=15,
                        num_return_sequences=1,
                        return_full_text=False,
                    )
                else:
                    res = self.generator(
                        prompt, max_new_tokens=15, num_return_sequences=1
                    )

                name = res[0]["generated_text"].strip()

                if not name or len(name) < 2:
                    return super()._get_cluster_keywords(documents)

                name = name.replace('"', "").replace("-", " ").strip()
                return name
        except Exception as e:
            logging.error(f"Generative naming failed: {e}")
            return super()._get_cluster_keywords(documents)


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
clustering_registry.register("generative", GenerativeNamingStrategy())
