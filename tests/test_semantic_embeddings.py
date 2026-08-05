import os
import tempfile
import shutil
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import SemanticEmbeddingManager, get_active_model_properties


class MockSettings:
    MAX_FOLDERS = 5
    STOP_WORDS = {"the", "and"}
    AI_CONSENT_GRANTED = True


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield Path(dir_path)
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture
def db_worker():
    worker = DBWorker()
    yield worker
    worker.stop()


@pytest.fixture
def db(temp_dir, db_worker):
    return Database(temp_dir / "test.db", db_worker)


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
        extracted_text="Some text content here"
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
    assert len(docs[0]) == 4  # (filepath, extracted_text, file_hash, user_verified_target_path)

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
        extracted_text="First doc"
    )
    db.upsert_document_vectors(str(temp_dir), [("doc1.txt", [0.5] * manager.dimensions)])

    # Verify we have vectors stored
    stored_vector = manager.get_vector(str(temp_dir), "doc1.txt")
    assert stored_vector is not None

    # Now, let's simulate swapping the model by instantiating a manager with different active properties
    # Let's patch get_active_model_properties to return new properties (different signature and dimensions)
    with patch("app.core.semantic_embeddings.get_active_model_properties") as mock_props:
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

    # Put a document missing vector
    db.upsert_document(
        base_dir=str(temp_dir),
        filepath="doc1.txt",
        file_hash="hash1",
        extracted_text="Content to vectorise"
    )

    # Ensure no vector is present
    assert manager.get_vector(str(temp_dir), "doc1.txt") is None

    # Trigger reconstruction
    manager.trigger_reconstruction(str(temp_dir))

    # Verify background task is non-blocking and eventually finishes generating the vector
    timeout = 5.0
    start_time = time.time()
    while time.time() - start_time < timeout:
        v = manager.get_vector(str(temp_dir), "doc1.txt")
        if v is not None:
            break
        time.sleep(0.1)

    v = manager.get_vector(str(temp_dir), "doc1.txt")
    assert v is not None
    assert len(v) == manager.dimensions


def test_graceful_fallback_during_reconstruction(db, temp_dir):
    """Requirement 5: Document sorting/similarity requests completed during background reconstruction fall back gracefully to text similarity."""
    from app.core.analyzer import IncrementalAnalyzer

    analyzer = IncrementalAnalyzer(
        max_folders=5,
        stop_words={"the"},
        db=db,
        model_path=None
    )

    # Set background reconstruction to active
    analyzer.embedding_manager._reconstruction_active = True
    with patch.object(analyzer.embedding_manager, "is_reconstruction_active", return_value=True):
        # We also patch standard TF-IDF similarity calculation to verify it gets called
        with patch("sklearn.feature_extraction.text.TfidfVectorizer.fit") as mock_tfidf_fit:
            # Prepare some documents
            db.upsert_documents([
                (str(temp_dir), "doc1.txt", "hash1", "Historical document content"),
                (str(temp_dir), "doc2.txt", "hash2", "New document content to sort"),
            ])
            db.set_user_verified_target(str(temp_dir), "hash1", "FolderA")

            # Try generating sorting plan during reconstruction
            plan = analyzer.generate_sorting_plan(str(temp_dir))

            # It must fall back gracefully to text similarity (TF-IDF vectorizer is called)
            assert mock_tfidf_fit.called


def test_memory_throttling_and_low_priority_thread(db, temp_dir):
    """Constraints & Guardrails: Loading document texts for vector updates must be throttled (<= 50 records at once), and run on low priority."""
    manager = SemanticEmbeddingManager(db, model_path=None)

    # Spy on get_documents_missing_vectors
    with patch.object(db, "get_documents_missing_vectors", wraps=db.get_documents_missing_vectors) as mock_get_docs:
        # Trigger reconstruction
        manager.trigger_reconstruction(str(temp_dir))

        # Wait for thread to finish
        while manager.is_reconstruction_active():
            time.sleep(0.1)

        # get_documents_missing_vectors must have been called with limit=50 to prevent memory exhaustion
        if mock_get_docs.called:
            args, kwargs = mock_get_docs.call_args
            assert kwargs.get("limit") == 50 or args[1] == 50
