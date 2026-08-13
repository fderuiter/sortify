"""Tests for in-memory vector failure tracking and background reconstruction overwrite."""

import shutil
import tempfile
import time
from pathlib import Path

import pytest

from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import SemanticEmbeddingManager


@pytest.fixture
def temp_env():
    """Create a temporary test environment with database and db worker."""
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_path = Path(tmp_dir)
        db_worker = DBWorker()
        db_path = tmp_path / "test_failure_tracking.db"
        db = Database(db_path, db_worker)

        # Create a mock model path with a valid (non-empty) .onnx file
        model_dir = tmp_path / "mock_model"
        model_dir.mkdir(parents=True, exist_ok=True)
        onnx_file = model_dir / "model.onnx"
        with open(onnx_file, "wb") as f:
            f.write(b"a" * 2048)

        yield str(tmp_path), db, str(model_dir), db_worker
    finally:
        db_worker.stop()
        clear_connection_cache()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_corrupt_vector_tracking_and_overwrite(temp_env):
    """Verify that corrupt vectors are tracked, bypass NULL-only filters, are overwritten, and cleared."""
    base_dir, db, model_path, db_worker = temp_env

    # 1. Populate a document in the documents table
    documents = [
        (
            base_dir,
            "corrupt_doc.txt",
            "hash_corrupt",
            "this is the content of the corrupt document",
        )
    ]
    db.upsert_documents(documents)

    # Pre-populate model metadata so verify_active_model() does not wipe the table
    db.set_model_metadata("active_model_signature", "default_onnx_sig")
    db.set_model_metadata("active_model_dimensions", "384")
    db.set_model_metadata("active_model_version", "1.0.0")

    # 2. Insert an explicitly invalid/corrupt string into document_vectors
    # Since we want it to fail decryption/deserialization, inserting non-base64/non-JSON data works perfectly.
    conn = get_db_connection(db.db_path)
    with conn:
        conn.execute(
            "INSERT INTO document_vectors (base_dir, filepath, vector) VALUES (?, ?, ?)",
            (base_dir, "corrupt_doc.txt", "this is completely invalid ciphertext"),
        )

    # 3. Assert the tracking set starts empty
    assert len(db.corrupted_vectors) == 0

    # 4. Attempt to retrieve the vector, which should fail decryption/deserialization,
    # return None, and record the failure in-memory.
    vector = db.get_document_vector(base_dir, "corrupt_doc.txt")
    assert vector is None
    assert (base_dir.replace("\\", "/"), "corrupt_doc.txt") in db.corrupted_vectors

    # 5. Initialize the SemanticEmbeddingManager with model_path=None to force fast mock generation
    embedding_manager = SemanticEmbeddingManager(db, model_path=None)

    # Trigger reconstruction and wait briefly for the background thread to finish
    embedding_manager.trigger_reconstruction(base_dir)

    # Wait for reconstruction thread to complete
    start_time = time.time()
    while embedding_manager.is_reconstruction_active() and (
        time.time() - start_time < 5.0
    ):
        time.sleep(0.05)

    # 6. Verify that:
    # - The background worker successfully generated a valid embedding and overwritten the record in-place.
    # - The in-memory failure tracker cleared the successfully reconstructed file paths immediately after successful database update.
    assert len(db.corrupted_vectors) == 0

    # Retrieval should now succeed and return a valid vector of dimensions 384
    restored_vector = db.get_document_vector(base_dir, "corrupt_doc.txt")
    assert restored_vector is not None
    assert embedding_manager.validate_vector_dimension(restored_vector)
    assert len(db.corrupted_vectors) == 0


def test_tracking_collection_size_limit(temp_env):
    """Verify that the in-memory failure tracking set limits its maximum size to prevent memory leaks."""
    base_dir, db, model_path, db_worker = temp_env

    # Try to add 1100 corrupted vectors, but the maximum size limit must be enforced (e.g. at 1000)
    for i in range(1100):
        db.track_corrupted_vector(base_dir, f"doc_{i}.txt")

    assert len(db.corrupted_vectors) == 1000
