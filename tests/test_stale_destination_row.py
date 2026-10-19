"""A leftover DB row at a move's destination must not abort the sort (#724)."""

import os

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import execute_moves


@pytest.fixture
def env(tmp_path):
    base_dir = tmp_path / "inbox"
    base_dir.mkdir()
    worker = DBWorker()
    db = Database(tmp_path / "docs.db", worker=worker)
    cache = CacheManager(str(tmp_path / "cache.db"), worker=worker)
    history = HistoryManager(db, cache, str(tmp_path / "history.db"))
    yield str(base_dir), db, history
    worker.stop()
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()


def _filepaths(db, base_dir):
    # Writes land on the worker thread; wait for them and read past the cache.
    db.worker.execute_write(lambda: None)
    db.invalidate_cache()
    return sorted(doc[0] for doc in db.get_all_documents(base_dir))


def test_update_document_path_replaces_stale_destination_row(env):
    base_dir, db, _ = env
    db.upsert_document(base_dir, "report.txt", "h1", "fresh report")
    db.upsert_document(base_dir, "Cat/report.txt", "h0", "stale report")

    db.update_document_path(base_dir, "report.txt", "Cat/report.txt")

    assert _filepaths(db, base_dir) == ["Cat/report.txt"]
    assert db.get_document(base_dir, "Cat/report.txt")["file_hash"] == "h1"


def test_sort_succeeds_with_stale_destination_row(env):
    base_dir, db, history = env
    for name in ("notes.txt", "report.txt"):
        with open(os.path.join(base_dir, name), "w", encoding="utf-8") as f:
            f.write(f"contents of {name}")
    db.upsert_document(base_dir, "notes.txt", "h2", "notes")
    db.upsert_document(base_dir, "report.txt", "h1", "report")
    db.upsert_document(base_dir, "Cat/report.txt", "h0", "stale")

    plan = {
        "Cat": {
            name: {
                "__type__": "file",
                "relative_source": f"../{name}",
                "target_filename": name,
            }
            for name in ("notes.txt", "report.txt")
        }
    }
    execute_moves(base_dir, plan, db, history)

    assert sorted(os.listdir(os.path.join(base_dir, "Cat"))) == [
        "notes.txt",
        "report.txt",
    ]
    assert _filepaths(db, base_dir) == ["Cat/notes.txt", "Cat/report.txt"]
    assert db.get_document(base_dir, "Cat/report.txt")["file_hash"] == "h1"


def test_update_without_source_row_keeps_destination_row(env):
    # A replayed update (e.g. during rollback) must not delete the row it targets.
    base_dir, db, _ = env
    db.upsert_document(base_dir, "report.txt", "h1", "report")

    db.update_document_path(base_dir, "Cat/report.txt", "report.txt")

    assert _filepaths(db, base_dir) == ["report.txt"]
