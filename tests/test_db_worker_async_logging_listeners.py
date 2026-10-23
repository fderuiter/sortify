"""Unit tests for DBWorker async write error logging and listener dispatch."""

import logging
import threading

import pytest

from app.core.db_worker import DBWorker

pytestmark = pytest.mark.real_db_worker


def test_async_write_failure_logs_traceback_and_notifies_listener(caplog):
    """Verify unhandled exception in async write logs traceback and triggers write_error event."""
    worker = DBWorker()
    events = []

    def _listener(event_type, data):
        events.append((event_type, data))

    worker.register_listener(_listener)

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

    # Verify listener received write_error event
    write_error_events = [e for e in events if e[0] == "write_error"]
    assert len(write_error_events) == 1
    event_type, payload = write_error_events[0]

    assert event_type == "write_error"
    assert payload["func_name"] == "_failing_async_write"
    assert isinstance(payload["exception"], ValueError)
    assert str(payload["exception"]) == "Simulated database async write failure"
    assert payload["error"] == "Simulated database async write failure"
    assert payload["args"] == ("test_arg",)
    assert payload["kwargs"] == {"kwarg1": "val"}

    worker.stop()


def test_async_write_failure_direct_on_worker_thread(caplog):
    """Verify execute_write_async called on worker thread catches failure, logs, and notifies."""
    worker = DBWorker()
    events = []

    def _listener(event_type, data):
        events.append((event_type, data))

    worker.register_listener(_listener)

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

    # Verify listener received write_error event
    write_error_events = [e for e in events if e[0] == "write_error"]
    assert len(write_error_events) == 1
    _evt, payload = write_error_events[0]
    assert payload["func_name"] == "_failing_inner"
    assert isinstance(payload["exception"], RuntimeError)

    worker.stop()


def test_sync_execute_write_failure_raises_and_logs(caplog):
    """Verify synchronous execute_write logs traceback, raises exception, and skips write_error listener event."""
    worker = DBWorker()
    events = []

    def _listener(event_type, data):
        events.append((event_type, data))

    worker.register_listener(_listener)

    def _failing_sync_write():
        raise KeyError("Simulated sync failure")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(KeyError, match="Simulated sync failure"):
            worker.execute_write(_failing_sync_write)

    # Traceback is logged
    assert "Simulated sync failure" in caplog.text
    assert "Traceback" in caplog.text

    # No write_error event sent for synchronous writes (result_q is not None)
    write_error_events = [e for e in events if e[0] == "write_error"]
    assert len(write_error_events) == 0

    worker.stop()


def test_async_write_failure_within_transaction_session(caplog, tmp_path):
    """Verify async write failure inside an active session logs traceback, rolls back session, and notifies listener."""
    worker = DBWorker()
    events = []

    def _listener(event_type, data):
        events.append((event_type, data))

    worker.register_listener(_listener)

    db_path = str(tmp_path / "session_test.db")
    session_id = "sess-123"

    worker.begin_session(session_id, db_path=db_path)
    assert worker.is_in_session()

    def _failing_session_write():
        raise RuntimeError("Session write error")

    with caplog.at_level(logging.ERROR):
        worker.execute_write_async(_failing_session_write)

        wait_event = threading.Event()
        worker.execute_write_async(wait_event.set)
        assert wait_event.wait(timeout=3.0)

    # Verify logging and session rollback
    assert "Session write error" in caplog.text
    assert "Rolling back open session transaction automatically" in caplog.text
    assert not worker.is_in_session()

    # Verify listener notification
    write_error_events = [e for e in events if e[0] == "write_error"]
    assert len(write_error_events) == 1
    assert write_error_events[0][1]["func_name"] == "_failing_session_write"

    worker.stop()
