"""Core semantic analysis module.

This module provides topic modeling functionality.
"""

import logging
import os
import re
from collections import defaultdict

from sklearn.decomposition import MiniBatchNMF
from sklearn.feature_extraction.text import HashingVectorizer

from app.config import settings


class TopicNode:
    """Stateful node in the topic hierarchy, encapsulating an NMF model."""
    
    def __init__(self, max_folders: int, depth: int = 1, prevent_clustering: bool = False):
        self.max_folders = max_folders
        self.depth = depth
        self.prevent_clustering = prevent_clustering
        
        self.model = None
        self.actual_k = None
        self.folder_names = {}
        self.children = {}  # topic_idx -> TopicNode
        
        self.assigned_docs = {}  # filename -> doc
        self.new_docs = {}       # filename -> doc
        self.misc_docs = {}      # filename -> doc
        
    def add_documents(self, docs: dict):
        """Add new documents to the node for processing."""
        self.new_docs.update(docs)
        
    def process(self, vectorizer, index_to_word):
        """Process new documents and update the NMF model incrementally."""
        if not self.new_docs:
            return
            
        total_docs = {**self.assigned_docs, **self.new_docs}
        
        if self.prevent_clustering or self.depth >= 5 or len(total_docs) < 3:
            self.assigned_docs.update(self.new_docs)
            self.new_docs = {}
            return
            
        if self.model is None:
            # Initial fit
            filenames = list(total_docs.keys())
            documents = list(total_docs.values())
            
            X = vectorizer.transform(documents)
            self.actual_k = min(self.max_folders, len(documents) // 2)
            if self.actual_k < 2:
                self.actual_k = 2
                
            self.model = MiniBatchNMF(n_components=self.actual_k, random_state=42)
            doc_topic_matrix = self.model.fit_transform(X)
            
            self._generate_folder_names(index_to_word)
            self._route_documents(filenames, documents, doc_topic_matrix, is_initial=True)
            
            self.assigned_docs = total_docs
            self.new_docs = {}
        else:
            # Incremental update
            filenames = list(self.new_docs.keys())
            documents = list(self.new_docs.values())
            
            X_new = vectorizer.transform(documents)
            
            # Stability Check: ratio of new docs to existing docs
            ratio = len(self.new_docs) / len(self.assigned_docs) if self.assigned_docs else 1.0
            
            if ratio > 0.1:
                # Exceeds change threshold -> selective update
                self.model.partial_fit(X_new)
                self._generate_folder_names(index_to_word)
                
            doc_topic_matrix = self.model.transform(X_new)
            self._route_documents(filenames, documents, doc_topic_matrix, is_initial=False)
            
            self.assigned_docs.update(self.new_docs)
            self.new_docs = {}
            
        # Process children
        for child in self.children.values():
            child.process(vectorizer, index_to_word)
            
    def _generate_folder_names(self, index_to_word):
        topic_word_matrix = self.model.components_
        for topic_idx, topic in enumerate(topic_word_matrix):
            top_indices = topic.argsort()[:-3:-1]
            top_terms = []
            for i in top_indices:
                word = index_to_word.get(i)
                if word:
                    top_terms.append(word.capitalize())
                else:
                    top_terms.append(f"Topic{i}")
            if not top_terms:
                self.folder_names[topic_idx] = "Miscellaneous"
            else:
                self.folder_names[topic_idx] = "-".join(top_terms)

    def _route_documents(self, filenames, documents, doc_topic_matrix, is_initial):
        topic_groups = defaultdict(list)
        
        for i, filename in enumerate(filenames):
            best_topic_idx = doc_topic_matrix[i].argmax()
            if doc_topic_matrix[i][best_topic_idx] == 0:
                self.misc_docs[filename] = documents[i]
            else:
                topic_groups[best_topic_idx].append((filename, documents[i]))
                
        for topic_idx, group in topic_groups.items():
            if topic_idx not in self.children:
                prevent = is_initial and len(group) == len(filenames)
                self.children[topic_idx] = TopicNode(max_folders=self.max_folders, depth=self.depth + 1, prevent_clustering=prevent)
                
            child_new_docs = {f: d for f, d in group}
            self.children[topic_idx].add_documents(child_new_docs)
            
    def get_plan(self):
        """Generate a hierarchical sorting plan from this node."""
        plan = {}
        
        if self.model is None or self.prevent_clustering:
            for f in self.assigned_docs:
                plan[f] = None
            if self.depth == 1:
                return {"Miscellaneous": plan} if plan else {}
            return plan
            
        for topic_idx, child in self.children.items():
            folder_name = self.folder_names.get(topic_idx, "Miscellaneous")
            child_plan = child.get_plan()
            
            if folder_name not in plan:
                plan[folder_name] = {}
            plan[folder_name].update(child_plan)
            
        if self.misc_docs:
            if "Miscellaneous" not in plan:
                plan["Miscellaneous"] = {}
            for f in self.misc_docs:
                plan["Miscellaneous"][f] = None
                
        return plan


class IncrementalAnalyzer:
    """Stateful ML analyzer using incremental topic modeling.
    
    Uses HashingVectorizer and MiniBatchNMF to cluster documents incrementally.
    """

    def __init__(self, max_folders: int) -> None:
        """Initialize the analyzer with the maximum number of folders."""
        self.max_folders = max_folders
        self.n_features = 10000
        self.vectorizer = HashingVectorizer(
            stop_words=list(settings.STOP_WORDS),
            n_features=self.n_features,
            norm=None,
            alternate_sign=False
        )
        self.corpus = {}
        self.index_to_word = {}
        self.root_node = None

    def _update_vocab(self, documents: list) -> None:
        """Update the reverse lookup for HashingVectorizer indices."""
        for doc in documents:
            words = set(re.findall(r'\b[a-zA-Z]{3,}\b', doc.lower()))
            words.difference_update(settings.STOP_WORDS)
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
            
            if self.root_node is None:
                self.root_node = TopicNode(max_folders=self.max_folders, depth=1)
                
            self.root_node.add_documents(new_corpus)
            self.root_node.process(self.vectorizer, self.index_to_word)
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
            if not self.corpus:
                return {}
                
            if self.root_node is None:
                return {}
                
            plan = self.root_node.get_plan()
            
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
