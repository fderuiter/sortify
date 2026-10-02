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
    plan = {
        "folder": {
            "file1.txt": {
                "__type__": "file",
                "relative_source": "../file1.txt",
                "status": "To Be Sorted",
            }
        }
    }

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
