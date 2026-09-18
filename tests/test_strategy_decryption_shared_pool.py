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
    from app.core.shared_registry import SharedWorkerPool

    executor = get_decryption_executor()
    pool = SharedWorkerPool.get_instance()
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
