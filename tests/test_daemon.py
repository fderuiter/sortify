import threading
from unittest import mock

import pytest

from app.core.daemon import ContinuousWatchdogDaemon


class DummySettings:
    def __init__(self):
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()

    def load(self):
        self.loaded_ok = True


def test_daemon_should_ignore_path(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))

    # Ignored paths
    assert daemon.should_ignore_path(".autosorter") is True
    assert daemon.should_ignore_path("autosorter.db") is True
    assert daemon.should_ignore_path("history.db") is True
    assert daemon.should_ignore_path("cache.db") is True
    assert daemon.should_ignore_path("plan.json") is True
    assert daemon.should_ignore_path(".git/HEAD") is True
    assert daemon.should_ignore_path("__pycache__/foo.py") is True
    assert daemon.should_ignore_path("settings.json") is True
    assert daemon.should_ignore_path("autosorter.log") is True
    assert daemon.should_ignore_path("autosorter_sessions/abc") is True

    # Non-ignored paths
    assert daemon.should_ignore_path("document.txt") is False
    assert daemon.should_ignore_path("subfolder/report.pdf") is False


def test_daemon_trigger_recalculation_and_debounce(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    # We mock _schedule_run to check if it's called
    with mock.patch.object(daemon, "_schedule_run") as mock_schedule:
        # Trigger recalculation multiple times in quick succession
        daemon.trigger_recalculation()
        timer1 = daemon._debounce_timer
        assert timer1 is not None

        daemon.trigger_recalculation()
        timer2 = daemon._debounce_timer
        assert timer2 is not None
        assert (
            timer1 is not timer2
        )  # First timer should have been canceled and replaced

        # Clean up
        daemon.stop()


def test_daemon_interruption_mid_execution(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    cancel_event_1 = daemon._cancel_event
    assert cancel_event_1.is_set() is False

    # Triggering recalculation should set the old cancellation event and create a new one
    daemon.trigger_recalculation()
    assert cancel_event_1.is_set() is True

    cancel_event_2 = daemon._cancel_event
    assert cancel_event_2 is not cancel_event_1
    assert cancel_event_2.is_set() is False

    daemon.stop()


@pytest.mark.timeout(5)
def test_daemon_execution_flow(tmp_path):
    # Set up folders
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    file_to_sort = src_dir / "invoice.txt"
    file_to_sort.write_text("Invoice contents")

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    # Mock AppSession and its methods to check that the daemon executes the flow correctly
    mock_app_session_class = mock.MagicMock()
    mock_app_session_inst = mock.MagicMock()
    mock_app_session_class.return_value = mock_app_session_inst

    # Mock get_files_recursively to return our test file
    with (
        mock.patch("app.core.daemon.AppSession", mock_app_session_class),
        mock.patch(
            "app.core.daemon.get_files_recursively", return_value=["invoice.txt"]
        ),
        mock.patch("app.core.daemon.MetadataPass.run", return_value=[]),
    ):
        # We also mock process_items_async
        async def dummy_process_items(items, cancel_check, **kwargs):
            yield "invoice.txt", "extracted text", "hash123", False

        mock_app_session_inst.process_items_async = dummy_process_items
        mock_app_session_inst.generate_sorting_plan.return_value = {
            "invoice.txt": "dest/invoice.txt"
        }
        mock_app_session_inst.execute_moves.return_value = {"moved": 1}

        # Run the sorting sync manually
        cancel_event = threading.Event()
        daemon._run_sorting_sync(cancel_event)

        # Verify AppSession was closed and moves were executed
        mock_app_session_class.assert_called_once_with(settings, str(src_dir))
        mock_app_session_inst.partial_fit.assert_called()
        mock_app_session_inst.generate_sorting_plan.assert_called_once()
        mock_app_session_inst.execute_moves.assert_called_once_with(
            {"invoice.txt": "dest/invoice.txt"}
        )
        mock_app_session_inst.close.assert_called_once()
