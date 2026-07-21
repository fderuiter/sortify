import os
import shutil
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

def test_rollback_zero_inode(setup_history_env):
    base_dir, db, history_manager = setup_history_env
    
    file1_src = os.path.join(base_dir, "doc1.txt")
    file2_src = os.path.join(base_dir, "doc2.txt")
    with open(file1_src, "w") as f:
        f.write("content 1")
    with open(file2_src, "w") as f:
        f.write("content 2")

    db.upsert_document(base_dir, "doc1.txt", "hash1", "text1")
    db.upsert_document(base_dir, "doc2.txt", "hash2", "text2")

    # Mock os.stat to return 0 for st_ino for all files
    original_stat = os.stat
    class MockStatResult:
        def __init__(self, st):
            self.st_mode = st.st_mode
            self.st_ino = 0  # <--- ZERO INODE SIMULATION
            self.st_dev = st.st_dev
            self.st_nlink = st.st_nlink
            self.st_uid = st.st_uid
            self.st_gid = st.st_gid
            self.st_size = st.st_size
            self.st_atime = st.st_atime
            self.st_mtime = st.st_mtime
            self.st_ctime = st.st_ctime

    def mock_stat(path, *args, **kwargs):
        st = original_stat(path, *args, **kwargs)
        return MockStatResult(st)

    with patch('os.stat', side_effect=mock_stat):
        session_id = history_manager.create_snapshot(base_dir)

        # Move files around
        folder_dir = os.path.join(base_dir, "folder")
        os.makedirs(folder_dir, exist_ok=True)
        shutil.move(file1_src, os.path.join(folder_dir, "doc1.txt"))
        shutil.move(file2_src, os.path.join(folder_dir, "doc2.txt"))
        
        # Rollback using the zero-inode environment
        history_manager.rollback(session_id)

    # Verify they were restored
    assert os.path.exists(file1_src)
    assert os.path.exists(file2_src)
    assert not os.path.exists(os.path.join(folder_dir, "doc1.txt"))
    assert not os.path.exists(os.path.join(folder_dir, "doc2.txt"))
