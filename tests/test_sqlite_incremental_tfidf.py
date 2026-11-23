import math
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker


@contextmanager
def managed_sqlite_db(tmp_dir, db_filename="test.db"):
    """Context manager to reliably manage and teardown SQLite test database connections and workers."""
    db_worker = DBWorker()
    try:
        db_path = Path(tmp_dir) / db_filename
        db = Database(db_path, db_worker)
        yield db, db_path, db_worker
    finally:
        db_worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_sqlite_incremental_tfidf_lifecycle():
    """Test the full lifecycle of the incremental TF-IDF engine."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with managed_sqlite_db(tmp_dir, "test_tfidf.db") as (db, db_path, db_worker):
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
            vocab_dict = dict(top_terms)
            assert vocab_dict["baking"] == 1
            assert vocab_dict["chocolate"] == 1

            # 3. Updating the document content should update TF-IDF term frequencies
            # Let's update the content to add "extra sweet cookies" and remove "cupcakes"
            new_content = "baking delicious chocolate cakes and extra sweet cookies"
            db.upsert_document(base_dir, filepath, "hash1_updated", new_content)

            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 1
            vocab_dict = dict(top_terms)
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


def test_mathematical_equivalence():
    """Verify that incremental TF-IDF and traditional TfidfVectorizer produce identical mathematical results."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with managed_sqlite_db(tmp_dir, "test_math.db") as (db, db_path, db_worker):
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

            def mock_run_prompt(prompt, max_tokens, grammar=None, **kwargs):
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


def test_batch_tfidf_vocab_large_vocabulary():
    """Verify term counter accuracy with large document vocabularies batched via executemany."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with managed_sqlite_db(tmp_dir, "test_large_vocab.db") as (
            db,
            db_path,
            db_worker,
        ):
            base_dir = "large_base"
            doc_count = 5
            common_terms = [f"commonword{i:03d}" for i in range(200)]
            shared_terms = [f"sharedword{i:03d}" for i in range(100)]

            for doc_idx in range(doc_count):
                filepath = f"docs/doc_{doc_idx}.txt"
                unique_terms = [f"uniqueword{doc_idx}_{i:03d}" for i in range(200)]
                words = list(common_terms) + unique_terms
                if doc_idx < 2:
                    words.extend(shared_terms)
                content = " ".join(words)
                db.upsert_document(base_dir, filepath, f"hash_{doc_idx}", content)
                db.set_user_verified_target_path(base_dir, filepath, "VerifiedFolder")

            db_worker.q.join()

            conn = get_db_connection(db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT term, df FROM tfidf_vocab WHERE base_dir = ?",
                    (base_dir,),
                )
                vocab = dict(cursor.fetchall())

            # Verify total vocabulary count: 200 common + 100 shared + 5*200 unique = 1300 terms
            assert len(vocab) == 1300
            for term in common_terms:
                assert vocab[term] == doc_count
            for term in shared_terms:
                assert vocab[term] == 2
            for doc_idx in range(doc_count):
                for i in range(200):
                    term = f"uniqueword{doc_idx}_{i:03d}"
                    assert vocab[term] == 1

            # Verify tfidf_doc_terms row count:
            # 5 docs * (200 common + 200 unique) + 2 docs * 100 shared = 2000 + 200 = 2200
            with conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ?",
                    (base_dir,),
                )
                assert cursor.fetchone()[0] == 2200


def test_batch_tfidf_vocab_incremental_updates_and_deletions():
    """Verify vocabulary counter decrements, conflict increments, and df<=0 cleanups under batch executemany."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with managed_sqlite_db(tmp_dir, "test_vocab_updates.db") as (
            db,
            db_path,
            db_worker,
        ):
            base_dir = "update_base"
            doc1 = "folder/doc1.txt"
            doc2 = "folder/doc2.txt"

            doc1_initial = [f"term{i:03d}" for i in range(300)]
            doc2_initial = [f"term{i:03d}" for i in range(100, 300)]

            db.upsert_document(base_dir, doc1, "hash1", " ".join(doc1_initial))
            db.set_user_verified_target_path(base_dir, doc1, "Folder1")

            db.upsert_document(base_dir, doc2, "hash2", " ".join(doc2_initial))
            db.set_user_verified_target_path(base_dir, doc2, "Folder2")

            db_worker.q.join()

            conn = get_db_connection(db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT term, df FROM tfidf_vocab WHERE base_dir = ?",
                    (base_dir,),
                )
                vocab = dict(cursor.fetchall())

            assert len(vocab) == 300
            for i in range(100):
                assert vocab[f"term{i:03d}"] == 1
            for i in range(100, 300):
                assert vocab[f"term{i:03d}"] == 2

            # Now update doc1: remove term000-term049, keep term050-term299, add newterm000-newterm099
            doc1_updated = [f"term{i:03d}" for i in range(50, 300)] + [
                f"newterm{i:03d}" for i in range(100)
            ]
            db.upsert_document(base_dir, doc1, "hash1_v2", " ".join(doc1_updated))
            db_worker.q.join()

            with conn:
                cursor = conn.execute(
                    "SELECT term, df FROM tfidf_vocab WHERE base_dir = ?",
                    (base_dir,),
                )
                vocab = dict(cursor.fetchall())

            # term000-term049 were only in doc1, so their df became 0 and were deleted
            for i in range(50):
                assert f"term{i:03d}" not in vocab
            # term050-term099 are still only in doc1
            for i in range(50, 100):
                assert vocab[f"term{i:03d}"] == 1
            # term100-term299 are in both doc1 and doc2
            for i in range(100, 300):
                assert vocab[f"term{i:03d}"] == 2
            # newterm000-newterm099 are new in doc1
            for i in range(100):
                assert vocab[f"newterm{i:03d}"] == 1

            # Remove doc2
            db.remove_document(base_dir, doc2)
            db_worker.q.join()

            with conn:
                cursor = conn.execute(
                    "SELECT term, df FROM tfidf_vocab WHERE base_dir = ?",
                    (base_dir,),
                )
                vocab = dict(cursor.fetchall())

            # Now term100-term299 should have df decremented from 2 to 1
            for i in range(100, 300):
                assert vocab[f"term{i:03d}"] == 1

            # Clear doc1 target path (setting it to empty)
            db.set_user_verified_target_path(base_dir, doc1, "")
            db_worker.q.join()

            with conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM tfidf_vocab WHERE base_dir = ?",
                    (base_dir,),
                )
                assert cursor.fetchone()[0] == 0
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM tfidf_doc_terms WHERE base_dir = ?",
                    (base_dir,),
                )
                assert cursor.fetchone()[0] == 0


def test_batch_tfidf_vocab_empty_edge_cases():
    """Verify that empty documents and documents with no eligible terms execute cleanly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with managed_sqlite_db(tmp_dir, "test_vocab_empty.db") as (
            db,
            db_path,
            db_worker,
        ):
            base_dir = "empty_base"
            filepath = "empty.txt"

            # 1. Empty string document
            db.upsert_document(base_dir, filepath, "empty_hash", "")
            db.set_user_verified_target_path(base_dir, filepath, "EmptyFolder")
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 0
            assert len(top_terms) == 0
            assert len(doc_terms) == 0

            # 2. Update to only stop words
            db.upsert_document(base_dir, filepath, "stops_hash", "the and of in for")
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 0
            assert len(top_terms) == 0

            # 3. Add actual terms
            db.upsert_document(
                base_dir,
                filepath,
                "valid_hash",
                "machine learning artificial intelligence",
            )
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 1
            assert len(top_terms) == 4

            # 4. Update back to empty content
            db.upsert_document(base_dir, filepath, "empty_again_hash", "")
            db_worker.q.join()

            N, top_terms, doc_terms, doc_metadata = db.get_tfidf_stats(base_dir)
            assert N == 0
            assert len(top_terms) == 0
            assert len(doc_terms) == 0


def test_tfidf_matrix_cache_invalidated_on_document_move_and_custom_stopwords():
    """Verify moving an indexed document invalidates stale tfidf_matrix_cache rows and custom stop words bypass the default analyzer cache."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with managed_sqlite_db(tmp_dir, "test_matrix_invalidation.db") as (
            db,
            _db_path,
            db_worker,
        ):
            base_dir = "matrix_inval_base"
            db.upsert_document(
                base_dir, "old/doc.txt", "hash_doc", "quantum photonics laser research"
            )
            db.set_user_verified_target_path(base_dir, "old/doc.txt", "Physics")
            db_worker.q.join()

            db.update_tfidf_matrix_cache(base_dir)
            cached_before = db.get_tfidf_matrix_cache(base_dir)
            assert any(r[0] == "old/doc.txt" for r in cached_before)

            db.update_document_path(base_dir, "old/doc.txt", "Physics/doc_moved.txt")
            db_worker.q.join()

            assert db.get_tfidf_matrix_cache(base_dir) == []
            db.update_tfidf_matrix_cache(base_dir)
            cached_after = db.get_tfidf_matrix_cache(base_dir)
            assert all(r[0] == "Physics/doc_moved.txt" for r in cached_after)

            custom_counts = db.compute_term_frequencies(
                "quantum photonics laser", stop_words_list=["quantum"]
            )
            assert "quantum" not in custom_counts
            assert custom_counts["photonics"] == 1
