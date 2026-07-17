import os
import shutil
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
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


def test_incremental_sync_and_stop_on_failure(setup_history_env):
    base_dir, db, history_manager = setup_history_env
    
    # Create two files
    file1_src = os.path.join(base_dir, "file1.txt")
    file2_src = os.path.join(base_dir, "file2.txt")
    with open(file1_src, "w") as f:
        f.write("file1")
    with open(file2_src, "w") as f:
        f.write("file2")

    # Upsert to DB
    db.upsert_document(base_dir, "file1.txt", "hash1", "text1", None)
    db.upsert_document(base_dir, "file2.txt", "hash2", "text2", None)

    # Take snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # Move files and update DB to simulate organization
    file1_dst = os.path.join(base_dir, "folder", "file1.txt")
    file2_dst = os.path.join(base_dir, "folder", "file2.txt")
    os.makedirs(os.path.dirname(file1_dst), exist_ok=True)
    shutil.move(file1_src, file1_dst)
    shutil.move(file2_src, file2_dst)

    
    def _delete():
        with closing(sqlite3.connect(db.db_path)) as conn, conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))
    db.worker.execute_write(_delete)
    db.upsert_document(base_dir, os.path.join("folder", "file1.txt"), "hash1", "text1", None)
    db.upsert_document(base_dir, os.path.join("folder", "file2.txt"), "hash2", "text2", None)

    # Mock shutil.move to fail on the second file
    original_move = shutil.move
    def mock_move(src, dst):
        if "file2.txt" in src or "file2.txt" in dst:
            raise OSError("Mocked permission error on file2")
        return original_move(src, dst)

    with patch('shutil.move', side_effect=mock_move):
        with pytest.raises(OSError, match="Mocked permission error on file2"):
            history_manager.rollback(session_id)

    # Validate state:
    # 1. file1 should be rolled back to its original position
    assert os.path.exists(file1_src)
    assert not os.path.exists(file1_dst)

    # 2. file2 should remain at its organized position because rollback failed
    assert os.path.exists(file2_dst)
    assert not os.path.exists(file2_src)

    # 3. Database should reflect file1 at 'file1.txt' and file2 at 'folder/file2.txt'
    with closing(sqlite3.connect(db.db_path)) as conn, conn:
        cur = conn.execute("SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,))
        filepaths = {r[0] for r in cur.fetchall()}
    
    assert "file1.txt" in filepaths
    assert os.path.join("folder", "file2.txt") in filepaths
    assert os.path.join("folder", "file1.txt") not in filepaths
    assert "file2.txt" not in filepaths

    # 4. Session status should be 'failed'
    with closing(sqlite3.connect(history_manager.db_path)) as conn, conn:
        cur = conn.execute("SELECT status FROM sessions WHERE session_id = ?", (session_id,))
        status = cur.fetchone()[0]
    
    assert status == "failed"

def test_rollback_cyclic_collision(setup_history_env):
    base_dir, db, history_manager = setup_history_env
    
    file1_src = os.path.join(base_dir, "A.txt")
    file2_src = os.path.join(base_dir, "B.txt")
    with open(file1_src, "w") as f:
        f.write("A")
    with open(file2_src, "w") as f:
        f.write("B")

    db.upsert_document(base_dir, "A.txt", "hashA", "textA", None)
    db.upsert_document(base_dir, "B.txt", "hashB", "textB", None)

    session_id = history_manager.create_snapshot(base_dir)

    # Swap A and B
    temp = os.path.join(base_dir, "temp.txt")
    shutil.move(file1_src, temp)
    shutil.move(file2_src, file1_src)
    shutil.move(temp, file2_src)

    
    def _delete():
        with closing(sqlite3.connect(db.db_path)) as conn, conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))
    db.worker.execute_write(_delete)
    db.upsert_document(base_dir, "A.txt", "hashB", "textB", None)
    db.upsert_document(base_dir, "B.txt", "hashA", "textA", None)

    # Perform rollback
    history_manager.rollback(session_id)

    # Verify files restored
    with open(file1_src, "r") as f:
        assert f.read() == "A"
    with open(file2_src, "r") as f:
        assert f.read() == "B"

    # Verify db restored
    docA = db.get_document(base_dir, "A.txt")
    assert docA["file_hash"] == "hashA"

    docB = db.get_document(base_dir, "B.txt")
    assert docB["file_hash"] == "hashB"
