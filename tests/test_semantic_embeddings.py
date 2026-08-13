import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import SemanticEmbeddingManager


class MockSettings:
    MAX_FOLDERS = 5
    STOP_WORDS = {"the", "and"}
    AI_CONSENT_GRANTED = True


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


def test_metadata_recording_on_launch(db):
    """Requirement 1: System records model dimensions and profile info in the metadata store on launch."""
    # Initialize embedding manager
    manager = SemanticEmbeddingManager(db, model_path=None)

    # Verify that dimensions and signature were recorded in model_metadata table
    stored_sig = db.get_model_metadata("active_model_signature")
    stored_dim = db.get_model_metadata("active_model_dimensions")
    stored_ver = db.get_model_metadata("active_model_version")

    assert stored_sig == manager.signature
    assert int(stored_dim) == manager.dimensions
    assert stored_ver == manager.version


def test_decoupled_vector_storage(db):
    """Requirement 2: Vector records are stored in a decoupled relation and do not load during standard decryption queries."""
    # Add a document to standard documents table
    db.upsert_document(
        base_dir="/base",
        filepath="doc1.txt",
        file_hash="hash1",
        extracted_text="Some text content here",
    )

    # Insert a vector for this document
    db.upsert_document_vectors("/base", [("doc1.txt", [0.1, 0.2, 0.3])])

    # Perform a standard decryption query (get_all_documents)
    docs = db.get_all_documents("/base")

    # The standard documents query must not load vector data into active memory
    assert len(docs) == 1
    assert docs[0][0] == "doc1.txt"
    assert docs[0][1] == "Some text content here"
    # Ensure vector field is not present in standard document entities
    assert (
        len(docs[0]) == 4
    )  # (filepath, extracted_text, file_hash, user_verified_target_path)

    # Check that vector is retrieved decoupled / separately
    vector = db.get_document_vector("/base", "doc1.txt")
    assert vector == [0.1, 0.2, 0.3]


def test_swapping_model_triggers_purge_and_reconstruction(db, temp_dir):
    """Requirement 3 & 4: Swapping ONNX model triggers automatic deletion of outdated vectors and schedules background recovery."""
    manager = SemanticEmbeddingManager(db, model_path=None)

    # Populate vector records
    db.upsert_document(
        base_dir=str(temp_dir),
        filepath="doc1.txt",
        file_hash="hash1",
        extracted_text="First doc",
    )
    db.upsert_document_vectors(
        str(temp_dir), [("doc1.txt", [0.5] * manager.dimensions)]
    )

    # Verify we have vectors stored
    stored_vector = manager.get_vector(str(temp_dir), "doc1.txt")
    assert stored_vector is not None

    # Now, let's simulate swapping the model by instantiating a manager with different active properties
    # Let's patch get_active_model_properties to return new properties (different signature and dimensions)
    with patch(
        "app.core.semantic_embeddings.get_active_model_properties"
    ) as mock_props:
        mock_props.return_value = ("new_onnx_sig_hash", 128, "2.0.0")

        # Initialize new manager, which triggers verify_active_model on startup
        new_manager = SemanticEmbeddingManager(db, model_path=None)

        # Outdated vector records must be entirely purged immediately
        purged_vector = new_manager.get_vector(str(temp_dir), "doc1.txt")
        assert purged_vector is None

        # Verify new active metadata is updated
        assert db.get_model_metadata("active_model_signature") == "new_onnx_sig_hash"
        assert int(db.get_model_metadata("active_model_dimensions")) == 128
        assert db.get_model_metadata("active_model_version") == "2.0.0"


def test_background_reconstruction_spawns(db, temp_dir):
    """Requirement 4: Background thread is automatically spawned to reconstruct missing embeddings."""
    manager = SemanticEmbeddingManager(db, model_path=None)

    try:
        # Put a document missing vector
        db.upsert_document(
            base_dir=str(temp_dir),
            filepath="doc1.txt",
            file_hash="hash1",
            extracted_text="Content to vectorise",
        )

        # Ensure no vector is present
        assert manager.get_vector(str(temp_dir), "doc1.txt") is None

        # Trigger reconstruction
        manager.trigger_reconstruction(str(temp_dir))

        # Verify background task is non-blocking and eventually finishes generating the vector
        timeout = 30.0
        start_time = time.time()
        while time.time() - start_time < timeout:
            v = manager.get_vector(str(temp_dir), "doc1.txt")
            if v is not None:
                break
            time.sleep(0.1)

        v = manager.get_vector(str(temp_dir), "doc1.txt")
        assert v is not None
        assert len(v) == manager.dimensions

        # Wait for the background thread to finish completely before ending the test
        while manager.is_reconstruction_active():
            time.sleep(0.1)
    finally:
        manager.stop()


def test_graceful_fallback_during_reconstruction(db, temp_dir):
    """Requirement 5: Document sorting/similarity requests completed during background reconstruction fall back gracefully to text similarity."""
    from app.core.analyzer import IncrementalAnalyzer

    analyzer = IncrementalAnalyzer(
        max_folders=5, stop_words={"the"}, db=db, model_path=None
    )

    try:
        # Set background reconstruction to active
        analyzer.embedding_manager._reconstruction_active = True
        with patch.object(
            analyzer.embedding_manager, "is_reconstruction_active", return_value=True
        ):
            # We also patch standard TF-IDF similarity calculation to verify it gets called
            with patch(
                "sklearn.feature_extraction.text.TfidfVectorizer.fit"
            ) as mock_tfidf_fit:
                # Prepare some documents
                db.upsert_documents(
                    [
                        (
                            str(temp_dir),
                            "doc1.txt",
                            "hash1",
                            "Historical document content",
                        ),
                        (
                            str(temp_dir),
                            "doc2.txt",
                            "hash2",
                            "New document content to sort",
                        ),
                    ]
                )
                db.set_user_verified_target(str(temp_dir), "hash1", "FolderA")

                # Try generating sorting plan during reconstruction
                plan = analyzer.generate_sorting_plan(str(temp_dir))

                # It must fall back gracefully to text similarity (TF-IDF vectorizer is called)
                assert mock_tfidf_fit.called
    finally:
        analyzer.close()


def test_memory_throttling_and_low_priority_thread(db, temp_dir):
    """Constraints & Guardrails: Loading document texts for vector updates must be throttled (<= 50 records at once), and run on low priority."""
    manager = SemanticEmbeddingManager(db, model_path=None)

    called_limits = []
    original_get_docs = db.get_documents_missing_vectors

    def spied_get_docs(*args, **kwargs):
        limit = kwargs.get("limit")
        if limit is None and len(args) > 1:
            limit = args[1]
        called_limits.append(limit)
        return original_get_docs(*args, **kwargs)

    db.get_documents_missing_vectors = spied_get_docs
    try:
        # Trigger reconstruction
        manager.trigger_reconstruction(str(temp_dir))

        # Wait for thread to finish
        while manager.is_reconstruction_active():
            time.sleep(0.1)

        # get_documents_missing_vectors must have been called with limit=50 to prevent memory exhaustion
        assert 50 in called_limits
    finally:
        db.get_documents_missing_vectors = original_get_docs
        manager.stop()


def test_real_onnx_inference_pipeline_math(db, temp_dir):
    """Verify that the local ONNX inference pipeline correctly executes tokenization,
    retrieves ONNX sessions with thread limits, performs correct mean pooling with attention mask,
    and returns a mathematically correct L2-normalized vector.
    """
    import sys
    from unittest.mock import MagicMock, patch

    import numpy as np

    # Set up a mock tokenizer
    mock_tokenizer = MagicMock()
    mock_inputs = {
        "input_ids": np.array([[101, 102, 103]], dtype=np.int64),
        "attention_mask": np.array([[1, 1, 0]], dtype=np.int64),
    }
    mock_tokenizer.return_value = mock_inputs

    mock_transformers = MagicMock()
    mock_transformers.AutoTokenizer.from_pretrained.return_value = mock_tokenizer

    # Set up a mock ONNX session
    mock_session = MagicMock()
    # Mock node inputs
    input_node_ids = MagicMock()
    input_node_ids.name = "input_ids"
    input_node_mask = MagicMock()
    input_node_mask.name = "attention_mask"
    mock_session.get_inputs.return_value = [input_node_ids, input_node_mask]

    # Mock output embeddings: token embeddings shape [1, 3, 2]
    # first token: [1.0, 2.0], second token: [3.0, 4.0], third token (masked): [5.0, 6.0]
    token_embeddings = np.array(
        [[[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]], dtype=np.float32
    )
    mock_session.run.return_value = [token_embeddings]

    # Create dummy model dir with a mock onnx file
    model_dir = temp_dir / "mock_model_dir"
    model_dir.mkdir()
    onnx_file = model_dir / "model.onnx"
    onnx_file.write_text("dummy onnx content")

    # Patch AutoTokenizer via sys.modules and get_onnx_session
    with (
        patch.dict(sys.modules, {"transformers": mock_transformers}),
        patch(
            "app.core.shared_registry.SharedModelRegistry.get_onnx_session",
            return_value=mock_session,
        ) as mock_get_sess,
        patch("app.core.semantic_embeddings.get_active_model_properties") as mock_props,
    ):
        mock_props.return_value = ("mock_sig_hash", 2, "1.0.0")

        # Initialize manager with the local path
        manager = SemanticEmbeddingManager(db, model_path=str(model_dir))

        assert manager.is_model_valid is True
        assert manager.dimensions == 2

        # Generate embedding
        embedding = manager.generate_embedding("Test document text")

        # Verify AutoTokenizer was loaded with local_files_only=True
        mock_transformers.AutoTokenizer.from_pretrained.assert_called_once_with(
            str(model_dir), local_files_only=True
        )

        # Verify get_onnx_session was called on the correct onnx file
        mock_get_sess.assert_called_once_with(str(onnx_file))

        # Check the mathematical result of mean pooling & L2 normalization:
        # Mean Pooled = ([1.0, 2.0]*1 + [3.0, 4.0]*1 + [5.0, 6.0]*0) / 2 = [2.0, 3.0]
        # L2 norm of [2, 3] = sqrt(4 + 9) = sqrt(13)
        # Normalized = [2 / sqrt(13), 3 / sqrt(13)]
        expected_norm = np.linalg.norm([2.0, 3.0])
        expected_vector = [2.0 / expected_norm, 3.0 / expected_norm]

        assert len(embedding) == 2
        np.testing.assert_allclose(embedding, expected_vector, rtol=1e-5)


def test_real_onnx_pipeline_unrelated_and_similar_matching(db, temp_dir):
    """Verify that similar documents receive high similarity scores and unrelated receive low scores
    using actual simulated high-fidelity model vectors.
    """
    import sys
    from unittest.mock import MagicMock, patch

    import numpy as np

    # Set up dummy model path
    model_dir = temp_dir / "mock_model_dir"
    model_dir.mkdir()
    onnx_file = model_dir / "model.onnx"
    onnx_file.write_text("dummy onnx content")

    mock_tokenizer = MagicMock()
    mock_session = MagicMock()

    # Input nodes
    input_node = MagicMock()
    input_node.name = "input_ids"
    mock_session.get_inputs.return_value = [input_node]

    # We will simulate high-fidelity semantic representations
    # text_to_embedding maps mock texts to custom token embeddings
    # Similar texts get similar vectors, unrelated get orthogonal/opposite vectors
    def mock_tokenize_side_effect(text, *args, **kwargs):
        # Return a simple input dictionary
        return {"input_ids": np.array([[101]], dtype=np.int64)}

    mock_tokenizer.side_effect = mock_tokenize_side_effect

    mock_transformers = MagicMock()
    mock_transformers.AutoTokenizer.from_pretrained.return_value = mock_tokenizer

    # Define high-fidelity outputs depending on input texts
    # We will use patch to observe what text was passed or simulate based on simple state
    text_embeddings_db = {
        "contract agreement apple": np.array(
            [[[1.0, 0.0]]], dtype=np.float32
        ),  # apples vector
        "contract agreement fruits": np.array(
            [[[0.9, 0.1]]], dtype=np.float32
        ),  # very close to apples
        "space exploration galaxy": np.array(
            [[[0.0, 1.0]]], dtype=np.float32
        ),  # orthogonal vector
    }

    last_text_called = []

    # Wrapper to capture text passed to generate_embedding
    original_generate = SemanticEmbeddingManager.generate_embedding

    with (
        patch.dict(sys.modules, {"transformers": mock_transformers}),
        patch(
            "app.core.shared_registry.SharedModelRegistry.get_onnx_session",
            return_value=mock_session,
        ),
        patch("app.core.semantic_embeddings.get_active_model_properties") as mock_props,
    ):
        mock_props.return_value = ("mock_sig_hash", 2, "1.0.0")
        manager = SemanticEmbeddingManager(db, model_path=str(model_dir))

        def custom_generate(text):
            # Capture what text was passed, and return the matching simulated semantic embedding
            last_text_called.append(text)
            for k, v in text_embeddings_db.items():
                if text == k:
                    # Mock the run outputs for this text
                    mock_session.run.return_value = [v]
                    break
            return original_generate(manager, text)

        with patch.object(manager, "generate_embedding", side_effect=custom_generate):
            v_apple = manager.generate_embedding("contract agreement apple")
            v_fruits = manager.generate_embedding("contract agreement fruits")
            v_galaxy = manager.generate_embedding("space exploration galaxy")

            # Calculate cosine similarities (since they are L2 normalized, dot product is cosine similarity)
            sim_apple_fruits = np.dot(v_apple, v_fruits)
            sim_apple_galaxy = np.dot(v_apple, v_galaxy)

            # Similar documents receive high similarity scores (should be close to 0.9)
            assert sim_apple_fruits > 0.8
            # Unrelated documents receive low similarity scores (should be close to 0.0)
            assert sim_apple_galaxy < 0.2


def test_real_onnx_pipeline_graceful_fallback(db, temp_dir):
    """Verify that when any part of the real ONNX initialization or inference fails,
    the manager gracefully falls back to the deterministic dummy vector generator.
    """
    import sys
    from unittest.mock import patch

    # Set up model path but make tokenizer fail
    model_dir = temp_dir / "faulty_model_dir"
    model_dir.mkdir()
    onnx_file = model_dir / "model.onnx"
    onnx_file.write_text("dummy onnx content")

    with (
        patch.dict(sys.modules, {"transformers": None}),
        patch("app.core.semantic_embeddings.get_active_model_properties") as mock_props,
    ):
        mock_props.return_value = ("faulty_sig_hash", 384, "1.0.0")
        manager = SemanticEmbeddingManager(db, model_path=str(model_dir))

        # Even with ImportError, generate_embedding should succeed and return deterministic dummy vectors
        embedding1 = manager.generate_embedding("fallback test text")
        embedding2 = manager.generate_embedding("fallback test text")
        embedding3 = manager.generate_embedding("different text")

        assert len(embedding1) == 384
        assert embedding1 == embedding2  # deterministic
        assert embedding1 != embedding3  # content-dependent


def test_vector_field_level_encryption_raw_db(db, temp_dir):
    """Verify that vector data is stored as encrypted string in SQLite, but decrypted by get_document_vector."""
    # Write a vector using the API
    db.upsert_document_vectors("/base", [("doc_test.txt", [0.1, 0.2, 0.3, 0.4])])

    # Direct DB select to verify it is NOT plain text (not parseable as plain JSON)
    from app.core.db_conn import get_db_connection

    conn = get_db_connection(db.db_path)
    with conn:
        cursor = conn.execute(
            "SELECT vector FROM document_vectors WHERE filepath = 'doc_test.txt'"
        )
        row = cursor.fetchone()
        assert row is not None
        raw_val = row[0]
        # It must be a string and not parseable as a plain JSON list directly
        assert isinstance(raw_val, str)
        import json

        with pytest.raises((ValueError, TypeError, json.JSONDecodeError)):
            json.loads(raw_val)

        # Decrypt it using db's crypto to verify it is encrypted with session key
        decrypted_raw = db.crypto.decrypt_text(raw_val)
        decrypted_list = json.loads(decrypted_raw)
        assert decrypted_list == [0.1, 0.2, 0.3, 0.4]

    # Retrieval using get_document_vector should automatically decrypt and parse it
    retrieved_vector = db.get_document_vector("/base", "doc_test.txt")
    assert retrieved_vector == [0.1, 0.2, 0.3, 0.4]


def test_vector_unencrypted_purge_on_startup(temp_dir, db_worker):
    """Verify that on startup, the database automatically purges any unencrypted vectors."""
    db_file = temp_dir / "migration_test.db"

    # Create a properly encrypted SQLCipher database first
    database = Database(db_file, db_worker)

    # Write an unencrypted vector directly into the SQLCipher database
    import json

    from app.core.db_conn import clear_connection_cache, get_db_connection

    conn = get_db_connection(str(db_file))
    with conn:
        plain_vector = json.dumps([0.5, 0.6, 0.7])
        conn.execute(
            "INSERT INTO document_vectors (base_dir, filepath, vector) VALUES (?, ?, ?)",
            ("/base", "doc_insecure.txt", plain_vector),
        )
    clear_connection_cache()

    # Now load the database again using Database class, which triggers init_db() and the purge
    database_new = Database(db_file, db_worker)

    # Verify that the unencrypted vector has been purged from the table
    retrieved = database_new.get_document_vector("/base", "doc_insecure.txt")
    assert retrieved is None

    # Connect to verify the row was indeed deleted
    conn2 = get_db_connection(str(db_file))
    with conn2:
        cursor = conn2.execute("SELECT count(*) FROM document_vectors")
        assert cursor.fetchone()[0] == 0
    clear_connection_cache()


def test_global_model_properties_cache_and_thread_safety(temp_dir):
    """Verify that:
    1. Repeated status queries perform exactly zero disk reads/hash checks if model file remains unchanged.
    2. Modifying the model file on disk instantly forces full re-validation of model properties and updates the cache.
    3. Concurrent threads accessing model properties do not deadlock or cause write races.
    """
    import os
    from unittest.mock import patch
    import threading
    from app.core.semantic_embeddings import get_active_model_properties

    # 1. Setup a dummy ONNX model file
    model_dir = temp_dir / "test_cache_model"
    model_dir.mkdir()
    onnx_file = model_dir / "model.onnx"
    onnx_file.write_text("fake onnx file content")

    # Clear cache before testing to ensure a clean state
    from app.core.semantic_embeddings import _model_properties_cache, _model_properties_cache_lock
    with _model_properties_cache_lock:
        _model_properties_cache.clear()

    # We mock 'open' to track how many times the file's contents are read
    open_count = 0
    original_open = open

    def mock_open_fn(file, *args, **kwargs):
        nonlocal open_count
        if str(file) == str(onnx_file):
            open_count += 1
        return original_open(file, *args, **kwargs)

    with patch("builtins.open", side_effect=mock_open_fn):
        # First call: should be a cache miss. File is opened, SHA-256 computed.
        props1 = get_active_model_properties(str(model_dir))
        assert props1.is_valid is False  # valid is false because it's not a real ONNX file, but that's fine
        assert open_count == 1

        # Second call: should be a cache hit. Open should not be called again!
        props2 = get_active_model_properties(str(model_dir))
        assert props2 == props1
        assert open_count == 1

        # 2. Invalidation upon modification
        # Change the modification time on disk
        current_mtime = os.path.getmtime(str(onnx_file))
        new_mtime = current_mtime + 5.0
        os.utime(str(onnx_file), (new_mtime, new_mtime))

        # Third call: should be a cache miss due to mtime change.
        props3 = get_active_model_properties(str(model_dir))
        assert open_count == 2
        assert props3 == props1

        # Fourth call: should be a cache hit again.
        props4 = get_active_model_properties(str(model_dir))
        assert props4 == props3
        assert open_count == 2

    # 3. Concurrent access test to ensure thread-safety and no deadlocks
    # We will spawn multiple threads that call get_active_model_properties concurrently on the same path
    results = []
    def worker():
        for _ in range(50):
            res = get_active_model_properties(str(model_dir))
            results.append(res)

    threads = []
    for _ in range(10):
        t = threading.Thread(target=worker)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    # All threads should have successfully finished, and results should all match
    assert len(results) == 500
    for res in results:
        assert res == props3

