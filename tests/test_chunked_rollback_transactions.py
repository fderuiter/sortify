"""Tests for chunked rollback transactions with intermediate savepoints."""

import os
import shutil
from unittest.mock import patch

import pytest

import app.core.history as hist_mod
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager, RollbackBatchTransaction


@pytest.fixture
def history_env(tmp_path):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    db_path = tmp_path / "docs.db"
    cache_path = tmp_path / "cache.db"
    history_db_path = tmp_path / "history.db"

    clear_connection_cache()
    db_worker = DBWorker()
    db = Database(db_path=str(db_path), worker=db_worker)
    db.init_db()

    cache_mgr = CacheManager(str(cache_path), worker=db_worker)
    history_mgr = HistoryManager(
        db=db, cache_manager=cache_mgr, db_path=str(history_db_path)
    )

    yield str(base_dir), db, history_mgr, db_worker

    db_worker.stop()
    clear_connection_cache()


def test_batch_transaction_unit_behavior():
    """Unit test RollbackBatchTransaction savepoint lifecycle and batching."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE docs (id INT PRIMARY KEY, path TEXT)")
    conn.commit()

    batch = RollbackBatchTransaction(conn, batch_size=3)

    # 1. First 2 operations do not commit batch yet
    batch.execute("INSERT INTO docs VALUES (1, 'p1')")
    batch.step()
    batch.execute("INSERT INTO docs VALUES (2, 'p2')")
    batch.step()
    assert batch.op_count == 2
    assert batch.current_savepoint is not None

    # 2. Third operation hits batch_size and triggers commit_batch
    batch.execute("INSERT INTO docs VALUES (3, 'p3')")
    batch.step()
    assert batch.op_count == 0
    assert batch.current_savepoint is None
    assert batch.total_committed == 3

    # 3. Add 4th operation and flush
    batch.execute("INSERT INTO docs VALUES (4, 'p4')")
    batch.step()
    assert batch.op_count == 1
    batch.flush()
    assert batch.op_count == 0
    assert batch.current_savepoint is None
    assert batch.total_committed == 4

    rows = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [1, 2, 3, 4]

    # 4. Test rollback_batch isolates errors
    batch.execute("INSERT INTO docs VALUES (5, 'p5')")
    batch.rollback_batch()
    assert batch.op_count == 0
    assert batch.current_savepoint is None

    rows_after = conn.execute("SELECT id FROM docs ORDER BY id").fetchall()
    assert [r[0] for r in rows_after] == [1, 2, 3, 4]


def test_rollback_commits_in_batches_of_fifty(history_env):
    """Multi-file rollback commits database updates in chunked batches of 50."""
    base_dir, db, history_mgr, _ = history_env

    file_count = 125
    for i in range(file_count):
        fname = f"file_{i:03d}.txt"
        fpath = os.path.join(base_dir, fname)
        with open(fpath, "w") as f:
            f.write(f"data {i}")
        db.upsert_document(base_dir, fname, f"hash_{i}", f"data {i}")

    session_id = history_mgr.create_snapshot(base_dir)

    # Move all files into a subfolder
    sub_dir = os.path.join(base_dir, "organized")
    os.makedirs(sub_dir, exist_ok=True)
    for i in range(file_count):
        fname = f"file_{i:03d}.txt"
        shutil.move(os.path.join(base_dir, fname), os.path.join(sub_dir, fname))

    # Track commits on the docs database connection
    orig_get_db = hist_mod.get_db_connection
    commit_count = 0

    def proxy_get_db(path):
        conn = orig_get_db(path)
        if path == db.db_path:

            class ConnProxy:
                def __init__(self, target):
                    self._target = target

                def commit(self):
                    nonlocal commit_count
                    commit_count += 1
                    return self._target.commit()

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    if exc_type is None:
                        self.commit()
                    else:
                        self._target.rollback()

                def __getattr__(self, name):
                    return getattr(self._target, name)

            return ConnProxy(conn)
        return conn

    with patch("app.core.history.get_db_connection", side_effect=proxy_get_db):
        history_mgr.rollback(session_id)

    # 125 files:
    # 1 pre-sync commit + batch 1 (50 files) + batch 2 (50 files) + flush remainder (25 files)
    # Total commits should be at most 4-5 instead of 125+
    assert commit_count <= 5
    # Reduction is over 95% compared to 125 per-file commits
    assert (125 - commit_count) / 125 > 0.95

    # Verify all 125 files restored on disk
    for i in range(file_count):
        fname = f"file_{i:03d}.txt"
        assert os.path.exists(os.path.join(base_dir, fname))
        assert not os.path.exists(os.path.join(sub_dir, fname))

    # Verify database records reflect restored state
    docs_conn = get_db_connection(db.db_path)
    rows = docs_conn.execute(
        "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
    ).fetchall()
    restored_paths = {r[0] for r in rows}
    for i in range(file_count):
        assert f"file_{i:03d}.txt" in restored_paths


def test_partial_failure_preserves_prior_batches(history_env):
    """When a file move fails midway, prior completed batches remain committed."""
    base_dir, db, history_mgr, _ = history_env

    # 60 files: batch 1 will be 50 files, batch 2 will fail on file #55
    file_count = 60
    for i in range(file_count):
        fname = f"file_{i:03d}.txt"
        fpath = os.path.join(base_dir, fname)
        with open(fpath, "w") as f:
            f.write(f"data {i}")
        db.upsert_document(base_dir, fname, f"hash_{i}", f"data {i}")

    session_id = history_mgr.create_snapshot(base_dir)

    sub_dir = os.path.join(base_dir, "organized")
    os.makedirs(sub_dir, exist_ok=True)
    for i in range(file_count):
        fname = f"file_{i:03d}.txt"
        shutil.move(os.path.join(base_dir, fname), os.path.join(sub_dir, fname))

    # Mock _robust_move to fail on file_055.txt
    from app.core.history import _robust_move

    orig_robust_move = _robust_move

    def mock_move(src, dst):
        if "file_055.txt" in src or "file_055.txt" in dst:
            raise PermissionError("Simulated permission denied on file_055")
        return orig_robust_move(src, dst)

    with patch("app.core.history._robust_move", side_effect=mock_move):
        with pytest.raises(PermissionError, match="file_055"):
            history_mgr.rollback(session_id)

    # Validate state:
    # 1. Batch 1 files (0..49) were restored to disk and committed to DB
    docs_conn = get_db_connection(db.db_path)
    rows = docs_conn.execute(
        "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
    ).fetchall()
    restored_paths = {r[0] for r in rows}

    for i in range(50):
        fname = f"file_{i:03d}.txt"
        assert os.path.exists(os.path.join(base_dir, fname))
        assert fname in restored_paths

    # 2. File 55 failed: remains in organized folder
    assert os.path.exists(os.path.join(sub_dir, "file_055.txt"))
    assert not os.path.exists(os.path.join(base_dir, "file_055.txt"))

    # 3. Successfully restored files in batch 2 prior to file 55 also committed
    for i in range(50, 55):
        fname = f"file_{i:03d}.txt"
        assert os.path.exists(os.path.join(base_dir, fname))
        assert fname in restored_paths


def test_blocking_parents_uses_batch_transaction_context(history_env):
    """_move_aside_blocking_parents executes within the batch transaction context."""
    base_dir, db, history_mgr, _ = history_env

    # Setup: folder/file.txt originally existed
    orig_path = os.path.join(base_dir, "myfolder", "file.txt")
    os.makedirs(os.path.dirname(orig_path), exist_ok=True)
    with open(orig_path, "w") as f:
        f.write("content")
    db.upsert_document(base_dir, "myfolder/file.txt", "hash", "content")

    session_id = history_mgr.create_snapshot(base_dir)

    # Move file to other location
    target_path = os.path.join(base_dir, "other", "file.txt")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.move(orig_path, target_path)
    os.rmdir(os.path.join(base_dir, "myfolder"))

    # Now create a file where the folder 'myfolder' needs to go (blocking parent)
    blocking_file = os.path.join(base_dir, "myfolder")
    with open(blocking_file, "w") as f:
        f.write("blocking file")
    db.upsert_document(base_dir, "myfolder", "blocking_hash", "blocking")

    # Rollback should move the blocking file aside using the batch transaction context
    history_mgr.rollback(session_id)

    # Verify original file restored
    assert os.path.exists(orig_path)
    # Verify blocking file was moved aside (e.g. myfolder (1))
    assert os.path.isdir(os.path.join(base_dir, "myfolder"))

    docs_conn = get_db_connection(db.db_path)
    rows = docs_conn.execute(
        "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
    ).fetchall()
    paths = {r[0] for r in rows}
    assert "myfolder/file.txt" in paths
    # The blocking parent was updated to safe path (e.g., myfolder_1)
    safe_rel_entries = [p for p in paths if p.startswith("myfolder_")]
    assert len(safe_rel_entries) == 1


def test_benchmark_five_hundred_files_commit_reduction(history_env):
    """Rollback execution on 500 files decreases commits by over 95%."""
    base_dir, db, history_mgr, _ = history_env

    file_count = 500
    for i in range(file_count):
        fname = f"file_{i:04d}.txt"
        fpath = os.path.join(base_dir, fname)
        with open(fpath, "w") as f:
            f.write(f"content {i}")
        db.upsert_document(base_dir, fname, f"hash_{i}", f"content {i}")

    session_id = history_mgr.create_snapshot(base_dir)

    sub_dir = os.path.join(base_dir, "organized")
    os.makedirs(sub_dir, exist_ok=True)
    for i in range(file_count):
        fname = f"file_{i:04d}.txt"
        shutil.move(os.path.join(base_dir, fname), os.path.join(sub_dir, fname))

    orig_get_db = hist_mod.get_db_connection
    commits = 0

    def proxy_get_db(path):
        conn = orig_get_db(path)
        if path == db.db_path:

            class ConnProxy:
                def __init__(self, target):
                    self._target = target

                def commit(self):
                    nonlocal commits
                    commits += 1
                    return self._target.commit()

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    if exc_type is None:
                        self.commit()
                    else:
                        self._target.rollback()

                def __getattr__(self, name):
                    return getattr(self._target, name)

            return ConnProxy(conn)
        return conn

    with patch("app.core.history.get_db_connection", side_effect=proxy_get_db):
        history_mgr.rollback(session_id)

    # 500 files / 50 = 10 batch commits (+ 1 pre-sync) = ~11-12 commits
    assert commits <= 15
    reduction = (500 - commits) / 500
    assert reduction > 0.95


def test_symlink_restoration_with_batch_transaction(history_env):
    """Restoration of symlinks batches database updates when moving aside conflicting files."""
    if __import__("sys").platform == "win32":
        pytest.skip("Windows requires admin privileges to create symlinks")

    base_dir, db, history_mgr, _ = history_env

    # 1. Target file and symlink
    target_path = os.path.join(base_dir, "real_target.txt")
    with open(target_path, "w") as f:
        f.write("target data")

    symlink_path = os.path.join(base_dir, "mylink.txt")
    os.symlink("real_target.txt", symlink_path)

    from app.core.scanner import get_files_recursively

    get_files_recursively(base_dir)

    session_id = history_mgr.create_snapshot(base_dir)

    # 2. Remove symlink and place a regular file there
    os.remove(symlink_path)
    with open(symlink_path, "w") as f:
        f.write("conflicting regular file")
    db.upsert_document(base_dir, "mylink.txt", "conflicting_hash", "conflicting data")

    # 3. Rollback
    history_mgr.rollback(session_id)

    # 4. Verify symlink restored
    assert os.path.exists(symlink_path)
    assert os.path.islink(symlink_path)
    assert os.readlink(symlink_path) == "real_target.txt"

    # 5. Verify conflicting regular file was moved aside and its document updated in DB
    docs_conn = get_db_connection(db.db_path)
    rows = docs_conn.execute(
        "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
    ).fetchall()
    paths = {r[0] for r in rows}
    safe_rel_entries = [p for p in paths if p.startswith("mylink_")]
    assert len(safe_rel_entries) == 1
