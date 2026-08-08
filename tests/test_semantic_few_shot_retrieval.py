import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import ModelProperties, SemanticEmbeddingManager


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield Path(dir_path)
    from app.core.db_conn import clear_connection_cache
    clear_connection_cache()
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture
def db_worker():
    worker = DBWorker()
    yield worker
    worker.stop()


@pytest.fixture
def db(temp_dir, db_worker):
    database = Database(temp_dir / "test.db", db_worker)
    yield database
    from app.core.db_conn import clear_connection_cache
    clear_connection_cache()


def test_generative_naming_uses_precomputed_vectors_and_zero_decryption(db, temp_dir):
    """
    Verify:
    1. Generative naming queries the SQLite vector table instead of running raw document decryption.
    2. Similarity matching uses cosine similarity against pre-computed ONNX embeddings.
    3. Zero raw text decryption operations are triggered during normal-path semantic matching.
    """
    base_dir = str(temp_dir)
    db.clear(base_dir)

    # Set up active model metadata in the database to prevent verify_active_model from purging the database
    db.set_model_metadata("active_model_signature", "mock_sig_hash_123")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # Insert historical documents
    db.upsert_document(base_dir, "hist1.txt", "hash_h1", "space flight Mars rocket astronauts NASA space agency")
    db.set_user_verified_target(base_dir, "hash_h1", "Space")
    db.upsert_document(base_dir, "hist2.txt", "hash_h2", "gourmet cooking recipe pasta chef cuisine kitchen")
    db.set_user_verified_target(base_dir, "hash_h2", "Cooking")

    # Insert corresponding vectors (384 dimensions)
    vector_space = [1.0] + [0.0] * 383
    vector_cooking = [0.0] + [1.0] * 383
    
    db.upsert_document_vectors(base_dir, [
        ("hist1.txt", vector_space),
        ("hist2.txt", vector_cooking),
    ])

    # Instantiate strategy
    strategy = GenerativeNamingStrategy()
    strategy.set_db_context(db, base_dir)
    strategy.stop_words = {"the", "and"}
    
    # We must set model_path to a truthy existing path to ensure is_mock is False
    strategy.model_path = str(temp_dir)

    # Mock ModelProperties to simulate valid ONNX model matching our signature and dimensions
    valid_properties = ModelProperties(
        signature="mock_sig_hash_123",
        dimensions=384,
        version="1.0.0",
        is_valid=True,
    )

    # We also mock generate_embedding to return a vector strongly aligned with space
    target_vector = [0.9] + [0.0] * 383

    # Set up generator mock on strategy
    strategy.generator = MagicMock()
    strategy._model_initialized = True

    with (
        patch("app.core.semantic_embeddings.get_active_model_properties", return_value=valid_properties),
        patch.object(SemanticEmbeddingManager, "generate_embedding", return_value=target_vector),
        patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as spy_decrypt,
        patch.object(db, "get_all_documents", wraps=db.get_all_documents) as spy_get_all,
        patch.object(strategy, "_run_prompt", return_value="Space Mission Group") as mock_run_prompt,
    ):
        # Current cluster text
        documents = ["rocket launcher launch NASA mission to outer space planets"]
        
        # Invoke generative naming for the cluster
        name = strategy._get_cluster_keywords(documents)

        # 1. Verify correct descriptive folder name returned
        assert name == "Space Mission Group"

        # 2. Verify we did NOT call raw decryption on the database documents during the semantic path
        spy_decrypt.assert_not_called()
        spy_get_all.assert_not_called()

        # 3. Verify exact cosine similarity picked the "Space" exemplar (hist1.txt) over "Cooking"
        assert mock_run_prompt.called
        prompt_passed = mock_run_prompt.call_args[0][0]
        assert "hist1.txt" in prompt_passed
        assert "Space" in prompt_passed
        assert "hist2.txt" not in prompt_passed


def test_generative_naming_fallback_to_keyword_on_missing_embeddings(db, temp_dir):
    """
    Verify:
    1. The system falls back to keyword-based matching if embeddings are corrupted or missing.
    2. Fallback works when the manager is mock, rebuilding, or when vectors are missing.
    """
    base_dir = str(temp_dir)
    db.clear(base_dir)

    # Insert historical document, but DO NOT provide pre-computed vectors
    db.upsert_document(base_dir, "hist1.txt", "hash_h1", "space flight Mars rocket astronauts NASA space agency")
    db.set_user_verified_target(base_dir, "hash_h1", "Space")

    strategy = GenerativeNamingStrategy()
    strategy.set_db_context(db, base_dir)
    strategy.stop_words = {"the", "and"}
    strategy.generator = MagicMock()
    strategy._model_initialized = True

    # 1. Test case: vectors are missing from db
    # Invalidate cache to force decryption in the fallback path
    db.invalidate_cache()
    
    with (
        patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as spy_decrypt,
        patch.object(db, "get_all_documents", wraps=db.get_all_documents) as spy_get_all,
        patch.object(strategy, "_run_prompt", return_value="Space Flight Exploration") as mock_run_prompt,
    ):
        documents = ["rocket launcher launch NASA mission to outer space planets"]
        name = strategy._get_cluster_keywords(documents)
        
        # Verify it fallback and found the match
        assert name == "Space Flight Exploration"
        assert spy_decrypt.called
        assert spy_get_all.called

        prompt_passed = mock_run_prompt.call_args[0][0]
        # Should contain part of the decrypted snippet
        assert "space flight Mars" in prompt_passed

    # 2. Test case: Embedding manager is mock
    # Set up active model metadata to prevent purge
    db.set_model_metadata("active_model_signature", "mock_sig_hash_123")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    db.upsert_document_vectors(base_dir, [("hist1.txt", [1.0] * 384)])
    
    # Invalidate cache to force decryption in the fallback path
    db.invalidate_cache()

    with (
        patch.object(SemanticEmbeddingManager, "is_mock", new_callable=PropertyMock, return_value=True),
        patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as spy_decrypt,
        patch.object(strategy, "_run_prompt", return_value="Space Fallback") as mock_run_prompt,
    ):
        documents = ["rocket launcher launch NASA mission"]
        name = strategy._get_cluster_keywords(documents)
        assert name == "Space Fallback"
        assert spy_decrypt.called


def test_generative_naming_latency_performance_1000_docs(db, temp_dir):
    """
    Verify:
    1. Generative naming duration is under 200 milliseconds for a set of 1,000 historical documents.
    """
    base_dir = str(temp_dir)
    db.clear(base_dir)

    # Set up active model metadata in the database to prevent verify_active_model from purging the database
    db.set_model_metadata("active_model_signature", "mock_sig_hash_perf")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # Insert 1000 historical documents and vectors
    documents_batch = []
    vectors_batch = []
    
    for i in range(1000):
        filepath = f"doc_{i}.txt"
        file_hash = f"hash_{i}"
        target = "Space" if i % 2 == 0 else "Cooking"
        documents_batch.append((base_dir, filepath, file_hash, "some dummy text content here"))
        vectors_batch.append((filepath, [1.0 if i % 2 == 0 else 0.0] + [0.0 if i % 2 == 0 else 1.0] + [0.0] * 382))

    # Fast insertion
    db.upsert_documents(documents_batch)
    for i in range(1000):
        db.set_user_verified_target(base_dir, f"hash_{i}", "Space" if i % 2 == 0 else "Cooking")
    db.upsert_document_vectors(base_dir, vectors_batch)

    # Setup GenerativeNamingStrategy
    strategy = GenerativeNamingStrategy()
    strategy.set_db_context(db, base_dir)
    strategy.stop_words = {"the", "and"}
    strategy.generator = MagicMock()
    strategy._model_initialized = True
    
    # Set model_path to non-empty string directory path
    strategy.model_path = str(temp_dir)

    valid_properties = ModelProperties(
        signature="mock_sig_hash_perf",
        dimensions=384,
        version="1.0.0",
        is_valid=True,
    )

    target_vector = [1.0] + [0.0] * 383

    with (
        patch("app.core.semantic_embeddings.get_active_model_properties", return_value=valid_properties),
        patch.object(SemanticEmbeddingManager, "generate_embedding", return_value=target_vector),
        patch.object(strategy, "_run_prompt", return_value="Fast Semantic Cluster") as mock_run_prompt,
    ):
        # Measure duration of matching and prompt preparation
        start_time = time.perf_counter()
        name = strategy._get_cluster_keywords(["space flight launch astronauts NASA rocket"])
        end_time = time.perf_counter()
        
        duration_ms = (end_time - start_time) * 1000.0
        
        # Verify correct folder name returned
        assert name == "Fast Semantic Cluster"
        # Must be strictly under 200ms
        assert duration_ms < 200.0, f"Generative naming matched too slow: {duration_ms} ms"
        print(f"\n1000 documents generative naming matching duration: {duration_ms:.2f} ms")
