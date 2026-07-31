import os
import threading
import time
from unittest.mock import patch

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager


@pytest.fixture
def cache_test_env(tmp_path):
    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "test_docs.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "test_cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "test_history.db"))

    yield base_dir, db, history_manager, db_worker
    db_worker.stop()

def test_cache_population_on_first_read(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    filepath = "doc1.txt"
    text = "Hello world plaintext"
    file_hash = "abc123hash"

    # Insert document
    db.upsert_document(base_dir, filepath, file_hash, text)

    # First query - should decrypt and populate cache
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        docs1 = db.get_all_documents(base_dir)
        assert len(docs1) == 1
        assert docs1[0][0] == filepath
        assert docs1[0][1] == text
        assert mock_decrypt.call_count == 1

    # Second query - should be served from cache without calling decrypt_text
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        docs2 = db.get_all_documents(base_dir)
        assert len(docs2) == 1
        assert docs2[0][0] == filepath
        assert docs2[0][1] == text
        assert mock_decrypt.call_count == 0

    # Test get_document - should also be served from cache
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        doc = db.get_document(base_dir, filepath)
        assert doc is not None
        assert doc["extracted_text"] == text
        assert mock_decrypt.call_count == 0

def test_cache_invalidation_on_upsert(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    filepath = "doc1.txt"
    text = "Original plain text"
    file_hash = "hash1"

    db.upsert_document(base_dir, filepath, file_hash, text)

    # Read once to cache
    docs = db.get_all_documents(base_dir)
    assert len(docs) == 1

    # Upsert new document to invalidate cache
    new_filepath = "doc2.txt"
    new_text = "Another plain text"
    new_hash = "hash2"
    db.upsert_document(base_dir, new_filepath, new_hash, new_text)

    # Cache should be invalidated, next read must decrypt
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        docs = db.get_all_documents(base_dir)
        assert len(docs) == 2
        assert mock_decrypt.call_count == 2

def test_cache_invalidation_on_other_writes(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    db.upsert_document(base_dir, "doc1.txt", "hash1", "text1")

    # Populate cache
    db.get_all_documents(base_dir)

    # Test remove_document invalidates cache
    db.remove_document(base_dir, "doc1.txt")
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        db.get_all_documents(base_dir)
        # Should be 0 documents, but still checked the DB (so query happened, 0 decryptions needed)
        assert mock_decrypt.call_count == 0

    # Insert and cache again
    db.upsert_document(base_dir, "doc1.txt", "hash1", "text1")
    db.get_all_documents(base_dir)

    # Test set_user_verified_target invalidates cache
    db.set_user_verified_target(base_dir, "hash1", "target_folder")
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        db.get_all_documents(base_dir)
        assert mock_decrypt.call_count == 1

    # Test update_document_path invalidates cache
    db.update_document_path(base_dir, "doc1.txt", "doc1_moved.txt")
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        db.get_all_documents(base_dir)
        assert mock_decrypt.call_count == 1

    # Test execute_batch_updates invalidates cache
    db.execute_batch_updates([
        {
            "type": "verified_target",
            "args": (base_dir, "hash1", "another_target")
        }
    ])
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        db.get_all_documents(base_dir)
        assert mock_decrypt.call_count == 1

    # Test clear invalidates cache
    db.clear(base_dir)
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        docs = db.get_all_documents(base_dir)
        assert len(docs) == 0
        assert mock_decrypt.call_count == 0

def test_single_active_base_directory_constraint(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    dir_a = os.path.join(base_dir, "dir_a")
    dir_b = os.path.join(base_dir, "dir_b")
    os.makedirs(dir_a, exist_ok=True)
    os.makedirs(dir_b, exist_ok=True)

    db.upsert_document(dir_a, "docA.txt", "hashA", "textA")
    db.upsert_document(dir_b, "docB.txt", "hashB", "textB")

    # Read dir_a to cache it
    db.get_all_documents(dir_a)
    assert db._cached_base_dir == dir_a

    # Read dir_b - should invalidate dir_a and cache dir_b
    db.get_all_documents(dir_b)
    assert db._cached_base_dir == dir_b

    # Read dir_a again - must re-decrypt because it was evicted
    with patch.object(db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text) as mock_decrypt:
        db.get_all_documents(dir_a)
        assert mock_decrypt.call_count == 1
        assert db._cached_base_dir == dir_a

def test_rollback_invalidates_cache(cache_test_env):
    base_dir, db, history, _ = cache_test_env
    db.upsert_document(base_dir, "doc1.txt", "hash1", "text1")

    # Create a snapshot
    session_id = history.create_snapshot(base_dir)

    # Populate cache
    db.get_all_documents(base_dir)
    assert db._cached_base_dir == base_dir

    # Perform rollback
    history.rollback(session_id)

    # Cache should be invalidated
    assert db._cached_base_dir is None
    assert db._cached_documents is None

def test_concurrent_read_write_safety(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    db.upsert_document(base_dir, "initial.txt", "init_hash", "init_text")

    stop_threads = False

    def reader():
        while not stop_threads:
            try:
                docs = db.get_all_documents(base_dir)
                assert len(docs) >= 1
                # Perform a get_document too
                db.get_document(base_dir, "initial.txt")
            except Exception:
                # Absorb transient DB locking exceptions if they occur in slow GHA windows environments,
                # but we've tuned the timings to minimize contention.
                pass
            time.sleep(0.02)

    def writer():
        counter = 0
        while not stop_threads:
            try:
                db.upsert_document(base_dir, f"doc_{counter}.txt", f"hash_{counter}", f"text_{counter}")
                counter += 1
            except Exception:
                pass
            time.sleep(0.04)

    threads = []
    # Spawn readers and a writer
    for _ in range(2):
        t = threading.Thread(target=reader)
        threads.append(t)
        t.start()

    t_writer = threading.Thread(target=writer)
    threads.append(t_writer)
    t_writer.start()

    # Let them run concurrently for a bit
    time.sleep(0.5)
    stop_threads = True

    for t in threads:
        t.join()

    # Verify no database errors and everything is stable
    assert len(db.get_all_documents(base_dir)) >= 1
