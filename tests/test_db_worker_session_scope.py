"""Integration tests for explicit session transaction scope in DBWorker and Database."""

import os
import tempfile
import time
from pathlib import Path

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.mover import execute_moves


@pytest.fixture
def temp_db_and_worker():
    """Fixture providing a temporary Database instance and DBWorker."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test_session.db"
    worker = DBWorker()
    db = Database(db_path=db_path, worker=worker)
    yield db, worker, temp_dir
    worker.stop()
    try:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


def test_db_worker_session_scope_commit(temp_db_and_worker):
    """Verify explicit session transaction commit persists all batched updates atomically."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir
    session_id = "test_session_commit_1"

    # Pre-populate document in database
    db.upsert_document(base_dir, "doc1.txt", "hash1", "Content 1")
    db.upsert_document(base_dir, "doc2.txt", "hash2", "Content 2")

    # Begin transaction session
    worker.begin_session(session_id, db_path=db.db_path)
    assert worker.is_in_session()
    assert worker.get_active_session_id() == session_id

    # Execute batch 1
    db.execute_batch_updates([
        {
            "type": "document_path",
            "args": (base_dir, "doc1.txt", "moved/doc1.txt"),
        }
    ])

    # Execute batch 2
    db.execute_batch_updates([
        {
            "type": "document_path",
            "args": (base_dir, "doc2.txt", "moved/doc2.txt"),
        }
    ])

    # Commit session
    worker.commit_session(session_id)
    assert not worker.is_in_session()

    # Verify both updates are committed
    doc1 = db.get_document(base_dir, "moved/doc1.txt")
    doc2 = db.get_document(base_dir, "moved/doc2.txt")
    assert doc1 is not None
    assert doc2 is not None


def test_db_worker_session_scope_rollback(temp_db_and_worker):
    """Verify explicit session transaction rollback reverts all updates applied since session start."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir
    session_id = "test_session_rollback_1"

    # Pre-populate documents
    db.upsert_document(base_dir, "file1.txt", "hash1", "Text 1")
    db.upsert_document(base_dir, "file2.txt", "hash2", "Text 2")

    # Begin session
    worker.begin_session(session_id, db_path=db.db_path)

    # Batch 1 update
    db.execute_batch_updates([
        {
            "type": "document_path",
            "args": (base_dir, "file1.txt", "folder/file1.txt"),
        }
    ])

    # Batch 2 update
    db.execute_batch_updates([
        {
            "type": "document_path",
            "args": (base_dir, "file2.txt", "folder/file2.txt"),
        }
    ])

    # Roll back session
    worker.rollback_session(session_id)
    assert not worker.is_in_session()

    # Verify original paths remain and moved paths do not exist in DB
    orig_doc1 = db.get_document(base_dir, "file1.txt")
    orig_doc2 = db.get_document(base_dir, "file2.txt")
    moved_doc1 = db.get_document(base_dir, "folder/file1.txt")
    moved_doc2 = db.get_document(base_dir, "folder/file2.txt")

    assert orig_doc1 is not None
    assert orig_doc2 is not None
    assert moved_doc1 is None
    assert moved_doc2 is None


def test_db_worker_session_auto_rollback_on_task_exception(temp_db_and_worker):
    """Verify task exception during an active session automatically rolls back the transaction."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir
    session_id = "test_session_exception_1"

    db.upsert_document(base_dir, "item1.txt", "hash1", "Payload 1")

    worker.begin_session(session_id, db_path=db.db_path)

    # Apply valid batch 1
    db.execute_batch_updates([
        {
            "type": "document_path",
            "args": (base_dir, "item1.txt", "new_dir/item1.txt"),
        }
    ])

    # Submit a failing task to worker queue
    def _failing_task():
        raise RuntimeError("Simulated DB task failure mid-session")

    with pytest.raises(RuntimeError, match="Simulated DB task failure"):
        worker.execute_write(_failing_task)

    # Session should be automatically rolled back and closed
    assert not worker.is_in_session()

    # Verify item1.txt remains at its original path in DB
    assert db.get_document(base_dir, "item1.txt") is not None
    assert db.get_document(base_dir, "new_dir/item1.txt") is None


def test_db_worker_session_timeout(temp_db_and_worker):
    """Verify safety timeout on open transaction sessions automatically rolls back abandoned sessions."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir
    session_id = "test_session_timeout_1"

    db.upsert_document(base_dir, "data.txt", "hash_data", "Data content")

    # Begin session with 0.1s timeout
    worker.begin_session(session_id, db_path=db.db_path, timeout=0.1)

    db.execute_batch_updates([
        {
            "type": "document_path",
            "args": (base_dir, "data.txt", "timed_out/data.txt"),
        }
    ])

    # Wait for timeout to expire
    time.sleep(0.2)

    # Submit a non-session read/write task to trigger worker timeout check
    def _dummy():
        return True

    worker.execute_write(_dummy)

    # Session should be timed out and rolled back
    assert not worker.is_in_session()
    assert db.get_document(base_dir, "data.txt") is not None
    assert db.get_document(base_dir, "timed_out/data.txt") is None


def test_multi_batch_move_failure_auto_rollback(temp_db_and_worker, monkeypatch):
    """Verify multi-batch execute_moves rolls back database changes when a mid-batch physical move fails."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir

    # Create test source files on disk and records in DB
    os.makedirs(os.path.join(base_dir, "src"), exist_ok=True)
    f1_path = os.path.join(base_dir, "src", "file1.txt")
    f2_path = os.path.join(base_dir, "src", "file2.txt")

    with open(f1_path, "w") as f:
        f.write("Source 1")
    with open(f2_path, "w") as f:
        f.write("Source 2")

    db.upsert_document(base_dir, "src/file1.txt", "h1", "Source 1")
    db.upsert_document(base_dir, "src/file2.txt", "h2", "Source 2")

    # Construct plan to move files to "dst"
    plan = {
        "plan": {
            "dst": {
                "file1.txt": {
                    "__type__": "file",
                    "relative_source": "src/file1.txt",
                    "source_path": f1_path,
                    "target_filename": "file1.txt",
                },
                "file2.txt": {
                    "__type__": "file",
                    "relative_source": "src/file2.txt",
                    "source_path": f2_path,
                    "target_filename": "file2.txt",
                },
            }
        }
    }

    # Monkeypatch shutil.move to fail on file2.txt
    import shutil
    orig_shutil_move = shutil.move

    def mock_failing_move(src, dst):
        if "file2.txt" in src:
            raise PermissionError("Simulated permission error on file2.txt")
        return orig_shutil_move(src, dst)

    monkeypatch.setattr("shutil.move", mock_failing_move)

    with pytest.raises(PermissionError, match="Simulated permission error"):
        execute_moves(
            base_dir=base_dir,
            plan=plan,
            db=db,
            history_manager=None,
            batch_size=1,  # Force batch size of 1 to create multiple batches
        )

    # Verify that database updates for file1 (which moved in batch 1) were completely rolled back!
    doc1 = db.get_document(base_dir, "src/file1.txt")
    doc1_moved = db.get_document(base_dir, "dst/file1.txt")

    assert doc1 is not None, "Original DB record for file1 must remain post-rollback"
    assert doc1_moved is None, "Moved DB record for file1 must be rolled back"


def test_non_session_operations_continue_safely(temp_db_and_worker):
    """Verify non-session database operations continue processing safely without thread contention or blocking."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir

    # Standard write outside session
    db.upsert_document(base_dir, "std.txt", "hash_std", "Standard document")

    doc = db.get_document(base_dir, "std.txt")
    assert doc is not None
    assert doc["file_hash"] == "hash_std"
