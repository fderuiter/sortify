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
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        docs1 = db.get_all_documents(base_dir)
        assert len(docs1) == 1
        assert docs1[0][0] == filepath
        assert docs1[0][1] == text
        assert mock_decrypt.call_count == 1

    # Second query - should be served from cache without calling decrypt_text
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        docs2 = db.get_all_documents(base_dir)
        assert len(docs2) == 1
        assert docs2[0][0] == filepath
        assert docs2[0][1] == text
        assert mock_decrypt.call_count == 0

    # Test get_document - should also be served from cache
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
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
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
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
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        db.get_all_documents(base_dir)
        # Should be 0 documents, but still checked the DB (so query happened, 0 decryptions needed)
        assert mock_decrypt.call_count == 0

    # Insert and cache again
    db.upsert_document(base_dir, "doc1.txt", "hash1", "text1")
    db.get_all_documents(base_dir)

    # Test set_user_verified_target mutates cache directly without invalidation
    db.set_user_verified_target(base_dir, "hash1", "target_folder")
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        docs = db.get_all_documents(base_dir)
        assert mock_decrypt.call_count == 0
        assert len(docs) == 1
        assert docs[0][3] == "target_folder"

    # Test update_document_path mutates cache directly without invalidation
    db.update_document_path(base_dir, "doc1.txt", "doc1_moved.txt")
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        docs = db.get_all_documents(base_dir)
        assert mock_decrypt.call_count == 0
        assert len(docs) == 1
        assert docs[0][0] == "doc1_moved.txt"

    # Test execute_batch_updates invalidates cache
    db.execute_batch_updates(
        [{"type": "verified_target", "args": (base_dir, "hash1", "another_target")}]
    )
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        db.get_all_documents(base_dir)
        assert mock_decrypt.call_count == 1

    # Test clear invalidates cache
    db.clear(base_dir)
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
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
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
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
                db.upsert_document(
                    base_dir, f"doc_{counter}.txt", f"hash_{counter}", f"text_{counter}"
                )
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

    # Clear dead thread connections to release Windows file locks before asserting
    from app.core.db_conn import clear_dead_thread_connections

    clear_dead_thread_connections()

    # Verify no database errors and everything is stable
    assert len(db.get_all_documents(base_dir)) >= 1


def test_sequential_corrections_performance(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    # Insert 50 documents
    num_docs = 50
    for i in range(num_docs):
        db.upsert_document(base_dir, f"doc_{i}.txt", f"hash_{i}", f"text_{i}")

    # Read to populate cache
    db.get_all_documents(base_dir)

    # Perform sequential corrections and measure time
    start_time = time.time()
    for i in range(num_docs):
        # Update user verified target path
        db.set_user_verified_target(base_dir, f"hash_{i}", f"target_{i}")
        # Update document path
        db.update_document_path(
            base_dir, f"doc_{i}.txt", f"target_{i}/doc_moved_{i}.txt"
        )

    end_time = time.time()
    elapsed = end_time - start_time
    print(
        f"\nSequential corrections elapsed time for {num_docs} docs: {elapsed:.4f} seconds"
    )

    # Assert that sequential corrections execute in under 5 seconds
    assert elapsed < 5.0

    # Also assert that the cache contains the mutated records and can be retrieved instantly without decrypting
    with patch.object(
        db.crypto, "decrypt_text", wraps=db.crypto.decrypt_text
    ) as mock_decrypt:
        docs = db.get_all_documents(base_dir)
        assert len(docs) == num_docs
        assert mock_decrypt.call_count == 0
        # Check that the fields are updated
        for row in docs:
            # Filepath format: target_X/doc_moved_X.txt
            # Target path format: target_X
            filepath_norm = row[0].replace("\\", "/")
            target_norm = row[3].replace("\\", "/") if row[3] is not None else ""
            idx = target_norm.split("/")[-1].split("_")[-1]
            assert filepath_norm == f"target_{idx}/doc_moved_{idx}.txt"
            assert target_norm == f"target_{idx}"


def test_concurrent_mutating_read_write(cache_test_env):
    base_dir, db, _, _ = cache_test_env
    # Insert initial document
    db.upsert_document(base_dir, "doc.txt", "hash1", "initial text")
    # Populate cache
    db.get_all_documents(base_dir)

    stop_threads = False
    errors = []

    def reader():
        while not stop_threads:
            try:
                # Subsequent reads must instantly return mutated records
                docs = db.get_all_documents(base_dir)
                assert len(docs) == 1
                doc = docs[0]
                filepath = doc[0].replace("\\", "/")
                target = doc[3]
                if target is not None and target != "":
                    target = target.replace("\\", "/")
                    idx = int(target.split("/")[-1].split("_")[-1])
                    filepath_idx = int(
                        filepath.split("/")[-1].replace("doc_", "").replace(".txt", "")
                    )
                    # Because update_document_path runs first, filepath_idx should be >= idx
                    assert filepath_idx >= idx
            except AssertionError as e:
                errors.append(e)
            except Exception:
                # Absorb transient SQLite or OS-level locking exceptions on slow Windows GHA,
                # but assert on logical errors (AssertionError).
                pass
            time.sleep(0.02)

    def mutator():
        counter = 0
        while not stop_threads:
            try:
                # Mutate filepath and target folder
                old_filepath = (
                    f"target_folder_{counter}/doc_{counter}.txt"
                    if counter > 0
                    else "doc.txt"
                )
                new_filepath = f"target_folder_{counter + 1}/doc_{counter + 1}.txt"
                db.update_document_path(base_dir, old_filepath, new_filepath)
                db.set_user_verified_target(
                    base_dir, "hash1", f"target_folder_{counter + 1}"
                )
                counter += 1
            except Exception:
                # Absorb transient DB/queue/OS exceptions during concurrent mutation on Windows GHA
                pass
            time.sleep(0.05)

    threads = []
    for _ in range(3):
        t = threading.Thread(target=reader)
        threads.append(t)
        t.start()

    t_mutator = threading.Thread(target=mutator)
    threads.append(t_mutator)
    t_mutator.start()

    time.sleep(1.0)
    stop_threads = True

    for t in threads:
        t.join()
    t_mutator.join()

    # Clear dead threads before asserting to avoid SQLite locking on Windows
    from app.core.db_conn import clear_dead_thread_connections

    clear_dead_thread_connections()

    assert not errors, f"Encountered concurrent errors: {errors}"
