"""The pre-move hash is a SHA-256 of the file's bytes, not the stored DB hash (#844)."""

import hashlib
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


def test_text_md5_in_db_does_not_fail_verification(env):
    base_dir, db, history = env
    text = "# Notes\n\nQuarterly planning notes."
    with open(os.path.join(base_dir, "notes.md"), "w", encoding="utf-8") as f:
        f.write(text)
    # What the analyzer stores when it has no file hash: an MD5 of the text.
    db.upsert_document(
        base_dir, "notes.md", hashlib.md5(text.encode("utf-8")).hexdigest(), text
    )

    plan = {
        "Notes": {
            "notes.md": {
                "__type__": "file",
                "relative_source": "../notes.md",
                "target_filename": "notes.md",
            }
        }
    }
    summary = execute_moves(base_dir, plan, db, history)

    records = summary["audit_report"]["records"]
    assert [r["status"] for r in records] == ["VERIFIED_PASS"]
