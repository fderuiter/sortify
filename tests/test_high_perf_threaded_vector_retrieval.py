"""High-Performance Threaded Vector Retrieval and Database-Level Filtering Tests."""

import os
import shutil
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.shared_registry import SharedWorkerPool


@pytest.fixture
def high_perf_env():
    """Create a temporary environment for high performance vector tests."""
    tmp_dir = tempfile.mkdtemp()
    try:
        tmp_path = Path(tmp_dir)
        db_worker = DBWorker()
        db_path = tmp_path / "test_high_perf.db"
        db = Database(db_path, db_worker)
        yield str(tmp_path), db, db_worker
    finally:
        db_worker.stop()
        from app.core.db_conn import clear_connection_cache
        clear_connection_cache()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_db_level_extension_filtering(high_perf_env):
    """Requirement 1: Exclude files with unsupported extensions directly in the SQL queries."""
    _, db, _ = high_perf_env
    base_dir = "/base"

    # Insert active model signature
    db.set_model_metadata("active_model_signature", "active_sig")

    # Insert document vectors: one supported (.txt) and one unsupported (.zip)
    supported_file = "doc.txt"
    unsupported_file = "archive.zip"

    db.upsert_document_vectors(base_dir, [
        (supported_file, [0.1, 0.2, 0.3]),
        (unsupported_file, [0.4, 0.5, 0.6]),
    ], model_signature="active_sig")

    # Clear caches to force query
    with db._vector_cache_lock:
        db._vector_cache.clear()
        db._preloaded_vector_base_dirs.clear()

    # Preload and verify that ONLY the supported file has been preloaded into the cache
    db.preload_document_vectors(base_dir)

    with db._vector_cache_lock:
        # Cache must contain the supported file
        assert (base_dir, supported_file) in db._vector_cache
        # Cache must NOT contain the unsupported file because of SQL-level filtering
        assert (base_dir, unsupported_file) not in db._vector_cache

    # Also verify that calling get_document_vector on the unsupported file returns None immediately
    # without querying database (we can verify zero fetch rate / bypass)
    with patch("app.core.db.get_db_connection") as mock_conn:
        v = db.get_document_vector(base_dir, unsupported_file)
        assert v is None
        # Should not call database connection at all because it is unsupported!
        mock_conn.assert_not_called()


def test_parallel_vector_decryption_and_parsing(high_perf_env):
    """Requirement 2: Concurrent vector decryption and parsing using SharedWorkerPool."""
    _, db, _ = high_perf_env
    base_dir = "/base"

    db.set_model_metadata("active_model_signature", "active_sig")

    # Upsert a batch of vectors
    vectors_batch = [(f"doc_{i}.txt", [float(i)] * 10) for i in range(10)]
    db.upsert_document_vectors(base_dir, vectors_batch, model_signature="active_sig")

    # Clear memory caches
    with db._vector_cache_lock:
        db._vector_cache.clear()
        db._preloaded_vector_base_dirs.clear()

    # Spy on SharedWorkerPool.submit to verify that tasks are offloaded to background worker threads
    worker_pool = SharedWorkerPool.get_instance()
    with patch.object(worker_pool, "submit", wraps=worker_pool.submit) as mock_submit:
        db.preload_document_vectors(base_dir)
        # Should have submitted parallel tasks to the pool
        assert mock_submit.call_count >= 10

    # Ensure all vectors are correctly decrypted and in cache
    for i in range(10):
        v = db.get_document_vector(base_dir, f"doc_{i}.txt")
        assert v == [float(i)] * 10


def test_memory_cache_retrieval(high_perf_env):
    """Requirement 4: Consecutive requests bypass the database and pull directly from cache."""
    _, db, _ = high_perf_env
    base_dir = "/base"

    db.set_model_metadata("active_model_signature", "active_sig")
    db.upsert_document_vectors(base_dir, [("doc.txt", [1.0, 2.0, 3.0])], model_signature="active_sig")

    # Clear caches to start clean
    with db._vector_cache_lock:
        db._vector_cache.clear()
        db._preloaded_vector_base_dirs.clear()

    # Preload once
    db.preload_document_vectors(base_dir)

    # Now, mock the get_db_connection function to raise an error if any DB connection is attempted
    with patch("app.core.db.get_db_connection", side_effect=AssertionError("DB called!")):
        # Retrieval should succeed entirely from memory cache without hitting database!
        v = db.get_document_vector(base_dir, "doc.txt")
        assert v == [1.0, 2.0, 3.0]


def test_model_signature_compatibility(high_perf_env):
    """Requirement 3: Flag and ignore vectors that do not match the active model signature."""
    _, db, _ = high_perf_env
    base_dir = "/base"

    # Set active model signature
    db.set_model_metadata("active_model_signature", "new_active_sig")

    # Upsert two vectors: one with matching signature, one with stale signature
    db.upsert_document_vectors(base_dir, [
        ("matching.txt", [1.0, 1.0]),
    ], model_signature="new_active_sig")

    # Use low level write or bypass upsert cache for stale signature insertion
    def _write_stale():
        import json
        from app.core.db_conn import get_db_connection
        conn = get_db_connection(db.db_path)
        with conn:
            enc_vector = db.crypto.encrypt_vector(json.dumps([2.0, 2.0])).decode("utf-8")
            conn.execute(
                "INSERT INTO document_vectors (base_dir, filepath, vector, model_signature) VALUES (?, ?, ?, ?)",
                (base_dir, "stale.txt", enc_vector, "old_stale_sig")
            )
    db.worker.execute_write(_write_stale)

    # Clear memory cache to force reload
    with db._vector_cache_lock:
        db._vector_cache.clear()
        db._preloaded_vector_base_dirs.clear()

    # Retrieval of matching vector with verify_signature=True should succeed
    v_match = db.get_document_vector(base_dir, "matching.txt", verify_signature=True)
    assert v_match == [1.0, 1.0]

    # Retrieval of stale vector with verify_signature=True should return None (ignored/rejected)
    v_stale = db.get_document_vector(base_dir, "stale.txt", verify_signature=True)
    assert v_stale is None
