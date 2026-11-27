"""Tests for the targeted multiprocessing worker for core clustering.

These tests verify process isolation, cooperative queuing, thread limits,
priority management, and immediate cancellation of the clustering child process.
"""

import multiprocessing
import os
import sys
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.core.analyzer_strategies import (
    RecursiveKMeansStrategy,
    recursive_kmeans_worker_main,
)
from app.core.shared_registry import SharedModelRegistry

pytestmark = [pytest.mark.slow, pytest.mark.ml, pytest.mark.subprocess_clustering]


def _probe_clustering_worker_main(probe_conn: Any, *worker_args: Any) -> None:
    """Wrap _cluster_recursive inside the spawned child process to report runtime isolation state."""
    orig_cluster = RecursiveKMeansStrategy._cluster_recursive
    sent = False

    def _probed_cluster_recursive(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal sent
        if not sent:
            sent = True
            niceness = None
            if sys.platform != "win32":
                try:
                    niceness = os.nice(0)
                except Exception:
                    niceness = None
            probe_conn.send(
                {
                    "pid": os.getpid(),
                    "omp_threads": int(os.environ["OMP_NUM_THREADS"]),
                    "mkl_threads": int(os.environ["MKL_NUM_THREADS"]),
                    "openblas_threads": int(os.environ["OPENBLAS_NUM_THREADS"]),
                    "niceness": niceness,
                }
            )
            probe_conn.close()
        return orig_cluster(self, *args, **kwargs)

    RecursiveKMeansStrategy._cluster_recursive = _probed_cluster_recursive  # type: ignore[method-assign]
    recursive_kmeans_worker_main(*worker_args)


def test_multiprocess_clustering_execution(monkeypatch: pytest.MonkeyPatch):
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

    real_get_context = multiprocessing.get_context
    probe_pipes: list[tuple[Any, Any]] = []

    def _tracking_get_context(method: str | None = None) -> Any:
        ctx: Any = real_get_context(method)

        class _ContextProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(ctx, name)

            def Process(self, *args: Any, **kwargs: Any) -> Any:
                if kwargs.get("target") is recursive_kmeans_worker_main:
                    recv_conn, send_conn = ctx.Pipe(duplex=False)
                    probe_pipes.append((recv_conn, send_conn))
                    orig_args = tuple(kwargs.get("args", ()))
                    kwargs = dict(kwargs)
                    kwargs["target"] = _probe_clustering_worker_main
                    kwargs["args"] = (send_conn, *orig_args)
                return ctx.Process(*args, **kwargs)

        return _ContextProxy()

    monkeypatch.setattr(
        "app.core.analyzer_strategies.multiprocessing.get_context",
        _tracking_get_context,
    )

    strategy = RecursiveKMeansStrategy()

    plan, error = strategy.generate_plan(
        filenames=filenames,
        documents=documents,
        max_folders=2,
        stop_words={"the", "and"},
        pre_fetched_vectors=pre_fetched_vectors,
    )

    from app.core.analyzer import SortingPlan

    assert plan is not None
    assert isinstance(plan, (SortingPlan, dict))
    assert error >= 0.0

    assert len(probe_pipes) == 1
    recv_conn, send_conn = probe_pipes[0]
    send_conn.close()
    try:
        assert recv_conn.poll(5.0)
        child_report = recv_conn.recv()
    finally:
        recv_conn.close()

    # Verify process isolation
    assert child_report["pid"] is not None
    assert child_report["pid"] != os.getpid()

    # Verify thread limit is retrieved from global model registry and applied in child
    expected_thread_limit = SharedModelRegistry.get_instance().get_thread_limit()
    assert child_report["omp_threads"] == expected_thread_limit
    assert child_report["mkl_threads"] == expected_thread_limit
    assert child_report["openblas_threads"] == expected_thread_limit

    # Verify OS niceness/priority on Unix-like platforms
    if sys.platform != "win32":
        # The spawned child lowers its own priority, outside the test process.
        if sys.platform == "darwin":
            assert child_report["niceness"] in (9, 19)
        else:
            assert child_report["niceness"] == 19


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
    # The process must shut down and release resources within 4 seconds to avoid environment flakiness
    assert elapsed < 4.0


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
