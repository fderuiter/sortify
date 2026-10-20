import os

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager


@pytest.fixture
def test_snapshot_env(tmp_path):
    base_dir = str(tmp_path / "workspace")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "autosorter.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_db_path = str(tmp_path / "history.db")
    history_manager = HistoryManager(db, cache, history_db_path)

    yield base_dir, db, cache, history_manager, db_worker
    db_worker.stop()


def test_new_snapshot_stores_user_verified_target_paths(test_snapshot_env):
    base_dir, db, cache, history_manager, db_worker = test_snapshot_env

    # 1. Create test file and register in DB with user_verified_target_path
    file_rel = "doc1.txt"
    file_abs = os.path.join(base_dir, file_rel)
    with open(file_abs, "w") as f:
        f.write("Test content for document 1")

    db.upsert_document(base_dir, file_rel, "hash_doc1", "Test content for document 1")
    db.set_user_verified_target_path(base_dir, file_rel, "Financial_Reports")

    # Verify active document record has target path set
    fetched_path = db.get_user_verified_target_path(base_dir, file_rel)
    assert fetched_path == "Financial_Reports"

    # 2. Create snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # 3. Verify snapshot_documents table stores user_verified_target_path
    conn = get_db_connection(history_manager.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath, user_verified_target_path FROM snapshot_documents WHERE session_id = ?",
            (session_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == file_rel
    assert rows[0][1] == "Financial_Reports"
