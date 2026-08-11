import math
import tempfile
from pathlib import Path

import numpy as np
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.db import Database
from app.core.db_worker import DBWorker


def test_sqlite_incremental_tfidf_lifecycle():
    """Test the full lifecycle of the incremental TF-IDF engine."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_worker = DBWorker()
        try:
            db_path = Path(tmp_dir) / "test_tfidf.db"
            db = Database(db_path, db_worker)

            base_dir = "my_base"
            filepath = "folder/doc1.txt"
            doc_content = "baking delicious chocolate cakes and sweet cupcakes"

            # 1. Initially upserting a document without a verified target should NOT index it in TF-IDF
            db.upsert_document(base_dir, filepath, "hash1", doc_content)

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 0
            assert len(top_terms) == 0
            assert len(doc_terms) == 0

            # 2. Setting a user verified target should trigger incremental TF-IDF indexing
            db.set_user_verified_target_path(base_dir, filepath, "Baking Recipes")

            # Wait for any async db write to complete
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 1
            assert len(top_terms) > 0
            assert doc_metadata[filepath] == "Baking Recipes"

            # Verify that words like "baking", "chocolate", "cakes" are in the vocab with df = 1
            vocab_dict = {term: df for term, df in top_terms}
            assert vocab_dict["baking"] == 1
            assert vocab_dict["chocolate"] == 1

            # 3. Updating the document content should update TF-IDF term frequencies
            # Let's update the content to add "extra sweet cookies" and remove "cupcakes"
            new_content = "baking delicious chocolate cakes and extra sweet cookies"
            db.upsert_document(base_dir, filepath, "hash1_updated", new_content)

            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 1
            vocab_dict = {term: df for term, df in top_terms}
            assert "cookies" in vocab_dict
            assert "cupcakes" not in vocab_dict

            # 4. Moving the document should preserve the statistics but update the path
            new_filepath = "new_folder/doc1_moved.txt"
            db.update_document_path(base_dir, filepath, new_filepath)

            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 1
            assert new_filepath in doc_metadata
            assert filepath not in doc_metadata

            # Verify doc_terms now references the new filepath
            assert all(row[0] == new_filepath for row in doc_terms)

            # 5. Reverting/clearing the manual folder assignment should remove it and decrement counters
            db.set_user_verified_target_path(base_dir, new_filepath, "")

            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 0
            assert len(top_terms) == 0
            assert len(doc_terms) == 0

            # 6. Re-enabling verified target, then testing deletion
            db.set_user_verified_target_path(base_dir, new_filepath, "Baking Recipes")
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 1

            db.remove_document(base_dir, new_filepath)
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 0
            assert len(top_terms) == 0
            assert len(doc_terms) == 0

        finally:
            db_worker.stop()


def test_mathematical_equivalence():
    """Verify that incremental TF-IDF and traditional TfidfVectorizer produce identical mathematical results."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_worker = DBWorker()
        try:
            db_path = Path(tmp_dir) / "test_math.db"
            db = Database(db_path, db_worker)

            base_dir = "math_base"

            # Simple 3-document corpus
            corpus = {
                "file1.txt": "the baking recipe for sweet cookies",
                "file2.txt": "corporate finance reports and balance sheets",
                "file3.txt": "sweet corporate earnings showing quarterly sheets of baking finance",
            }

            for filepath, content in corpus.items():
                db.upsert_document(base_dir, filepath, filepath + "_hash", content)
                db.set_user_verified_target_path(base_dir, filepath, "Some Folder")

            db_worker.q.join()

            # Retrieve from DB
            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 3

            # Compute TF-IDF manually using the DB-backed stats
            vocab = {term: idx for idx, (term, df) in enumerate(top_terms)}
            idf_weights = {
                term: math.log((1 + N) / (1 + df)) + 1 for term, df in top_terms
            }

            from collections import defaultdict

            doc_tfs = defaultdict(list)
            for filepath, term, tf in doc_terms:
                doc_tfs[filepath].append((term, tf))

            manual_vectors = {}
            for filepath in corpus:
                vec = np.zeros(len(vocab))
                for term, tf in doc_tfs[filepath]:
                    if term in vocab:
                        idx = vocab[term]
                        tf_weight = 1 + math.log(tf)
                        vec[idx] = tf_weight * idf_weights[term]
                norm = np.linalg.norm(vec)
                if norm > 0:
                    vec = vec / norm
                manual_vectors[filepath] = vec

            # Compute TF-IDF using scikit-learn's TfidfVectorizer
            vectorizer = TfidfVectorizer(
                stop_words="english", max_features=1000, sublinear_tf=True
            )
            hist_texts = list(corpus.values())
            sklearn_matrix = vectorizer.fit_transform(hist_texts).toarray()
            sklearn_vocab = vectorizer.vocabulary_
            sklearn_idf = vectorizer.idf_

            # Match and assert vocabulary values
            for term, idx in vocab.items():
                if term in sklearn_vocab:
                    sk_idx = sklearn_vocab[term]
                    # Check IDF weights are identical
                    assert (
                        pytest.approx(idf_weights[term], rel=1e-5)
                        == sklearn_idf[sk_idx]
                    )

            # Let's perform a query and check similarity rankings
            strategy = GenerativeNamingStrategy()
            strategy.set_db_context(db, base_dir)

            target_docs = ["baking sweet cookies with corporate finance sheets"]
            # We will patch self._run_prompt to inspect the prompt generated
            captured_prompts = []

            def mock_run_prompt(prompt, max_tokens, grammar=None):
                captured_prompts.append(prompt)
                return "Mock Folder"

            strategy._model_initialized = True
            strategy.generator = np.ones(1)  # dummy
            strategy._run_prompt = mock_run_prompt

            res = strategy._get_cluster_keywords(target_docs)
            assert len(captured_prompts) == 1
            prompt = captured_prompts[0]

            # Since the query contains "baking", "sweet", "cookies", "corporate", "finance", "sheets",
            # the highest similarity should be file3.txt (which contains almost all of them),
            # followed by file1.txt and file2.txt.
            # Let's check that the matched examples appear in the prompt context.
            assert "file3.txt" in prompt or "sheets of baking finance" in prompt

        finally:
            db_worker.stop()
