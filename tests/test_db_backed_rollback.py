import os
from unittest.mock import MagicMock, patch

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.link_manager import LinkManager


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

    yield base_dir, db, history_manager, db_worker
    db_worker.stop()


def test_rollback_recreates_symbolic_link(setup_history_env):
    if __import__("sys").platform == "win32":
        pytest.skip("Windows requires admin privileges to create symlinks")

    base_dir, db, history_manager, db_worker = setup_history_env

    # 1. Create a target file and a symlink pointing to it
    target_file = os.path.join(base_dir, "target.txt")
    with open(target_file, "w") as f:
        f.write("target content")

    symlink_path = os.path.join(base_dir, "mylink.txt")
    os.symlink("target.txt", symlink_path)

    # 2. Register link in LinkManager and scan
    from app.core.scanner import get_files_recursively

    get_files_recursively(base_dir)

    # Check LinkManager registered it
    assert LinkManager.get_link_info(symlink_path) is not None

    # 3. Create snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # 4. Delete the symlink from disk (simulating failed relocation or user action)
    os.remove(symlink_path)
    assert not os.path.exists(symlink_path)

    # 5. Run rollback
    history_manager.rollback(session_id)

    # 6. Verify symlink is recreated and points to original target
    assert os.path.exists(symlink_path)
    assert os.path.islink(symlink_path)
    assert os.readlink(symlink_path) == "target.txt"


def test_rollback_restores_windows_shortcuts_mocked(setup_history_env):
    base_dir, db, history_manager, db_worker = setup_history_env

    shortcut_path = os.path.join(base_dir, "shortcut.lnk")

    # Mock pylnk3
    mock_pylnk3 = MagicMock()
    mock_parsed = MagicMock()
    mock_parsed.path = "C:\\path\\to\\app.exe"
    mock_parsed.arguments = "--test --verbose"
    mock_parsed.description = "My test description"
    mock_parsed.icon = "icon.ico"
    mock_parsed.icon_index = 3
    mock_parsed.work_dir = "C:\\path\\to"
    mock_parsed.window_mode = 1

    mock_pylnk3.parse.return_value = mock_parsed

    # Ensure shortcut.lnk exists physically on disk as a dummy file so scanner finds it
    with open(shortcut_path, "w") as f:
        f.write("dummy shortcut contents")

    with patch("app.core.history.pylnk3", mock_pylnk3):
        # 1. Scanner registers and we create snapshot
        from app.core.scanner import get_files_recursively

        get_files_recursively(base_dir)

        session_id = history_manager.create_snapshot(base_dir)

        # Let's verify the metadata was recorded in database
        from app.core.db_conn import get_db_connection

        history_conn = get_db_connection(history_manager.db_path)
        with history_conn:
            cur = history_conn.execute(
                "SELECT arguments, description, icon_file, icon_index FROM snapshot_files WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "--test --verbose"
            assert row[1] == "My test description"
            assert row[2] == "icon.ico"
            assert row[3] == 3

        # 2. Delete the shortcut file completely
        os.remove(shortcut_path)
        assert not os.path.exists(shortcut_path)

        # 3. Rollback should recreate it with pylnk3.for_file
        history_manager.rollback(session_id)

        # Check pylnk3.for_file was called to recreate the shortcut with original properties
        mock_pylnk3.for_file.assert_called_with(
            "C:\\path\\to\\app.exe",
            lnk_name=shortcut_path,
            arguments="--test --verbose",
            description="My test description",
            icon_file="icon.ico",
            icon_index=3,
            work_dir="C:\\path\\to",
            window_mode=1,
        )


def test_snapshot_dangling_or_broken_links(setup_history_env):
    if __import__("sys").platform == "win32":
        pytest.skip("Windows requires admin privileges to create symlinks")

    base_dir, db, history_manager, db_worker = setup_history_env

    # Create dangling symlink
    symlink_path = os.path.join(base_dir, "dangling.txt")
    os.symlink("nonexistent_target.txt", symlink_path)

    # Create snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # Let's delete the symlink
    os.remove(symlink_path)

    # Check it can be successfully rolled back / recreated
    history_manager.rollback(session_id)

    assert os.path.lexists(symlink_path)
    assert os.path.islink(symlink_path)
    assert os.readlink(symlink_path) == "nonexistent_target.txt"


def test_backward_compatibility_old_records(setup_history_env):
    base_dir, db, history_manager, db_worker = setup_history_env

    # We manually insert a snapshot file without the new columns into the history DB, simulating old structure
    from app.core.db_conn import get_db_connection

    history_conn = get_db_connection(history_manager.db_path)
    session_id = "old-session-123"

    # Create the old_file.txt so it is found physically
    old_file_path = os.path.join(base_dir, "old_file.txt")
    with open(old_file_path, "w") as f:
        f.write("old content")
    st = os.lstat(old_file_path)

    with history_conn:
        history_conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, 12345.6, ?, 'active')",
            (session_id, base_dir),
        )
        # We simulate the columns being NULL or not selected, but let's insert standard row
        history_conn.execute(
            """
            INSERT INTO snapshot_files (
                session_id, original_rel_path, inode, size, mtime, is_symlink, symlink_target
            ) VALUES (?, 'old_file.txt', ?, ?, ?, 0, NULL)
            """,
            (session_id, st.st_ino, st.st_size, st.st_mtime),
        )

    missing = history_manager.check_missing_files(session_id)
    assert len(missing) == 0

    # Ensure rollback doesn't raise error
    history_manager.rollback(session_id)
