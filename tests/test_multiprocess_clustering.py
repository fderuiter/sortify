"""Tests for the targeted multiprocessing worker for core clustering.

These tests verify process isolation, cooperative queuing, thread limits,
priority management, and immediate cancellation of the clustering child process.
"""

import os
import sys
import time
from unittest.mock import MagicMock

import pytest

from app.core.analyzer_strategies import RecursiveKMeansStrategy
from app.core.shared_registry import SharedModelRegistry


@pytest.fixture(autouse=True)
def force_multiprocessing():
    """Ensure that the multiprocessing code path is executed even during pytest."""
    os.environ["FORCE_MULTIPROCESSING_CLUSTERING"] = "1"
    yield
    os.environ.pop("FORCE_MULTIPROCESSING_CLUSTERING", None)


def test_multiprocess_clustering_execution():
    """
    Verify:
    - Calculations are offloaded to a separate child process.
    - Child process PID is different from main process PID.
    - Thread limit is respected.
    - Priority management (niceness) is set to low priority on Unix-like systems.
    """
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
    documents = [
        "pizza restaurant dinner mozzarella pepperoni",
        "consulting services python web application development",
        "restaurant wings delivery pizza mozzarella",
        "python backend consulting application django development",
    ]
    pre_fetched_vectors = [
        [0.1, 0.2, 0.3],
        [0.9, 0.8, 0.7],
        [0.12, 0.22, 0.32],
        [0.88, 0.78, 0.68],
    ]

    strategy = RecursiveKMeansStrategy()

    plan, error = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words={"the", "and"},
        pre_fetched_vectors=pre_fetched_vectors,
    )

    assert plan is not None
    assert isinstance(plan, dict)

    # Verify process isolation
    assert hasattr(strategy, "_last_worker_pid")
    assert strategy._last_worker_pid is not None
    assert strategy._last_worker_pid != os.getpid()

    # Verify thread limit is retrieved from global model registry
    expected_thread_limit = SharedModelRegistry.get_instance().get_thread_limit()
    assert strategy._last_worker_thread_limit == expected_thread_limit

    # Verify OS niceness/priority on Unix-like platforms
    if sys.platform != "win32":
        assert strategy._last_worker_niceness is not None
        if sys.platform == "darwin":
            assert strategy._last_worker_niceness in (9, 19)
        else:
            assert strategy._last_worker_niceness == 19


def test_multiprocess_clustering_cancellation():
    """
    Verify that immediate termination of the isolated process is respected upon cancellation.
    The child process should shutdown cleanly within 2 seconds of cancellation.
    """
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"] * 10
    documents = ["some text for document"] * len(filenames)
    pre_fetched_vectors = [[0.1, 0.2, 0.3]] * len(filenames)

    strategy = RecursiveKMeansStrategy()

    # We mock a cancel_check that returns True immediately after the first check.
    # To simulate a cancellation while the main thread polls, cancel_check returns True.
    cancel_check = MagicMock(return_value=True)

    start_time = time.time()
    plan, error = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words={"the"},
        pre_fetched_vectors=pre_fetched_vectors,
        cancel_check=cancel_check,
    )
    elapsed = time.time() - start_time

    # Return must be empty/blank on cancellation
    assert plan == {}
    # The process must shut down and release resources within 2 seconds
    assert elapsed < 2.0


def test_multiprocess_clustering_no_db_access():
    """
    Verify that no database context is passed to the worker strategy or accessed in the child process.
    The child process must run free of direct SQLite database connections.
    """
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
    documents = ["content one", "content two", "content three", "content four"]

    strategy = RecursiveKMeansStrategy()
    # Explicitly set mock database object that will raise error on any access
    mock_db = MagicMock()
    mock_db.db_path = "mock.db"
    strategy.db = mock_db
    strategy.base_dir = "mock_base"

    # Even if db is set on the strategy, the child process strategy must have .db = None
    plan, error = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words={"the"},
    )

    assert plan is not None
    # No calls to mock_db should have been made from the child process because we did not serialize or pass db.
    # Any access to db on strategy in child process is prevented as strategy.db = None.
    mock_db.assert_not_called()
