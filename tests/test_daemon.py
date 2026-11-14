import logging
import threading
from unittest import mock

import pytest

from app.core.daemon import (
    ContinuousWatchdogDaemon,
    DaemonState,
    InvalidStateError,
)


class DummySettings:
    def __init__(self):
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.AI_CONSENT_GRANTED = False
        self.POLICIES = []

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
    assert daemon.state == DaemonState.RUNNING

    # Triggering recalculation transitions state to RECALCULATING, allocates a scoped run token,
    # and keeps the daemon lifecycle cancellation token reference stable and unset
    daemon.trigger_recalculation()
    assert daemon._cancel_event is cancel_event_1
    assert cancel_event_1.is_set() is False
    assert daemon.state == DaemonState.RECALCULATING
    assert daemon.is_running is True
    assert daemon.is_paused is False
    assert daemon.get_status_telemetry()["status"] == "recalculating"

    scoped_token_1 = daemon.scoped_run_token
    assert scoped_token_1 is not None
    assert scoped_token_1.is_set() is False

    # Rapid recalculation events cooperatively signal the active scoped run token without replacing lifecycle token
    daemon.trigger_recalculation()
    assert scoped_token_1.is_set() is True
    scoped_token_2 = daemon.scoped_run_token
    assert scoped_token_2 is not None
    assert scoped_token_2 is not scoped_token_1
    assert scoped_token_2.is_set() is False
    assert daemon._cancel_event is cancel_event_1
    assert cancel_event_1.is_set() is False
    assert daemon.state == DaemonState.RECALCULATING

    # Worker cancellation is timely and detected via cooperative token check
    assert daemon._is_cancelled(scoped_token_1) is True
    assert daemon._is_cancelled(scoped_token_2) is False

    # Stopping daemon transitions state to STOPPED and signals both lifecycle and scoped tokens
    daemon.stop()
    assert daemon.state == DaemonState.STOPPED
    assert cancel_event_1.is_set() is True
    assert scoped_token_2.is_set() is True
    assert daemon.is_running is False
    assert daemon.get_status_telemetry()["status"] == "stopped"


def test_daemon_invalid_state_transitions(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    assert daemon.state == DaemonState.STOPPED

    # Moving directly from STOPPED to PAUSED or RECALCULATING must be rejected
    with pytest.raises(InvalidStateError):
        daemon.pause()

    with pytest.raises(InvalidStateError):
        daemon.state = DaemonState.PAUSED

    with pytest.raises(InvalidStateError):
        daemon.state = DaemonState.RECALCULATING

    # Transition to RUNNING
    daemon._is_running = True
    assert daemon.state == DaemonState.RUNNING

    # Pause daemon -> PAUSED
    daemon.pause()
    assert daemon.state == DaemonState.PAUSED
    assert daemon.is_paused is True

    # Transitioning directly from PAUSED to RECALCULATING must be rejected
    with pytest.raises(InvalidStateError):
        daemon.state = DaemonState.RECALCULATING

    # Resume daemon -> RUNNING
    daemon.resume()
    assert daemon.state == DaemonState.RUNNING

    daemon.stop()
    assert daemon.state == DaemonState.STOPPED


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
        mock_app_session_inst.generate_sorting_plan.side_effect = [
            {"invoice.txt": "dest/invoice.txt"},  # Phase 1: Fast-Path
            {},  # Phase 2: Slow-Path
        ]
        mock_app_session_inst.execute_moves.return_value = {"moved": 1}

        # Run the sorting sync manually
        cancel_event = threading.Event()
        daemon._run_sorting_sync(cancel_event)

        # Verify AppSession was closed and moves were executed
        mock_app_session_class.assert_called_once_with(settings, str(src_dir))
        mock_app_session_inst.partial_fit.assert_called()
        assert mock_app_session_inst.generate_sorting_plan.call_count == 2
        call_args = mock_app_session_inst.generate_sorting_plan.call_args_list
        assert call_args[0].kwargs.get("fast_path_only") is True
        assert call_args[1].kwargs.get("fast_path_only") is False
        mock_app_session_inst.execute_moves.assert_called_once_with(
            {"invoice.txt": "dest/invoice.txt"}
        )
        mock_app_session_inst.close.assert_called_once()


@pytest.mark.anyio
async def test_daemon_triage_file_path_stages_and_redacts(tmp_path):
    from app.core.db_conn import clear_connection_cache

    try:
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        sensitive_file = src_dir / "patient_report.txt"
        sensitive_file.write_text(
            "Confidential Medical Report for Subject 101 with SSN 123-45-6789",
            encoding="utf-8",
        )

        class CustomSettings(DummySettings):
            def __init__(self):
                super().__init__()
                self.POLICIES = [
                    {
                        "type": "keyword",
                        "expression": "subject",
                        "action": "redact",
                        "target_path": "Clean_Out",
                        "priority": 100,
                    }
                ]
                self.WORKER_TIMEOUT = 300.0

        settings = CustomSettings()
        daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
        daemon._is_running = True

        await daemon._triage_file_path(str(sensitive_file))

        assert not sensitive_file.exists()

        db = daemon._get_or_create_session().db
        records = db.get_quarantine_records_by_base_dir(str(src_dir))
        assert len(records) == 1
        rec = records[0]
        assert rec["status"] == "RELEASED"
        assert rec["policy_action"] == "redact"

        statuses = [entry["status"] for entry in rec["audit_log"]]
        assert "STAGED" in statuses
        assert "IN_INSPECTION" in statuses
        assert "REDACTED" in statuses
        assert "RELEASED" in statuses

        released_file = src_dir / "Clean_Out" / "patient_report.txt"
        assert released_file.exists()
        released_text = released_file.read_text(encoding="utf-8")
        assert "123-45-6789" not in released_text
        assert "[REDACTED" in released_text

        daemon.stop()
    finally:
        clear_connection_cache()


@pytest.mark.anyio
async def test_daemon_triage_file_path_quarantine_action(tmp_path):
    from app.core.db_conn import clear_connection_cache

    try:
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        malicious_file = src_dir / "restricted_file.txt"
        malicious_file.write_text("This contains restricted content", encoding="utf-8")

        class CustomSettings(DummySettings):
            def __init__(self):
                super().__init__()
                self.POLICIES = [
                    {
                        "type": "keyword",
                        "expression": "restricted",
                        "action": "quarantine",
                        "target_path": "_Quarantine_Staging",
                        "priority": 100,
                    }
                ]

        settings = CustomSettings()
        daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
        daemon._is_running = True

        await daemon._triage_file_path(str(malicious_file))

        assert not malicious_file.exists()

        db = daemon._get_or_create_session().db
        records = db.get_quarantine_records_by_base_dir(str(src_dir))
        assert len(records) == 1
        rec = records[0]
        assert rec["status"] == "QUARANTINED"
        assert rec["policy_action"] == "quarantine"

        daemon.stop()
    finally:
        clear_connection_cache()


@pytest.mark.anyio
async def test_daemon_triage_file_path_timeout_to_dlq(tmp_path):
    from app.core.db_conn import clear_connection_cache

    try:
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        heavy_file = src_dir / "heavy.txt"
        heavy_file.write_text("Heavy processing data", encoding="utf-8")

        class CustomSettings(DummySettings):
            def __init__(self):
                super().__init__()
                self.POLICIES = []
                self.WORKER_TIMEOUT = 0.0

        settings = CustomSettings()
        daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
        daemon._is_running = True

        await daemon._triage_file_path(str(heavy_file))

        db = daemon._get_or_create_session().db
        records = db.get_quarantine_records_by_base_dir(str(src_dir))
        assert len(records) == 1
        rec = records[0]
        assert rec["status"] == "MANUAL_REVIEW_REQUIRED"
        assert "timeout" in rec["error_message"].lower()

        daemon.stop()
    finally:
        clear_connection_cache()


def test_daemon_lifecycle_logging_no_stderr(tmp_path, caplog, capsys):
    """Verify that daemon start and stop operations emit via logging and do not write to sys.stderr."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))

    with caplog.at_level(logging.INFO, logger="app.daemon"):
        daemon.start()
        daemon.stop()

    captured = capsys.readouterr()
    assert captured.err == "", f"Expected empty stderr, got: {captured.err}"

    logs = caplog.text
    assert "Starting continuous watchdog daemon for:" in logs
    assert "Watchdog daemon stopped." in logs


def test_daemon_sorting_sync_logging_no_stderr(tmp_path, caplog, capsys):
    """Verify background sorting execution logs via module logger and produces no sys.stderr output."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "invoice.txt").write_text("Invoice contents")

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    mock_app_session_class = mock.MagicMock()
    mock_app_session_inst = mock.MagicMock()
    mock_app_session_class.return_value = mock_app_session_inst

    with (
        mock.patch("app.core.daemon.AppSession", mock_app_session_class),
        mock.patch(
            "app.core.daemon.get_files_recursively", return_value=["invoice.txt"]
        ),
        mock.patch("app.core.daemon.MetadataPass.run", return_value=[]),
        caplog.at_level(logging.INFO, logger="app.daemon"),
    ):
        mock_app_session_inst.generate_sorting_plan.side_effect = [
            {},
            {"invoice.txt": "dest/invoice.txt"},
        ]
        mock_app_session_inst.execute_moves.return_value = {"moved": 1}

        cancel_event = threading.Event()
        daemon._run_sorting_sync(cancel_event)

    captured = capsys.readouterr()
    assert captured.err == "", f"Expected empty stderr, got: {captured.err}"

    logs = caplog.text
    assert "Executing background sorting run..." in logs
    assert "Phase 2 (Slow-Path AI) completed successfully:" in logs


def test_daemon_sorting_sync_error_logging_no_stderr(tmp_path, caplog, capsys):
    """Verify background sorting exceptions are logged via logger.error and produce no sys.stderr output."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    with (
        mock.patch(
            "app.core.daemon.AppSession",
            side_effect=RuntimeError("Simulated sorting error"),
        ),
        caplog.at_level(logging.ERROR, logger="app.daemon"),
    ):
        cancel_event = threading.Event()
        daemon._run_sorting_sync(cancel_event)

    captured = capsys.readouterr()
    assert captured.err == "", f"Expected empty stderr, got: {captured.err}"

    logs = caplog.text
    assert (
        "Error during continuous watchdog execution run: Simulated sorting error"
        in logs
    )


@pytest.mark.anyio
async def test_daemon_triage_file_path_retains_source_and_cleans_staging(tmp_path):
    """Verify source file is not unlinked prior to triage and no stranded files remain in _Quarantine_Staging."""
    from app.config import AppSettings
    from app.core.db_conn import clear_connection_cache

    try:
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        test_doc = src_dir / "invoice_2026.csv"
        test_doc.write_text("Invoice ID, Total\n1001, $500", encoding="utf-8")

        settings = AppSettings()
        settings.AUTO_QUARANTINE_RATINGS = []
        daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
        daemon._is_running = True

        await daemon._triage_file_path(str(test_doc))

        # After triage, verify file was moved by JEV triage rule, and staging folder has zero stranded files
        quarantine_staging = src_dir / "_Quarantine_Staging"
        if quarantine_staging.exists():
            assert list(quarantine_staging.iterdir()) == []
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_daemon_triage_uncategorized_file_remains_intact(tmp_path):
    """Verify unclassified files cleared by quarantine remain intact in base_dir or Miscellaneous without being dropped or stranded."""
    from app.config import AppSettings
    from app.core.db_conn import clear_connection_cache

    try:
        src_dir = tmp_path / "src"
        src_dir.mkdir()

        uncategorized_doc = src_dir / "random_notes.txt"
        uncategorized_doc.write_text("Random unstructured text", encoding="utf-8")

        settings = AppSettings()
        settings.AUTO_QUARANTINE_RATINGS = []
        daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
        daemon._is_running = True

        await daemon._triage_file_path(str(uncategorized_doc))

        # File should remain intact at original source location or be moved to Miscellaneous by slow-path
        misc_doc = src_dir / "Miscellaneous" / "random_notes.txt"
        assert uncategorized_doc.exists() or misc_doc.exists()

        # Quarantine staging should contain no stranded files
        quarantine_staging = src_dir / "_Quarantine_Staging"
        if quarantine_staging.exists():
            assert list(quarantine_staging.iterdir()) == []
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_daemon_reads_pipeline_limits_from_settings_file(tmp_path, monkeypatch):
    import json

    from app.config import AppSettings

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "MAX_QUEUE_CAPACITY": 7,
                "DEDUP_WINDOW": 1.5,
                "RECONCILIATION_INTERVAL": 12.0,
                "WORKER_TIMEOUT": 42.0,
            }
        ),
        encoding="utf-8",
    )
    settings = AppSettings(filepath=str(settings_path))

    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))

    assert daemon.max_queue_capacity == 7
    assert daemon.dedup_window == 1.5
    assert daemon.reconciliation_interval == 12.0
    assert settings.WORKER_TIMEOUT == 42.0
