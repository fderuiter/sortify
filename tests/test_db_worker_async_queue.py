"""Tests that run against the real asynchronous DBWorker write queue (#738).

Every other test replaces ``DBWorker.execute_write_async`` with a synchronous
call (see ``sync_db_worker`` in conftest.py). These opt out, so ordering and
visibility bugs in the real queue can show up.
"""

import os
import threading

import pytest

from app.core.mover import execute_moves

pytestmark = pytest.mark.real_db_worker


def test_async_write_returns_before_it_runs_and_keeps_queue_order(test_history_env):
    _base, _db, _cache, _history, worker = test_history_env
    gate = threading.Event()
    order = []

    worker.execute_write_async(gate.wait, 10)
    worker.execute_write_async(order.append, "first")
    worker.execute_write_async(order.append, "second")

    # The worker is still blocked on the gate, so nothing has run yet.
    assert order == []

    gate.set()
    # A synchronous write queues behind the async ones, so it acts as a barrier.
    worker.execute_write(order.append, "barrier")
    assert order == ["first", "second", "barrier"]


def test_async_rating_write_is_visible_after_the_queue_drains(test_history_env):
    base_dir, db, _cache, _history, worker = test_history_env
    db.upsert_document(base_dir, "a.txt", "hash-a", "text")

    db.set_document_rating(base_dir, "a.txt", "good")
    worker.execute_write(lambda: None)

    assert db.get_document_rating(base_dir, "a.txt") == "good"


def test_sort_then_undo_with_real_async_queue(test_history_env):
    base_dir, db, _cache, history_manager, _worker = test_history_env
    src = os.path.join(base_dir, "report.txt")
    with open(src, "w") as fh:
        fh.write("report")
    plan = {
        "Docs": {
            "report.txt": {
                "__type__": "file",
                "relative_source": "../report.txt",
                "target_filename": "report.txt",
            }
        }
    }

    execute_moves(base_dir, plan, db, history_manager)

    dst = os.path.join(base_dir, "Docs", "report.txt")
    assert os.path.exists(dst)
    assert not os.path.exists(src)

    session_id = history_manager.get_sessions()[0]["session_id"]
    history_manager.unwind_session(session_id, db=db)

    assert os.path.exists(src)
    assert not os.path.exists(dst)
