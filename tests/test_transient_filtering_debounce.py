import threading
from unittest import mock

import pytest

from app.config import AppSettings, Settings
from app.core.daemon import ContinuousWatchdogDaemon


class DummySettings:
    """Mock configuration for testing."""

    def __init__(self):
        self.DEBOUNCE_DELAY = 0.6
        self.MAX_DEBOUNCE_DELAY = 5.0
        self.IGNORED_EXTENSIONS = [".crdownload", ".tmp", ".download"]
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.CONTEXTUAL_RENAMING = False
        self.PRESERVE_HIERARCHY = False
        self.SORTING_STRATEGY = "default"
        self.CLINICAL_SMART_RENAMING = False
        self.CLINICAL_GENERATE_AUDIT_REPORT = True
        self.AI_ASSISTED_NAMING = False

    def load(self):
        pass


def test_settings_debounce_and_ignored_extensions_defaults():
    """Verify that Settings defines expected defaults for transient filtering and debounce."""
    settings = Settings()
    assert settings.DEBOUNCE_DELAY == 0.6
    assert settings.MAX_DEBOUNCE_DELAY == 5.0
    assert settings.IGNORED_EXTENSIONS == [".crdownload", ".tmp", ".download"]


def test_settings_debounce_configurability(tmp_path):
    """Verify debounce properties are configurable via user settings."""
    filepath = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(filepath))

    # Test assignment and persistence
    app_settings.DEBOUNCE_DELAY = 1.5
    app_settings.MAX_DEBOUNCE_DELAY = 10.0
    app_settings.IGNORED_EXTENSIONS = [".crdownload", ".tmp", ".custom_tmp"]

    assert app_settings.DEBOUNCE_DELAY == 1.5
    assert app_settings.MAX_DEBOUNCE_DELAY == 10.0
    assert app_settings.IGNORED_EXTENSIONS == [".crdownload", ".tmp", ".custom_tmp"]

    # Reload from file to verify serialization/deserialization
    app_settings.load()
    assert app_settings.DEBOUNCE_DELAY == 1.5
    assert app_settings.MAX_DEBOUNCE_DELAY == 10.0
    assert app_settings.IGNORED_EXTENSIONS == [".crdownload", ".tmp", ".custom_tmp"]


def test_watchdog_filters_out_transient_files(tmp_path):
    """Verify that the system filters out .crdownload and .tmp files from triggering events."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))

    # Basic defaults
    assert daemon.should_ignore_path("file.tmp") is True
    assert daemon.should_ignore_path("document.crdownload") is True
    assert daemon.should_ignore_path("archive.download") is True
    assert daemon.should_ignore_path("valid_file.txt") is False

    # Check case-insensitivity
    assert daemon.should_ignore_path("UPPERCASE.TMP") is True
    assert daemon.should_ignore_path("large_archive.CRDOWNLOAD") is True

    # Custom extensions configurability check
    settings.IGNORED_EXTENSIONS = [".partial", ".temp"]
    assert daemon.should_ignore_path("file.tmp") is False
    assert daemon.should_ignore_path("file.partial") is True
    assert daemon.should_ignore_path("file.temp") is True


def test_sorting_run_initiates_at_max_debounce_limit(tmp_path):
    """Verify a sorting run successfully initiates exactly 5.0 seconds after the first event

    during a continuous stream of events.
    """
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    # We mock time.time and threading.Timer to simulate continuous write traffic
    mock_time = 1000.0
    timer_calls = []

    def mock_timer_init(delay, target, args=None, kwargs=None):
        timer_calls.append((delay, target, args))
        # Return a mock timer that can be cancelled
        m = mock.MagicMock()
        return m

    with (
        mock.patch("time.time", return_value=mock_time) as patch_time,
        mock.patch("threading.Timer", side_effect=mock_timer_init),
    ):
        # 1. First event at t=0.0
        # Should start tracking first event time and schedule debounce timer with DEBOUNCE_DELAY (0.6)
        daemon.trigger_recalculation()
        assert daemon._first_event_time == 1000.0
        assert len(timer_calls) == 1
        assert timer_calls[-1][0] == 0.6  # delay

        # 2. Continuous events at intervals.
        # Event at t=1.0. Elapsed = 1.0. Max delay = 5.0 - 1.0 = 4.0. delay = min(0.6, 4.0) = 0.6.
        patch_time.return_value = 1001.0
        daemon.trigger_recalculation()
        assert daemon._first_event_time == 1000.0
        assert len(timer_calls) == 2
        assert timer_calls[-1][0] == 0.6

        # Event at t=4.5. Elapsed = 4.5. Max delay = 5.0 - 4.5 = 0.5. delay = min(0.6, 0.5) = 0.5.
        patch_time.return_value = 1004.5
        daemon.trigger_recalculation()
        assert daemon._first_event_time == 1000.0
        assert len(timer_calls) == 3
        assert timer_calls[-1][0] == pytest.approx(0.5)

        # Event at t=4.9. Elapsed = 4.9. Max delay = 5.0 - 4.9 = 0.1. delay = min(0.6, 0.1) = 0.1.
        patch_time.return_value = 1004.9
        daemon.trigger_recalculation()
        assert daemon._first_event_time == 1000.0
        assert len(timer_calls) == 4
        assert timer_calls[-1][0] == pytest.approx(0.1)

        # Event at t=5.0. Elapsed = 5.0. Max delay = 0.0. Elapsed >= MAX_DEBOUNCE_DELAY.
        # Should return early without scheduling any more timers (allowing the t=4.9 timer to execute).
        patch_time.return_value = 1005.0
        daemon.trigger_recalculation()
        assert daemon._first_event_time == 1000.0
        assert len(timer_calls) == 4  # No new timer call!


def test_scanning_stage_filters_out_transient_files(tmp_path):
    """Verify that files found during sorting scan are filtered using should_ignore_path."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    # Mock AppSession and its methods to check that the daemon executes the flow correctly
    mock_app_session_class = mock.MagicMock()
    mock_app_session_inst = mock.MagicMock()
    mock_app_session_class.return_value = mock_app_session_inst

    # Return a mix of valid files and temporary browser files
    scanned_files = [
        str(tmp_path / "document.pdf"),
        str(tmp_path / "invoice.txt"),
        str(tmp_path / "downloading.crdownload"),
        str(tmp_path / "temp_file.tmp"),
    ]

    with (
        mock.patch("app.core.daemon.AppSession", mock_app_session_class),
        mock.patch(
            "app.core.daemon.get_files_recursively", return_value=scanned_files
        ) as mock_scan,
        mock.patch("app.core.daemon.MetadataPass.run", return_value=[]),
    ):
        # We also mock process_items_async
        async def dummy_process_items(items, cancel_check, **kwargs):
            # Verify only non-ignored files are processed!
            assert len(items) == 2
            assert all("crdownload" not in f and "tmp" not in f for f in items)
            for f in items:
                yield f, "extracted text", "hash123", False

        mock_app_session_inst.process_items_async = dummy_process_items
        mock_app_session_inst.generate_sorting_plan.return_value = {}

        # Run the sorting sync manually
        cancel_event = threading.Event()
        daemon._run_sorting_sync(cancel_event)

        # Verify scan was executed for directory
        mock_scan.assert_called_with(str(tmp_path))
        assert mock_scan.call_count >= 1


def test_pluggable_recalc_callback(tmp_path):
    """Verify ContinuousWatchdogDaemon invokes pluggable recalc_callback when supplied."""
    settings = DummySettings()
    callback_mock = mock.MagicMock()

    daemon = ContinuousWatchdogDaemon(
        settings, str(tmp_path), recalc_callback=callback_mock
    )
    daemon._is_running = True

    cancel_event = threading.Event()
    daemon._schedule_run(cancel_event)

    callback_mock.assert_called_once_with(cancel_event)


def test_transient_download_files_ignored_by_handler(tmp_path):
    """Verify DaemonFolderHandler ignores transient download files before triggering recalculation."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon.trigger_recalculation = mock.MagicMock()

    from app.core.daemon import DaemonFolderHandler

    handler = DaemonFolderHandler(daemon)

    # Temporary download event
    event_tmp = mock.MagicMock()
    event_tmp.src_path = str(tmp_path / "downloading.crdownload")
    handler.on_any_event(event_tmp)
    daemon.trigger_recalculation.assert_not_called()

    event_tmp2 = mock.MagicMock()
    event_tmp2.src_path = str(tmp_path / "temp.tmp")
    handler.on_any_event(event_tmp2)
    daemon.trigger_recalculation.assert_not_called()

    # Valid file event
    event_valid = mock.MagicMock()
    event_valid.src_path = str(tmp_path / "document.pdf")
    handler.on_any_event(event_valid)
    daemon.trigger_recalculation.assert_called_once()


def test_desktop_watcher_rebuilds_plan_without_executing_moves(tmp_path):
    """Verify desktop watcher triggers draft plan rebuilds without auto-executing file moves."""
    from app.ui.app import AutoSorterApp

    settings = DummySettings()
    app = AutoSorterApp(settings)
    app.base_dir = str(tmp_path)
    app._rebuild_plan_async = mock.MagicMock()
    app.execute_sort = mock.MagicMock()

    mock_loop = mock.MagicMock()
    mock_loop.is_running.return_value = True

    def mock_call_soon(func, *args):
        func(*args)

    mock_loop.call_soon_threadsafe.side_effect = mock_call_soon
    app.loop = mock_loop

    with mock.patch("asyncio.get_running_loop", return_value=mock_loop):
        app.start_watcher()

    assert app.daemon is not None
    assert app.observer is not None

    # Simulate event triggering recalculation
    app.daemon.trigger_recalculation()

    # Schedule run
    cancel_event = threading.Event()
    app.daemon._schedule_run(cancel_event)

    app._rebuild_plan_async.assert_called_once()
    app.execute_sort.assert_not_called()

    app.stop_watcher()
    assert app.daemon is None


def test_watcher_pauses_during_active_operations(tmp_path):
    """Verify folder watcher pauses during active file organization/analysis and resumes after completion."""
    from app.ui.app import AutoSorterApp

    async def run_test():
        settings = DummySettings()
        app = AutoSorterApp(settings)
        app.base_dir = str(tmp_path)
        app.status_label = mock.MagicMock()
        app.execute_btn = mock.MagicMock()
        app.progress_bar = mock.MagicMock()
        app.cancel_btn = mock.MagicMock()
        app.file_progress_bar = mock.MagicMock()
        app.file_progress_label = mock.MagicMock()
        app.warnings_label = mock.MagicMock()
        app.ai_warnings_label = mock.MagicMock()
        app.meta_label = mock.MagicMock()

        app.stop_watcher = mock.MagicMock()
        app.start_watcher = mock.MagicMock()

        # 1. start_analysis stops watcher
        with (
            mock.patch("app.ui.app.AppSession"),
            mock.patch.object(app, "_scan_and_process_worker"),
        ):
            app.start_analysis()
            app.stop_watcher.assert_called_once()

        app.stop_watcher.reset_mock()
        app.start_watcher.reset_mock()

        # 2. execute_sort stops watcher during run and starts watcher when complete
        app.plan = {"test.txt": "Target/test.txt"}
        app.app_session = mock.MagicMock()
        app.app_session.execute_moves.return_value = {"success": True}
        app.app_session.history_manager.partial_fit_ratings_async = mock.AsyncMock()
        app.load_locked_files_from_db = mock.MagicMock()
        app.load_ratings_from_db = mock.MagicMock()
        app.render_tree = mock.MagicMock()

        app.execute_sort()
        await asyncio.sleep(0.05)

        assert app.stop_watcher.called
        assert app.start_watcher.called

    import asyncio

    asyncio.run(run_test())

