import os
import shutil
import time
from unittest.mock import patch
import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager


@pytest.fixture
def setup_history_env(tmp_path):
    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "test_docs.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "test_cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "test_history.db"))

    yield base_dir, db, history_manager
    db_worker.stop()


def test_preserve_divergent_branches_during_cleanup(setup_history_env):
    base_dir, db, history_manager = setup_history_env

    # Setup files in base_dir
    file_path = os.path.join(base_dir, "file.txt")
    with open(file_path, "w") as f:
        f.write("initial content")

    db.upsert_document(base_dir, "file.txt", "hash1", "text1")

    # Force snapshot pruning to keep only the 1 latest session.
    # We patch HistoryManager._prune_snapshots to override limit to 1.
    original_prune = HistoryManager._prune_snapshots

    def mock_prune(self, conn, limit=10):
        return original_prune(self, conn, limit=1)

    with patch.object(HistoryManager, "_prune_snapshots", new=mock_prune):
        # Create Session 1
        session1_id = history_manager.create_snapshot(base_dir)
        time.sleep(0.05)

        # Create divergent branch for Session 1 with an active user file
        branch_dir = os.path.join(base_dir, ".branches", session1_id)
        os.makedirs(branch_dir, exist_ok=True)
        with open(os.path.join(branch_dir, "work.txt"), "w") as f:
            f.write("active unmerged work")

        # Create Session 2 (which is not divergent)
        session2_id = history_manager.create_snapshot(base_dir)
        time.sleep(0.05)

        # Create Session 3 (to trigger pruning on older sessions: S2 and S1)
        session3_id = history_manager.create_snapshot(base_dir)

    # Check database sessions
    conn = get_db_connection(history_manager.db_path)
    with conn:
        cur = conn.execute("SELECT session_id FROM sessions")
        remaining_session_ids = {r[0] for r in cur.fetchall()}

    # S3 must exist because it is the newest session
    assert session3_id in remaining_session_ids

    # S1 must be preserved because it has an active divergent branch with unmerged data
    assert session1_id in remaining_session_ids

    # S2 should be pruned/deleted because it's older than S3 and lacks an active divergent branch
    assert session2_id not in remaining_session_ids

    # Verify that the active divergent branch directory for S1 is intact on disk
    assert os.path.exists(os.path.join(branch_dir, "work.txt"))


def test_restore_symlinks_with_blocked_paths(setup_history_env):
    base_dir, db, history_manager = setup_history_env

    # 1. Create a physical file that will act as the target of a symlink
    target_rel = "target_file.txt"
    target_abs = os.path.join(base_dir, target_rel)
    with open(target_abs, "w") as f:
        f.write("target contents")

    # Upsert the target file into documents
    db.upsert_document(base_dir, target_rel, "target_hash", "target_extracted_text")

    # 2. Create a symbolic link pointing to target_file.txt
    link_rel = "my_symlink"
    link_abs = os.path.join(base_dir, link_rel)
    os.symlink(target_rel, link_abs)

    # 3. Create snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # 4. Now, simulate a path collision: we replace the symlink with a physical directory
    os.remove(link_abs)
    os.makedirs(link_abs)

    # Put a conflicting file inside the directory and upsert to database to verify it's updated
    conflict_rel = os.path.join(link_rel, "inside_conflict.txt")
    conflict_abs = os.path.join(base_dir, conflict_rel)
    with open(conflict_abs, "w") as f:
        f.write("blocking data")

    db.upsert_document(base_dir, conflict_rel, "conflict_hash", "conflict_extracted_text")

    # 5. Rollback to the snapshot, ignoring missing since user modified the link
    history_manager.rollback(session_id, ignore_missing=True)

    # 6. Verify that:
    # A. The symbolic link is restored and points to target_file.txt
    assert os.path.islink(link_abs)
    assert os.readlink(link_abs) == target_rel

    # B. The conflicting directory and file have been moved to a safe path (e.g. my_symlink_1)
    expected_safe_dir_rel = "my_symlink_1"
    expected_safe_dir_abs = os.path.join(base_dir, expected_safe_dir_rel)
    expected_safe_file_rel = os.path.join(expected_safe_dir_rel, "inside_conflict.txt")
    expected_safe_file_abs = os.path.join(base_dir, expected_safe_file_rel)

    assert os.path.isdir(expected_safe_dir_abs)
    assert os.path.exists(expected_safe_file_abs)
    with open(expected_safe_file_abs, "r") as f:
        assert f.read() == "blocking data"

    # C. DB documents table must have its records updated
    # "my_symlink/inside_conflict.txt" should be updated to "my_symlink_1/inside_conflict.txt"
    doc_moved = db.get_document(base_dir, expected_safe_file_rel)
    assert doc_moved is not None
    assert doc_moved["file_hash"] == "conflict_hash"

    # And old path record is removed/updated
    doc_old = db.get_document(base_dir, conflict_rel)
    assert doc_old is None


def test_metadata_signature_volatile_inodes_matching(setup_history_env):
    base_dir, db, history_manager = setup_history_env

    # 1. Setup a file in base_dir
    file_rel = "volatile_file.txt"
    file_abs = os.path.join(base_dir, file_rel)
    content = "very unique contents that match hash"
    with open(file_abs, "w") as f:
        f.write(content)

    from app.core.extractor import get_file_hash
    expected_hash = get_file_hash(file_abs)

    # Upsert to document database
    db.upsert_document(base_dir, file_rel, expected_hash, "extracted text")

    # Let's lstat and save original stat
    orig_stat = os.lstat(file_abs)

    # 2. Take a snapshot.
    # To test the volatile/missing inode scenario, we mock `os.lstat` so that it returns st_ino = 0.
    # This simulates a filesystem where inodes are volatile or unsupported.
    original_lstat = os.lstat

    def mock_lstat(path):
        st = original_lstat(path)

        class StatWrapper:
            def __init__(self, real_st):
                self._st = real_st

            @property
            def st_ino(self):
                return 0

            def __getattr__(self, name):
                return getattr(self._st, name)

        return StatWrapper(st)

    # Create the snapshot under the mock lstat so its recorded inode is 0
    with patch("os.lstat", side_effect=mock_lstat):
        session_id = history_manager.create_snapshot(base_dir)

    # Let's verify that the recorded inode is 0 in snapshot_files table
    conn = get_db_connection(history_manager.db_path)
    with conn:
        cur = conn.execute(
            "SELECT inode, file_hash FROM snapshot_files WHERE session_id = ?", (session_id,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == expected_hash

    # 3. Simulate moving/renaming the file.
    # Rename volatile_file.txt to nested_folder/moved_file.txt
    new_rel = os.path.join("nested_folder", "moved_file.txt")
    new_abs = os.path.join(base_dir, new_rel)
    os.makedirs(os.path.dirname(new_abs), exist_ok=True)
    shutil.move(file_abs, new_abs)

    # Explicitly restore mtime to match exactly, as signature (size, mtime, is_symlink, symlink_target) is compared.
    os.utime(new_abs, (orig_stat.st_atime, orig_stat.st_mtime))

    # Update database to simulate that the document path is now at new_rel
    db.upsert_document(base_dir, new_rel, expected_hash, "extracted text")
    db_conn = get_db_connection(db.db_path)
    with db_conn:
        db_conn.execute(
            "DELETE FROM documents WHERE base_dir = ? AND filepath = ?", (base_dir, file_rel)
        )

    # 4. Rollback.
    # We execute rollback under the same mock lstat so that inodes are marked unreliable.
    # This will force the signature/hash fallback matching.
    with patch("os.lstat", side_effect=mock_lstat):
        history_manager.rollback(session_id)

    # 5. Verify file is successfully recognized, matched via signature and hash, and restored back!
    assert os.path.exists(file_abs)
    assert not os.path.exists(new_abs)
    with open(file_abs, "r") as f:
        assert f.read() == content

    # DB records should be restored too
    doc_restored = db.get_document(base_dir, file_rel)
    assert doc_restored is not None
    assert doc_restored["file_hash"] == expected_hash

    doc_moved_removed = db.get_document(base_dir, new_rel)
    assert doc_moved_removed is None
