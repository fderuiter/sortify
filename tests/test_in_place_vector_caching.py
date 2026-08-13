"""Tests for in-place vector caching for active document sorting.

These tests verify that:
1. The local database contains persistent records of generated active document embeddings immediately after similarity matching.
2. The system performs exactly one embedding generation per unique active document across both similarity and clustering phases.
3. Pipeline execution logs show zero calls to the embedding model for documents that already have cached vectors.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import ModelProperties


@pytest.fixture
def temp_env():
    """Create a temporary test environment with database and mock ONNX model."""
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_path = Path(tmp_dir)
        db_worker = DBWorker()
        db_path = tmp_path / "test_caching.db"
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


def test_in_place_vector_caching_during_similarity(temp_env):
    """
    Verify that:
    1. Active document embeddings are immediately persisted after the similarity phase.
    2. Exactly one generation is performed across both similarity and clustering.
    3. Running the pipeline again results in 0 generation calls.
    """
    base_dir, db, model_path, db_worker = temp_env

    # 1. Populating historical documents (with user_verified_target_path)
    # This ensures similarity matching is active
    historical_docs = [
        (
            base_dir,
            "historical_doc1.txt",
            "hash_hist1",
            "consulting python web app development invoice",
        ),
        (
            base_dir,
            "historical_doc2.txt",
            "hash_hist2",
            "receipt from restaurant dinner pizza wings",
        ),
    ]
    db.upsert_documents(historical_docs)
    db.execute_batch_updates(
        [
            {"type": "verified_target", "args": (base_dir, "hash_hist1", "Consulting")},
            {"type": "verified_target", "args": (base_dir, "hash_hist2", "Pizza")},
        ]
    )

    # 2. Populating active documents (with NO target folder)
    active_docs = [
        (
            base_dir,
            "active_doc1.txt",
            "hash_active1",
            "python consulting services invoice",
        ),
        (base_dir, "active_doc2.txt", "hash_active2", "restaurant pizza wings order"),
    ]
    db.upsert_documents(active_docs)

    # Initially, active files have NO vector embeddings in the database
    assert db.get_document_vector(base_dir, "active_doc1.txt") is None
    assert db.get_document_vector(base_dir, "active_doc2.txt") is None

    # We need historical documents to have valid cached vectors in order for use_semantic to remain True
    # during similarity phase (see lines 358-378 in analyzer.py)
    hist_vectors = [
        ("historical_doc1.txt", [0.1] * 384),
        ("historical_doc2.txt", [0.2] * 384),
    ]
    db.upsert_document_vectors(base_dir, hist_vectors)

    # Set up mocks for transformers and get_active_model_properties
    import sys

    import numpy as np

    mock_tokenizer = MagicMock()
    mock_inputs = {
        "input_ids": np.array([[101, 102, 103]], dtype=np.int64),
        "attention_mask": np.array([[1, 1, 1]], dtype=np.int64),
    }
    mock_tokenizer.return_value = mock_inputs

    mock_transformers = MagicMock()
    mock_transformers.AutoTokenizer.from_pretrained.return_value = mock_tokenizer

    # Set up a mock ONNX session
    mock_session = MagicMock()
    input_node_ids = MagicMock()
    input_node_ids.name = "input_ids"
    input_node_mask = MagicMock()
    input_node_mask.name = "attention_mask"
    mock_session.get_inputs.return_value = [input_node_ids, input_node_mask]

    # dimension 384
    token_embeddings = np.zeros((1, 3, 384), dtype=np.float32)
    token_embeddings[0, 0, 0] = 1.0  # make sure it's non-zero
    mock_session.run.return_value = [token_embeddings]

    with (
        patch.dict(sys.modules, {"transformers": mock_transformers}),
        patch(
            "app.core.shared_registry.SharedModelRegistry.get_onnx_session",
            return_value=mock_session,
        ),
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=ModelProperties("valid_sig", 384, "1.0.0", is_valid=True),
        ),
    ):
        analyzer = IncrementalAnalyzer(
            max_folders=2,
            stop_words={"the", "and", "from", "for"},
            db=db,
            strategy_name="default",  # RecursiveKMeansStrategy
            model_path=model_path,
        )

        # Let's spy on generate_embedding to count the generation calls!
        original_gen = analyzer.embedding_manager.generate_embedding
        call_count = 0
        generated_texts = []

        def spy_generate_embedding(text):
            nonlocal call_count
            call_count += 1
            generated_texts.append(text)
            return original_gen(text)

        analyzer.embedding_manager.generate_embedding = spy_generate_embedding

        try:
            # First Run: Execute similarity matching and clustering
            plan = analyzer.generate_sorting_plan(base_dir)

            # 1. Assert that new vector embeddings were generated for active documents
            # and immediately persisted in the database!
            assert db.get_document_vector(base_dir, "active_doc1.txt") is not None
            assert db.get_document_vector(base_dir, "active_doc2.txt") is not None

            # 2. Assert exactly ONE generation per unique active document across similarity and clustering
            # We have 2 active docs. Since they are cached in the similarity matching phase,
            # the subsequent clustering phase must reuse the cached vectors instead of calling generation.
            # Thus, call_count should be exactly 2!
            assert call_count == 2
            assert "python consulting services invoice" in generated_texts
            assert "restaurant pizza wings order" in generated_texts

            # Clear spy count for a second run
            call_count = 0
            generated_texts.clear()

            # Second Run: All vectors are already in the DB.
            # Pipeline execution should make exactly ZERO calls to generate_embedding!
            plan2 = analyzer.generate_sorting_plan(base_dir)
            assert call_count == 0

        finally:
            analyzer.close()
