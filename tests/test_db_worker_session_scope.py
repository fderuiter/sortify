"""Integration tests for per-batch database commits in DBWorker and Database."""

import os
import tempfile
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


def test_multi_batch_move_failure_keeps_database_matching_disk(
    temp_db_and_worker, monkeypatch
):
    """A move that fails mid-sort leaves each document recorded where its file is.

    Each batch commits on its own (ADR 0008 puts every table in one store, so no
    write lock spans the sort), so file1's committed move stays recorded.
    """
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

    # Make the mover's file move fail on file2.txt
    from app.core import resilient_file_ops

    orig_resilient_move = resilient_file_ops.resilient_move

    def mock_failing_move(src, dst):
        if "file2.txt" in src:
            raise PermissionError("Simulated permission error on file2.txt")
        return orig_resilient_move(src, dst)

    monkeypatch.setattr("app.core.resilient_file_ops.resilient_move", mock_failing_move)

    with pytest.raises(PermissionError, match="Simulated permission error"):
        execute_moves(
            base_dir=base_dir,
            plan=plan,
            db=db,
            history_manager=None,
            batch_size=1,  # Force batch size of 1 to create multiple batches
        )

    on_disk = {
        os.path.relpath(os.path.join(root, name), base_dir).replace(os.sep, "/")
        for root, _dirs, names in os.walk(base_dir)
        for name in names
        if name.endswith(".txt")
    }
    recorded = {
        p
        for p in ("src/file1.txt", "src/file2.txt", "plan/dst/file1.txt")
        if db.get_document(base_dir, p) is not None
    }
    assert recorded == on_disk


def test_non_session_operations_continue_safely(temp_db_and_worker):
    """Verify non-session database operations continue processing safely without thread contention or blocking."""
    db, worker, temp_dir = temp_db_and_worker
    base_dir = temp_dir

    # Standard write outside session
    db.upsert_document(base_dir, "std.txt", "hash_std", "Standard document")

    doc = db.get_document(base_dir, "std.txt")
    assert doc is not None
    assert doc["file_hash"] == "hash_std"
