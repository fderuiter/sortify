import os

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

    yield base_dir, db, history_manager, db_worker
    db_worker.stop()


def test_rollback_prints_nothing_to_stdout(setup_history_env, capsys):
    """Rollback must not dump file inventories to stdout (#725)."""
    base_dir, db, history_manager, db_worker = setup_history_env
    with open(os.path.join(base_dir, "secret-report.txt"), "w") as f:
        f.write("content")
    session_id = history_manager.create_snapshot(base_dir)
    src = os.path.join(base_dir, "secret-report.txt")
    dst = os.path.join(base_dir, "Moved", "secret-report.txt")
    os.makedirs(os.path.join(base_dir, "Moved"))
    os.replace(src, dst)
    seq = history_manager.moves.record_intent(session_id, src, dst)
    history_manager.moves.mark_done(session_id, seq, dst)
    capsys.readouterr()

    history_manager.rollback(session_id)

    assert os.path.exists(os.path.join(base_dir, "secret-report.txt"))
    out = capsys.readouterr().out
    assert "DEBUG ROLLBACK" not in out
    assert "secret-report.txt" not in out
