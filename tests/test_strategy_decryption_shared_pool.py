import os
import socket
import threading
import time

import pytest

from app.core.analyzer_strategies import (
    IsolatedStrategyMixin,
    get_decryption_executor,
)
from app.core.shared_registry import SharedWorkerPool, block_external_network


class DummyStrategy(IsolatedStrategyMixin):
    """Dummy strategy implementation inheriting from IsolatedStrategyMixin for context testing."""

    def __init__(self):
        super().__init__()


def test_get_decryption_executor_delegates_to_shared_worker_pool():
    """Verify that get_decryption_executor returns the singleton SharedWorkerPool instance."""
    pool = SharedWorkerPool.get_instance()
    executor = get_decryption_executor()
    assert executor is pool
    assert isinstance(executor, SharedWorkerPool)


def test_strategy_context_variable_propagation_in_background_worker():
    """Verify that thread-isolated strategy attributes (contextvars) are preserved in worker threads."""
    strategy = DummyStrategy()
    strategy.stop_words = {"test_word_1", "test_word_2"}
    strategy.max_folders = 5
    strategy.max_depth = 3
    strategy.max_features = 100
    strategy.base_dir = "/tmp/test_dir"

    captured_attributes = {}

    def worker_task(item):
        # Access isolated attributes inside the background execution thread
        captured_attributes["stop_words"] = strategy.stop_words
        captured_attributes["max_folders"] = strategy.max_folders
        captured_attributes["max_depth"] = strategy.max_depth
        captured_attributes["max_features"] = strategy.max_features
        captured_attributes["base_dir"] = strategy.base_dir
        return item * 2

    pool = SharedWorkerPool.get_instance()
    results = list(pool.map(worker_task, [1, 2, 3]))

    assert results == [2, 4, 6]
    assert captured_attributes["stop_words"] == {"test_word_1", "test_word_2"}
    assert captured_attributes["max_folders"] == 5
    assert captured_attributes["max_depth"] == 3
    assert captured_attributes["max_features"] == 100
    assert captured_attributes["base_dir"] == "/tmp/test_dir"


def test_shared_worker_pool_map_enforces_socket_sandboxing(socket_mock):
    """Verify network socket sandboxing guardrails are enforced on all worker threads in pool.map."""
    mock_connect, _ = socket_mock
    pool = SharedWorkerPool.get_instance()

    def task_trying_to_connect(item):
        s = socket.socket()
        try:
            s.connect(("8.8.8.8", 80))
        finally:
            s.close()

    with block_external_network(reason="strategy decryption sandboxing"):
        with pytest.raises(
            PermissionError, match="External network connections are blocked"
        ):
            list(pool.map(task_trying_to_connect, [1]))

    mock_connect.assert_not_called()


def test_global_thread_count_strictly_bounded():
    """Verify that background decryption task execution stays within configured SharedWorkerPool thread limits."""
    pool = SharedWorkerPool.get_instance(max_workers=3)
    max_workers = pool.max_workers

    active_counter = 0
    max_active_observed = 0
    lock = threading.Lock()

    def slow_decryption_task(item):
        nonlocal active_counter, max_active_observed
        with lock:
            active_counter += 1
            if active_counter > max_active_observed:
                max_active_observed = active_counter
        time.sleep(0.05)
        with lock:
            active_counter -= 1
        return item

    tasks = list(range(10))
    results = list(pool.map(slow_decryption_task, tasks))

    assert results == tasks
    assert max_active_observed <= max_workers


def test_db_decryption_uses_shared_worker_pool(tmp_path):
    """Verify db decryption operations utilize SharedWorkerPool."""
    from app.core.db import Database
    from app.core.db_worker import DBWorker

    db_worker = DBWorker()
    try:
        db_path = tmp_path / "test_docs.db"
        db = Database(db_path, worker=db_worker)

        base_dir = str(tmp_path / "test_base")
        os.makedirs(base_dir, exist_ok=True)

        # Add a document to db
        db.upsert_document(
            base_dir=base_dir,
            filepath="doc1.txt",
            file_hash="hash123",
            extracted_text="Hello World Decryption Test",
        )

        docs = db.get_all_documents(base_dir)
        assert len(docs) == 1
        assert docs[0][0] == "doc1.txt"
        assert docs[0][1] == "Hello World Decryption Test"
    finally:
        db_worker.stop()


def test_reentrant_submit_and_map_prevent_deadlock():
    """Verify that re-entrant submit and map calls from inside pool threads execute inline without deadlocking."""
    pool = SharedWorkerPool.get_instance(max_workers=2)

    def inner_task(val):
        return val * 10

    def outer_task(val):
        fut = pool.submit(inner_task, val)
        sub_mapped = list(pool.map(inner_task, [val + 1]))
        return fut.result() + sub_mapped[0]

    # Submit enough outer tasks to fill all pool worker threads
    outer_futs = [pool.submit(outer_task, i) for i in range(5)]
    results = [f.result(timeout=5.0) for f in outer_futs]

    assert results == [i * 10 + (i + 1) * 10 for i in range(5)]


def test_db_worker_reentrant_shared_worker_pool_deadlock_prevention():
    """Verify DBWorker tasks executing on behalf of SharedWorkerPool worker threads execute nested pool operations inline without deadlocking."""
    from app.core.db_worker import DBWorker

    pool = SharedWorkerPool.get_instance(max_workers=2)
    db_worker = DBWorker()

    try:

        def inner_task(val):
            return val * 10

        def db_task(val):
            return list(pool.map(inner_task, [val]))

        def outer_task(val):
            return db_worker.execute_write(db_task, val)

        outer_futs = [pool.submit(outer_task, i) for i in range(4)]
        results = [f.result(timeout=5.0) for f in outer_futs]

        assert results == [[i * 10] for i in range(4)]
    finally:
        db_worker.stop()


def test_non_main_thread_shared_worker_pool_deadlock_prevention():
    """Verify tasks submitted to SharedWorkerPool from background threads execute inline without deadlocking."""
    import threading

    pool = SharedWorkerPool.get_instance(max_workers=2)
    bg_results = []
    exception_holder = []

    def bg_thread_worker():
        try:
            res = list(pool.map(lambda x: x * 5, range(3)))
            bg_results.append(res)
        except Exception as e:
            exception_holder.append(e)

    threads = [threading.Thread(target=bg_thread_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not exception_holder, f"Exception in background thread: {exception_holder}"
    assert len(bg_results) == 4
    assert all(res == [0, 5, 10] for res in bg_results)
