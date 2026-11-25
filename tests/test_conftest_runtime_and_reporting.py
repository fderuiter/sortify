"""Tests for production-default test runtime fixtures, DBWorker.flush(), and controller-only failure reporting."""

from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.analyzer_strategies import RecursiveKMeansStrategy
from app.core.db_worker import DBWorker
from tests.conftest import pytest_configure, pytest_runtest_logreport


def test_default_runtime_uses_async_db_worker_and_subprocess_clustering() -> None:
    """Verify tests execute real async DBWorker queue and subprocess clustering by default."""
    assert RecursiveKMeansStrategy.run_in_subprocess is True

    worker = DBWorker()
    executed_threads: list[int | None] = []
    release = threading.Event()

    def _job() -> None:
        release.wait(timeout=2.0)
        executed_threads.append(threading.get_ident())

    try:
        worker.execute_write_async(_job)
        # Async write has not yet drained before release is set
        assert executed_threads == []
        release.set()
        worker.flush()
        assert len(executed_threads) == 1
        assert executed_threads[0] == worker.thread.ident
    finally:
        worker.stop(stop_embeddings=False)


def test_db_worker_flush_is_noop_when_worker_stopped_or_inside_worker_thread() -> None:
    """Verify DBWorker.flush() drains queued work and returns safely on worker thread or after stop."""
    worker = DBWorker()
    events: list[str] = []

    def _nested() -> None:
        worker.flush()
        events.append("nested")

    try:
        for idx in range(5):
            worker.execute_write_async(lambda i=idx: events.append(f"w{i}"))
        worker.execute_write_async(_nested)
        worker.flush()
        assert events == ["w0", "w1", "w2", "w3", "w4", "nested"]
    finally:
        worker.stop(stop_embeddings=False)

    # Safe no-op after stop()
    worker.flush()


@pytest.mark.inline_clustering
def test_opt_in_inline_clustering_marker_disables_subprocess() -> None:
    """Verify @pytest.mark.inline_clustering attaches the inline_clustering fixture."""
    assert RecursiveKMeansStrategy.run_in_subprocess is False


@pytest.mark.sync_db_worker
def test_opt_in_sync_db_worker_marker_runs_writes_synchronously() -> None:
    """Verify @pytest.mark.sync_db_worker executes execute_write_async before returning."""
    worker = DBWorker()
    events: list[str] = []
    try:
        worker.execute_write_async(lambda: events.append("done"))
        assert events == ["done"]
    finally:
        worker.stop(stop_embeddings=False)


def test_controller_only_failure_log_truncation_and_github_actions_gating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pytest_failures.txt is truncated/appended only on the controller and ::error:: requires GITHUB_ACTIONS=true."""
    monkeypatch.chdir(tmp_path)
    failures_path = tmp_path / "pytest_failures.txt"
    failures_path.write_text("stale failure from previous session\n", encoding="utf-8")

    # 1. Worker configure must NOT truncate pytest_failures.txt
    worker_config = SimpleNamespace(workerinput={"workerid": "gw0"})
    pytest_configure(worker_config)
    assert (
        failures_path.read_text(encoding="utf-8")
        == "stale failure from previous session\n"
    )

    # 2. Controller configure MUST truncate/remove stale pytest_failures.txt
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    controller_config = SimpleNamespace()
    pytest_configure(controller_config)
    assert not failures_path.exists()

    report = SimpleNamespace(
        failed=True,
        when="call",
        nodeid="tests/test_sample.py::test_example",
        longrepr="AssertionError: expected 1 == 2",
    )

    # 3. Worker logreport must NOT append or emit ::error::
    buf = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    pytest_runtest_logreport(report)
    assert not failures_path.exists()
    assert buf.getvalue() == ""

    # 4. Controller logreport outside GitHub Actions appends once without ::error::
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    pytest_runtest_logreport(report)
    assert failures_path.is_file()
    assert "tests/test_sample.py::test_example" in failures_path.read_text(
        encoding="utf-8"
    )
    assert buf.getvalue() == ""

    # 5. Controller logreport with GITHUB_ACTIONS=true emits ::error:: annotation
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    pytest_runtest_logreport(report)
    assert "::error:: " in buf.getvalue()
