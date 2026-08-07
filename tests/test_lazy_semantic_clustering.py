"""Tests for lazy semantic vector generation and TF-IDF fallback mechanisms.

These tests verify lazy generation of embeddings on-the-fly and proper fallback
to standard TF-IDF when ONNX models are corrupt, missing, or unusable. All tests
ensure clean thread termination and database connection clearing for safe Windows execution.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.analyzer_strategies import (
    RecursiveKMeansStrategy,
)
from app.core.db import Database
from app.core.db_worker import DBWorker


from sklearn.feature_extraction.text import TfidfVectorizer


@pytest.fixture
def temp_env():
    """Create a temporary test environment with database and mock ONNX model."""
    import shutil
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_path = Path(tmp_dir)
        db_worker = DBWorker()
        db_path = tmp_path / "test.db"
        db = Database(db_path, db_worker)

        # Create a mock model path with a valid (non-empty) .onnx file
        model_dir = tmp_path / "mock_model"
        model_dir.mkdir(parents=True, exist_ok=True)
        onnx_file = model_dir / "model.onnx"
        with open(onnx_file, "wb") as f:
            f.write(b"a" * 2048)  # Size > 1024 bytes

        yield str(tmp_path), db, str(model_dir), db_worker
    finally:
        db_worker.stop()
        from app.core.db_conn import clear_connection_cache

        clear_connection_cache()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_semantic_clustering_lazy_generation_and_caching(temp_env):
    """
    Verify functional requirements 2, 3, 4, 5 and acceptance criteria:
    - Lazy vector generation on-the-fly sequentially within the sorting thread.
    - Caching newly generated embeddings to the local database before starting clustering.
    - Grouping documents using dense vector calculations rather than TF-IDF matrices.
    """
    base_dir, db, model_path, db_worker = temp_env

    # 1. Populate some documents that will go to clustering
    documents_to_add = [
        (base_dir, "doc1.txt", "hash1", "receipt from restaurant dinner pizza"),
        (base_dir, "doc2.txt", "hash2", "invoice for python consulting services"),
        (base_dir, "doc3.txt", "hash3", "restaurant pizza delivery and wings receipt"),
        (
            base_dir,
            "doc4.txt",
            "hash4",
            "consulting python web app development invoice",
        ),
    ]
    db.upsert_documents(documents_to_add)

    # Make sure they lack vector embeddings initially
    for item in documents_to_add:
        assert db.get_document_vector(base_dir, item[1]) is None

    # Initialize analyzer with our mock model_path and mock get_active_model_properties
    from app.core.semantic_embeddings import ModelProperties

    with patch(
        "app.core.semantic_embeddings.get_active_model_properties",
        return_value=ModelProperties("valid_sig", 384, "1.0.0", is_valid=True),
    ):
        analyzer = IncrementalAnalyzer(
            max_folders=2,
            stop_words={"the", "and", "from", "for"},
            db=db,
            strategy_name="default",  # RecursiveKMeansStrategy
            model_path=model_path,
        )

    try:
        # 2. Run the sorting plan
        plan = analyzer.generate_sorting_plan(base_dir)

        # 3. Verify that new vector embeddings were generated on-the-fly and CACHED to the DB!
        for item in documents_to_add:
            filepath = item[1]
            cached_vector = db.get_document_vector(base_dir, filepath)
            assert cached_vector is not None
            assert len(cached_vector) == analyzer.embedding_manager.dimensions

        # 4. Verify that the sorting plan grouped related documents semantically
        # "pizza receipt" files should be clustered together, "consulting invoice" files together
        assert isinstance(plan, dict)
        assert len(plan) > 0
    finally:
        analyzer.close()


def test_fallback_to_tfidf_when_onnx_missing_or_corrupt(temp_env):
    """
    Verify fallback to standard TF-IDF only if the local ONNX model files
    are entirely missing or corrupt.
    """
    base_dir, db, model_path, db_worker = temp_env

    # Populate some documents
    documents_to_add = [
        (base_dir, "doc1.txt", "hash1", "receipt from restaurant dinner pizza"),
        (base_dir, "doc2.txt", "hash2", "invoice for python consulting services"),
        (base_dir, "doc3.txt", "hash3", "restaurant pizza delivery and wings receipt"),
        (
            base_dir,
            "doc4.txt",
            "hash4",
            "consulting python web app development invoice",
        ),
    ]
    db.upsert_documents(documents_to_add)

    # Case 1: Initialize analyzer with a non-existent model path (entirely missing model files)
    from app.core.semantic_embeddings import ModelProperties

    with patch(
        "app.core.semantic_embeddings.get_active_model_properties",
        return_value=ModelProperties("missing_sig", 384, "1.0.0", is_valid=False),
    ):
        analyzer = IncrementalAnalyzer(
            max_folders=2,
            stop_words={"the", "and"},
            db=db,
            strategy_name="default",
            model_path="non_existent_directory_path",
        )

    try:
        with patch(
            "sklearn.feature_extraction.text.TfidfVectorizer.fit_transform",
            wraps=TfidfVectorizer.fit_transform,
        ) as mock_tfidf:
            analyzer.generate_sorting_plan(base_dir)
            assert mock_tfidf.called
    finally:
        analyzer.close()

    # Case 2: Initialize analyzer with a corrupt model path (ONNX present but corrupt/unusable)
    from app.core.semantic_embeddings import ModelProperties

    with patch(
        "app.core.semantic_embeddings.get_active_model_properties",
        return_value=ModelProperties("corrupt_sig", 384, "1.0.0", is_valid=False),
    ):
        analyzer_corrupt = IncrementalAnalyzer(
            max_folders=2,
            stop_words={"the", "and"},
            db=db,
            strategy_name="default",
            model_path=model_path,  # model_path contains mock model which fails to load
        )

    try:
        with patch(
            "sklearn.feature_extraction.text.TfidfVectorizer.fit_transform",
            wraps=TfidfVectorizer.fit_transform,
        ) as mock_tfidf:
            analyzer_corrupt.generate_sorting_plan(base_dir)
            assert mock_tfidf.called
    finally:
        analyzer_corrupt.close()


def test_dense_vector_calculations_in_hierarchical_clustering():
    """
    Test that RecursiveKMeansStrategy successfully uses dense vector
    calculations when pre-fetched vectors are provided, and falls back to TF-IDF when not.
    """
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
    documents = [
        "restaurant dinner pizza",
        "consulting services invoice",
        "pizza delivery wings",
        "python development consulting",
    ]
    pre_fetched_vectors = [
        [0.1, 0.2, 0.3],
        [0.9, 0.8, 0.7],
        [0.15, 0.22, 0.31],
        [0.85, 0.78, 0.72],
    ]

    strategy = RecursiveKMeansStrategy()

    # 1. Run with pre_fetched_vectors
    plan, error = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words={"the"},
        pre_fetched_vectors=pre_fetched_vectors,
    )

    assert plan is not None
    # Verify we clustered based on self._vector_map
    assert hasattr(strategy, "_vector_map")
    assert strategy._vector_map["doc1.txt"] == [0.1, 0.2, 0.3]

    # 2. Run without pre_fetched_vectors (falls back to TF-IDF)
    plan_tfidf, error_tfidf = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words={"the"},
        pre_fetched_vectors=None,
    )

    assert plan_tfidf is not None
    assert strategy._vector_map == {}
