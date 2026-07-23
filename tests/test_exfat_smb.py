import os
import shutil
from unittest.mock import patch


def test_rollback_zero_inode(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

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

    with patch("os.stat", side_effect=mock_stat):
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
