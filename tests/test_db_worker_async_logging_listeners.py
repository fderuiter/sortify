"""Unit tests for DBWorker async and sync write error logging."""

import logging
import threading

import pytest

from app.core.db_worker import DBWorker

pytestmark = pytest.mark.real_db_worker


def test_async_write_failure_logs_traceback_and_notifies_listener(caplog):
    """Verify unhandled exception in async write logs traceback without crashing worker."""
    worker = DBWorker()

    def _failing_async_write(arg1, kwarg1=None):
        raise ValueError("Simulated database async write failure")

    with caplog.at_level(logging.ERROR):
        worker.execute_write_async(_failing_async_write, "test_arg", kwarg1="val")

        # Barrier to wait for async write execution on worker thread
        wait_event = threading.Event()
        worker.execute_write_async(wait_event.set)
        assert wait_event.wait(timeout=3.0)

    # Verify log output contains function name and traceback
    assert "Simulated database async write failure" in caplog.text
    assert "_failing_async_write" in caplog.text
    assert "Traceback" in caplog.text

    worker.stop()


def test_async_write_failure_direct_on_worker_thread(caplog):
    """Verify execute_write_async called on worker thread catches failure and logs traceback."""
    worker = DBWorker()

    def _nested_async_caller():
        def _failing_inner():
            raise RuntimeError("Inner failure on worker thread")

        # Call execute_write_async while running inside the worker thread
        worker.execute_write_async(_failing_inner)

    with caplog.at_level(logging.ERROR):
        worker.execute_write(_nested_async_caller)

    # Verify log output
    assert "Inner failure on worker thread" in caplog.text
    assert "_failing_inner" in caplog.text
    assert "Traceback" in caplog.text

    worker.stop()


def test_sync_execute_write_failure_raises_and_logs(caplog):
    """Verify synchronous execute_write logs traceback and raises exception."""
    worker = DBWorker()

    def _failing_sync_write():
        raise KeyError("Simulated sync failure")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(KeyError, match="Simulated sync failure"):
            worker.execute_write(_failing_sync_write)

    # Traceback is logged
    assert "Simulated sync failure" in caplog.text
    assert "Traceback" in caplog.text

    worker.stop()
