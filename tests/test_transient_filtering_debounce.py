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


def test_ui_folder_change_handler_filtering(tmp_path):
    """Verify that FolderChangeHandler ignores transient, database, and session files in UI mode."""
    from app.ui.app import AutoSorterApp, FolderChangeHandler

    settings = DummySettings()
    mock_app = mock.MagicMock(spec=AutoSorterApp)
    mock_app.settings = settings
    mock_app.loop = mock.MagicMock()

    handler = FolderChangeHandler(mock_app)

    # Event for temporary/transient files and DB files
    event_tmp = mock.MagicMock(src_path=str(tmp_path / "download.tmp"))
    event_crdownload = mock.MagicMock(src_path=str(tmp_path / "file.crdownload"))
    event_db = mock.MagicMock(src_path=str(tmp_path / "autosorter.db"))
    event_session = mock.MagicMock(src_path=str(tmp_path / "autosorter_sessions" / "sess1"))
    event_valid = mock.MagicMock(src_path=str(tmp_path / "report.pdf"))

    handler.on_any_event(event_tmp)
    handler.on_any_event(event_crdownload)
    handler.on_any_event(event_db)
    handler.on_any_event(event_session)

    # None of the ignored events should trigger callback dispatch
    assert mock_app.loop.call_soon_threadsafe.call_count == 0

    # Valid event should trigger callback dispatch
    handler.on_any_event(event_valid)
    assert mock_app.loop.call_soon_threadsafe.call_count == 1
    mock_app.loop.call_soon_threadsafe.assert_called_with(mock_app._rebuild_plan_async)


def test_dynamic_ignored_extensions_change(tmp_path):
    """Verify dynamic changes to IGNORED_EXTENSIONS immediately take effect."""
    from app.core.event_handler import should_ignore_path

    settings = DummySettings()
    settings.IGNORED_EXTENSIONS = [".tmp", ".crdownload"]

    assert should_ignore_path("file.tmp", settings) is True
    assert should_ignore_path("file.custom_ext", settings) is False

    # Dynamic configuration update
    settings.IGNORED_EXTENSIONS = [".custom_ext"]

    assert should_ignore_path("file.tmp", settings) is False
    assert should_ignore_path("file.custom_ext", settings) is True


@pytest.mark.anyio
async def test_ui_debounce_starvation_max_delay(tmp_path):
    """Verify UI plan recalculation forces execution at MAX_DEBOUNCE_DELAY during continuous events."""
    from app.ui.app import AutoSorterApp

    settings = DummySettings()
    app = AutoSorterApp(settings)
    app.base_dir = str(tmp_path)
    app.app_session = mock.MagicMock()

    mock_time = 2000.0
    created_tasks = []

    def mock_create_task(coro):
        coro.close()  # prevent unawaited coroutine warning
        t = mock.MagicMock()
        t.done.return_value = False
        created_tasks.append(t)
        return t

    with (
        mock.patch("time.time", return_value=mock_time) as patch_time,
        mock.patch("asyncio.create_task", side_effect=mock_create_task),
    ):
        # 1. Event at t=0s
        app._rebuild_plan_async()
        assert app.debounce_tracker.first_event_time == 2000.0
        assert len(created_tasks) == 1

        # 2. Continuous events at t=1s, t=2s, t=4.9s
        patch_time.return_value = 2001.0
        app._rebuild_plan_async()
        assert len(created_tasks) == 2

        patch_time.return_value = 2004.9
        app._rebuild_plan_async()
        assert len(created_tasks) == 3

        # 3. Event at t=5.0s (elapsed >= MAX_DEBOUNCE_DELAY)
        # Should return early without scheduling a new task, allowing existing task to run.
        patch_time.return_value = 2005.0
        app._rebuild_plan_async()
        assert len(created_tasks) == 3  # No new task created!
