import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import execute_moves
from app.core.session import AppSession
from app.ui.app import AutoSorterApp


@pytest.fixture
def test_env(tmp_path):
    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "test_docs.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "test_cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "test_history.db"))

    # Create dummy files to move
    file1 = os.path.join(base_dir, "file1.txt")
    with open(file1, "w") as f:
        f.write("content 1")
    db.upsert_document(base_dir, "file1.txt", "hash1", "text1")

    yield base_dir, db, cache, history_manager, db_worker
    db_worker.stop()


def test_automatic_rollback_on_failed_move(test_env):
    """Verify that a physical move failure triggers automatic rollback and database state recovery."""
    base_dir, db, cache, history_manager, db_worker = test_env

    # We want to move file1.txt to folder/file1.txt
    plan = {"folder": {"file1.txt": {"__type__": "file", "status": "To Be Sorted"}}}

    # Mock shutil.move to fail
    def mock_move(src, dst):
        raise OSError("Intentional permission error on file write")

    with patch("shutil.move", side_effect=mock_move):
        with pytest.raises(OSError, match="Intentional permission error on file write"):
            execute_moves(base_dir, plan, db, history_manager)

    # Acceptance Criteria Check:
    # 1. file1.txt should still exist in original location
    original_path = os.path.join(base_dir, "file1.txt")
    assert os.path.exists(original_path)

    # 2. Database paths must match the pre-move snapshot paths exactly after rollback
    doc = db.get_document(base_dir, "file1.txt")
    assert doc is not None
    assert doc["file_hash"] == "hash1"


@pytest.mark.anyio
async def test_ui_recovery_and_watcher_restart(tmp_path):
    """Verify that when a background move execution fails, the UI restarts the folder watcher, displays an error alert dialog, and re-enables the execute button."""
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    app = AutoSorterApp(settings)
    app.base_dir = base_dir

    # Mock app_session and dialog
    app_session_mock = MagicMock(spec=AppSession)
    app_session_mock.base_dir = base_dir
    app_session_mock.execute_moves.side_effect = RuntimeError("Disk failure")

    app.app_session = app_session_mock
    app.plan = {"dummy.txt": None}
    app.execute_btn = MagicMock()
    app.progress_bar = MagicMock()
    app.status_label = MagicMock()

    # Mock NiceGUI ui.dialog, ui.notify and tree render
    with (
        patch("nicegui.ui.dialog") as mock_dialog,
        patch("nicegui.ui.notify") as mock_notify,
        patch.object(app, "render_tree") as mock_render_tree,
        patch.object(app, "start_watcher") as mock_start_watcher,
        patch.object(app, "stop_watcher") as mock_stop_watcher,
    ):
        mock_dialog_instance = MagicMock()
        mock_dialog.return_value.__enter__.return_value = mock_dialog_instance

        # Call execute_sort (which runs background task on asyncio event loop)
        app.execute_sort()

        # Let the async task run
        await asyncio.sleep(0.1)

        # Acceptance Criteria Check:
        # 1. stop_watcher should be called before execution starts
        mock_stop_watcher.assert_called_once()

        # 2. execute_btn must be re-enabled after rollback
        app.execute_btn.enable.assert_called_once()

        # 3. An error alert dialog is rendered on the UI
        mock_dialog.assert_called()
        mock_dialog_instance.open.assert_called()

        # 4. Folder observer is restarted
        mock_start_watcher.assert_called_once()
