"""Stopping the daemon must let in-flight sort work finish and leave nothing running."""

import asyncio
import threading
import time

import pytest

from app.core.daemon import ContinuousWatchdogDaemon


class DaemonSettings:
    LOG_FILE = "test.log"
    CONFLICT_POLICY = "rename"
    MAX_FOLDERS = 10
    STOP_WORDS = set()
    AI_CONSENT_GRANTED = False
    POLICIES = []

    def load(self):
        pass


class RecordingSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _daemon_threads():
    return [
        t
        for t in threading.enumerate()
        if t.is_alive() and t.name.startswith("daemon-sort")
    ]


@pytest.fixture
def daemon(tmp_path):
    d = ContinuousWatchdogDaemon(DaemonSettings(), str(tmp_path))
    yield d
    d.stop()


def test_stop_leaves_no_daemon_threads_and_can_run_twice(daemon):
    daemon.start()
    loop_thread = daemon._execution_thread
    assert loop_thread.is_alive()

    daemon.stop()
    daemon.stop()

    assert not loop_thread.is_alive()
    assert daemon.observer is None
    assert _daemon_threads() == []


def test_daemon_restarts_after_stop(daemon):
    daemon.start()
    daemon.stop()

    daemon.start()

    assert daemon._execution_thread.is_alive()
    assert daemon._event_loop is not None and daemon._event_loop.is_running()


def test_stop_waits_for_in_flight_sort_work_before_closing_the_session(daemon):
    """A move running on the sort thread must not lose its session mid-move."""
    daemon.start()
    session = RecordingSession()
    daemon._app_session = session
    started = threading.Event()
    seen = {}

    def slow_move():
        started.set()
        time.sleep(0.5)
        seen["session_closed_during_move"] = session.closed
        return "moved"

    future = asyncio.run_coroutine_threadsafe(
        daemon._in_sort_thread(slow_move), daemon._event_loop
    )
    assert started.wait(timeout=5)

    daemon.stop()

    assert seen == {"session_closed_during_move": False}
    assert session.closed
    assert _daemon_threads() == []
    assert future.done()


def test_stop_waits_for_a_triage_worker_move_before_closing_the_session(daemon):
    """Stop cancels the triage workers; the move they started keeps running."""
    daemon.start()
    session = RecordingSession()
    daemon._app_session = session
    started = threading.Event()
    seen = {}

    def slow_move():
        started.set()
        time.sleep(0.5)
        seen["session_closed_during_move"] = session.closed

    def start_worker_move():
        daemon._worker_tasks.append(
            asyncio.ensure_future(daemon._in_sort_thread(slow_move))
        )

    daemon._event_loop.call_soon_threadsafe(start_worker_move)
    assert started.wait(timeout=5)

    daemon.stop()

    assert seen == {"session_closed_during_move": False}
    assert session.closed
