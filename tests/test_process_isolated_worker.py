import multiprocessing
import os
import sys
from unittest import mock

from app.config import AppSettings
from app.core.daemon import (
    ContinuousWatchdogDaemon,
    lower_process_priority,
)


def _worker_priority_check_func(result_queue):
    lower_process_priority()
    import psutil

    p = psutil.Process()
    if sys.platform == "win32":
        res = p.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
    else:
        res = p.nice() == 19
    result_queue.put(res)


def test_lower_process_priority_does_not_affect_parent():
    """Verify that lower_process_priority() sets child process priority without modifying parent process priority."""
    import psutil

    parent_p = psutil.Process()
    initial_parent_nice = parent_p.nice() if sys.platform != "win32" else None

    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_worker_priority_check_func, args=(q,))
    proc.start()
    proc.join(timeout=5.0)

    assert not proc.is_alive()
    worker_lowered = q.get(timeout=2.0)
    assert worker_lowered is True

    if sys.platform != "win32":
        assert parent_p.nice() == initial_parent_nice


def test_daemon_spawns_process_isolated_worker(tmp_path):
    """Verify that ContinuousWatchdogDaemon spawns a distinct OS subprocess with a different PID for indexing and sorting."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "invoice.txt").write_text("Invoice details")

    settings = AppSettings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    daemon._spawn_sorting_process()

    proc = daemon._sorting_process
    assert proc is not None
    assert proc.pid is not None
    assert proc.pid != os.getpid()

    daemon.stop()
    assert daemon._sorting_process is None


def test_daemon_prevents_concurrent_worker_processes(tmp_path):
    """Verify that subsequent file changes queue rerun instead of spawning concurrent worker processes."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    settings = AppSettings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    # Simulate an active sorting process
    mock_proc = mock.MagicMock()
    mock_proc.is_alive.return_value = True
    daemon._sorting_process = mock_proc

    daemon._schedule_run(cancel_event=None)

    assert daemon._rerun_pending is True
    # Verify no new process replaced mock_proc while active
    assert daemon._sorting_process is mock_proc

    daemon.stop()


def test_daemon_stop_cleanly_terminates_worker_process(tmp_path):
    """Verify that stopping the daemon immediately terminates any active worker subprocess."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    settings = AppSettings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    mock_proc = mock.MagicMock()
    mock_proc.is_alive.return_value = True
    daemon._sorting_process = mock_proc

    daemon.stop()

    mock_proc.terminate.assert_called_once()
    mock_proc.join.assert_called()
    assert daemon._sorting_process is None
