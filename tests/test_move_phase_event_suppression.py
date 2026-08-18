"""Unit tests for Scoped Move Phase Event Suppression in ContinuousWatchdogDaemon."""

import threading
import time
from unittest import mock

from watchdog.events import FileModifiedEvent

from app.core.daemon import ContinuousWatchdogDaemon, DaemonFolderHandler


class DummySettings:
    def __init__(self):
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.DEBOUNCE_DELAY = 0.1
        self.MAX_DEBOUNCE_DELAY = 1.0

    def load(self):
        self.loaded_ok = True


def test_move_phase_flag_initial_and_scoped_toggle(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))

    # Initial state must be False
    assert daemon.is_moving is False

    # Within scoped move phase, state must be True
    with daemon.scoped_move_phase():
        assert daemon.is_moving is True

    # After exiting move phase, state must return to False
    assert daemon.is_moving is False


def test_event_suppression_during_active_move_phase(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    handler = DaemonFolderHandler(daemon)

    initial_cancel_event = daemon._cancel_event

    with daemon.scoped_move_phase():
        # Simulate filesystem event while move is active
        event = FileModifiedEvent(str(tmp_path / "moved_file.txt"))
        handler.on_any_event(event)

        # Triggering recalculation directly during move phase must also be suppressed
        daemon.trigger_recalculation()

        # Cancellation event should NOT be set and debounce timer should NOT be created
        assert initial_cancel_event.is_set() is False
        assert daemon._debounce_timer is None

    daemon.stop()


def test_events_outside_move_phase_trigger_recalculation(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True
    handler = DaemonFolderHandler(daemon)

    # When not in move phase, event must trigger recalculation
    event = FileModifiedEvent(str(tmp_path / "new_file.txt"))
    handler.on_any_event(event)

    assert daemon._debounce_timer is not None

    daemon.stop()


def test_exception_handling_releases_move_phase_flag(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    try:
        with daemon.scoped_move_phase():
            assert daemon.is_moving is True
            raise RuntimeError("Simulated error during file movement")
    except RuntimeError:
        pass

    # Flag must be released even after an exception
    assert daemon.is_moving is False

    # Subsequent event must trigger recalculation properly
    handler = DaemonFolderHandler(daemon)
    event = FileModifiedEvent(str(tmp_path / "file_after_error.txt"))
    handler.on_any_event(event)

    assert daemon._debounce_timer is not None

    daemon.stop()


def test_thread_safety_concurrent_access(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    results = []

    def move_worker():
        for _ in range(50):
            with daemon.scoped_move_phase():
                time.sleep(0.001)
                results.append(daemon.is_moving)

    def event_worker():
        handler = DaemonFolderHandler(daemon)
        event = FileModifiedEvent(str(tmp_path / "concurrent.txt"))
        for _ in range(50):
            handler.on_any_event(event)
            time.sleep(0.001)

    threads = [
        threading.Thread(target=move_worker),
        threading.Thread(target=event_worker),
        threading.Thread(target=move_worker),
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # Verify move_worker saw is_moving == True while in context
    assert all(r is True for r in results)
    daemon.stop()


def test_end_to_end_move_execution_suppresses_cancellation(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    handler = DaemonFolderHandler(daemon)

    mock_app_session_class = mock.MagicMock()
    mock_app_session_inst = mock.MagicMock()
    mock_app_session_class.return_value = mock_app_session_inst

    # Simulate execute_moves producing filesystem events during execution
    def side_effect_execute_moves(plan):
        # Fire watchdog event while moves are executing
        event = FileModifiedEvent(str(tmp_path / "sorted" / "doc.pdf"))
        handler.on_any_event(event)
        return {"moved": 1}

    mock_app_session_inst.execute_moves.side_effect = side_effect_execute_moves
    mock_app_session_inst.generate_sorting_plan.side_effect = [
        {"doc.pdf": "sorted/doc.pdf"},
        {},
    ]

    with (
        mock.patch("app.core.daemon.AppSession", mock_app_session_class),
        mock.patch("app.core.daemon.get_files_recursively", return_value=["doc.pdf"]),
        mock.patch("app.core.daemon.MetadataPass.run", return_value=[]),
    ):
        cancel_event = threading.Event()
        daemon._run_sorting_sync(cancel_event)

        # The active run must NOT have been canceled by the event emitted during execute_moves
        assert cancel_event.is_set() is False
        assert mock_app_session_inst.execute_moves.call_count == 1

    daemon.stop()
