"""Unit tests for the unified lifecycle telemetry mixin across core asynchronous workflows."""

import threading
from unittest.mock import MagicMock

import pytest

from app.core.daemon import ContinuousWatchdogDaemon, DaemonState, InvalidStateError
from app.core.downloader import DownloadManager, DownloadState
from app.core.history import SessionStateMachine
from app.core.ledger import LedgerStatus, TransactionLedger
from app.core.lifecycle import (
    LifecycleTelemetryMixin,
    sanitize_telemetry_payload,
)
from app.core.mover import AsyncMoveEngine, InvalidMoveStateError, MoveEngineState
from app.core.pipeline import SortPipeline
from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
    CROMultiStudyPipeline,
    CROPipelineState,
)


class TestLifecycleTelemetryMixin:
    """Tests for base LifecycleTelemetryMixin functionality."""

    def test_sanitize_telemetry_payload_redacts_sensitive_keys(self):
        payload = {
            "public_id": "test-123",
            "password": "supersecretpassword",
            "api_key": "12345-abcdef",
            "access_token": "bearer xyz987",
            "user": {
                "name": "Alice",
                "secret_field": "confidential",
            },
            "items": [
                {"credential_info": "privkey"},
                "plain_string",
            ],
        }
        sanitized = sanitize_telemetry_payload(payload)
        assert sanitized["public_id"] == "test-123"
        assert sanitized["password"] == "[REDACTED]"
        assert sanitized["api_key"] == "[REDACTED]"
        assert sanitized["access_token"] == "[REDACTED]"
        assert sanitized["user"]["name"] == "Alice"
        assert sanitized["user"]["secret_field"] == "[REDACTED]"
        assert sanitized["items"][0]["credential_info"] == "[REDACTED]"
        assert sanitized["items"][1] == "plain_string"

    def test_sanitize_telemetry_payload_redacts_secret_patterns_in_values(self):
        payload = {
            "notes": "Authorization: Bearer my-secret-jwt-token-value-here",
            "gh_key": "ghp_123456789012345678901234567890123456",
        }
        sanitized = sanitize_telemetry_payload(payload)
        assert "[REDACTED" in sanitized["notes"] or sanitized["notes"] == "[REDACTED]"
        assert "[REDACTED" in sanitized["gh_key"] or sanitized["gh_key"] == "[REDACTED]"

    def test_listener_exception_isolation(self):
        class SampleWorkflow(LifecycleTelemetryMixin):
            def trigger(self):
                self._notify_lifecycle_listeners(
                    old_state="OLD",
                    new_state="NEW",
                    event_type="SAMPLE_EVENT",
                )

        wf = SampleWorkflow()
        healthy_events = []

        def failing_listener(event):
            raise RuntimeError("Fatal explosion inside listener!")

        def healthy_listener(event):
            healthy_events.append(event)

        wf.add_transition_listener(failing_listener)
        wf.add_transition_listener(healthy_listener)

        # Trigger should complete cleanly without raising
        wf.trigger()

        assert len(healthy_events) == 1
        assert healthy_events[0]["event_type"] == "SAMPLE_EVENT"
        assert healthy_events[0]["old_state"] == "OLD"
        assert healthy_events[0]["new_state"] == "NEW"

    def test_telemetry_hooks_and_state_change_hooks(self):
        class SampleWorkflow(LifecycleTelemetryMixin):
            pass

        wf = SampleWorkflow()
        state_changes = []
        telemetry_events = []

        wf.add_state_change_hook(lambda old, new: state_changes.append((old, new)))
        wf.add_telemetry_hook(lambda evt: telemetry_events.append(evt))

        wf._notify_lifecycle_listeners(old_state="A", new_state="B", event_type="TEST")
        wf.emit_telemetry("CUSTOM_METRIC", {"metric_val": 42})

        assert state_changes == [("A", "B")]
        assert len(telemetry_events) == 2
        assert telemetry_events[0]["event_type"] == "TEST"
        assert telemetry_events[1]["event_type"] == "CUSTOM_METRIC"
        assert telemetry_events[1]["metric_val"] == 42

    def test_listener_registration_and_unregistration(self):
        wf = LifecycleTelemetryMixin()
        events = []

        def listener(evt):
            events.append(evt)

        wf.add_transition_listener(listener)
        wf._notify_lifecycle_listeners("S1", "S2")
        assert len(events) == 1

        wf.remove_transition_listener(listener)
        wf._notify_lifecycle_listeners("S2", "S3")
        assert len(events) == 1

    def test_thread_safe_listener_execution(self):
        wf = LifecycleTelemetryMixin()
        collected = []
        lock = threading.Lock()

        def listener(evt):
            with lock:
                collected.append(evt)

        wf.add_transition_listener(listener)

        threads = []
        for i in range(10):
            t = threading.Thread(
                target=lambda idx: wf._notify_lifecycle_listeners(
                    f"S{idx}", f"S{idx + 1}"
                ),
                args=(i,),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert len(collected) == 10


class DummySettings:
    def __init__(self):
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.AI_CONSENT_GRANTED = False
        self.POLICIES = []


class TestWatchdogDaemonLifecycle:
    """Tests for ContinuousWatchdogDaemon state validation and telemetry."""

    def test_daemon_inherits_lifecycle_mixin(self, tmp_path):
        daemon = ContinuousWatchdogDaemon(DummySettings(), str(tmp_path))
        assert isinstance(daemon, LifecycleTelemetryMixin)

    def test_daemon_validates_is_running_setter_transitions(self, tmp_path):
        daemon = ContinuousWatchdogDaemon(DummySettings(), str(tmp_path))
        assert daemon.state == DaemonState.STOPPED

        events = []
        daemon.add_transition_listener(lambda evt: events.append(evt))

        # STOPPED -> RUNNING via setter
        daemon._is_running = True
        assert daemon.state == DaemonState.RUNNING
        assert daemon.is_running is True
        assert len(events) == 1
        assert events[0]["old_state"] == DaemonState.STOPPED.value
        assert events[0]["new_state"] == DaemonState.RUNNING.value
        assert events[0]["event_type"] == "DAEMON_STATE_CHANGED"

        # RUNNING -> STOPPED via setter
        daemon._is_running = False
        assert daemon.state == DaemonState.STOPPED
        assert daemon.is_running is False
        assert len(events) == 2
        assert events[1]["old_state"] == DaemonState.RUNNING.value
        assert events[1]["new_state"] == DaemonState.STOPPED.value

    def test_daemon_invalid_transition_raises_error(self, tmp_path):
        daemon = ContinuousWatchdogDaemon(DummySettings(), str(tmp_path))
        daemon._transition_to(DaemonState.RUNNING)
        daemon._transition_to(DaemonState.PAUSED)
        assert daemon.state == DaemonState.PAUSED

        with pytest.raises(InvalidStateError):
            daemon._transition_to("NOT_A_STATE")


class TestDownloadManagerLifecycle:
    """Tests for DownloadManager state validation and telemetry."""

    def test_downloader_inherits_lifecycle_mixin(self):
        dm = DownloadManager()
        assert isinstance(dm, LifecycleTelemetryMixin)

    def test_downloader_state_transitions_and_telemetry(self):
        dm = DownloadManager()
        assert dm.status == DownloadState.IDLE

        events = []
        dm.add_transition_listener(lambda evt: events.append(evt))

        # IDLE -> DOWNLOADING
        with dm._lock:
            dm._transition_to_locked(DownloadState.DOWNLOADING)
        assert dm.status == DownloadState.DOWNLOADING
        assert len(events) == 1
        assert events[0]["old_state"] == DownloadState.IDLE.value
        assert events[0]["new_state"] == DownloadState.DOWNLOADING.value

        # DOWNLOADING -> CANCELLED via cancel_download
        dm.cancel_download()
        assert dm.status == DownloadState.CANCELLED
        assert len(events) == 2
        assert events[1]["old_state"] == DownloadState.DOWNLOADING.value
        assert events[1]["new_state"] == DownloadState.CANCELLED.value

    def test_downloader_cancel_idle_does_not_bypass(self):
        dm = DownloadManager()
        assert dm.status == DownloadState.IDLE

        events = []
        dm.add_transition_listener(lambda evt: events.append(evt))

        # Cancelling while IDLE should not assign DOWNLOADING bypass
        dm.cancel_download()
        assert dm.status == DownloadState.IDLE
        assert len(events) == 0

    def test_downloader_invalid_transition_rejected(self):
        dm = DownloadManager()
        with pytest.raises(ValueError, match="Invalid state transition"):
            with dm._lock:
                dm._transition_to_locked(DownloadState.CANCELLED)


class TestCROMultiStudyPipelineLifecycle:
    """Tests for CROMultiStudyPipeline state transitions and telemetry."""

    def test_cro_pipeline_inherits_lifecycle_mixin(self):
        pipeline = CROMultiStudyPipeline()
        assert isinstance(pipeline, LifecycleTelemetryMixin)

    def test_cro_pipeline_transitions_to_failed_safely(self):
        pipeline = CROMultiStudyPipeline()
        assert pipeline.state == CROPipelineState.IDLE

        events = []
        pipeline.add_transition_listener(lambda evt: events.append(evt))

        # Transitioning to FAILED from IDLE is allowed
        with pipeline._state_lock:
            pipeline._transition_to_locked(CROPipelineState.FAILED)

        assert pipeline.state == CROPipelineState.FAILED
        assert len(events) == 1
        assert events[0]["old_state"] == CROPipelineState.IDLE.value
        assert events[0]["new_state"] == CROPipelineState.FAILED.value
        assert events[0]["event_type"] == "CRO_PIPELINE_STATE_CHANGED"

    def test_cro_pipeline_run_failure_transitions_cleanly(self, tmp_path, monkeypatch):
        pipeline = CROMultiStudyPipeline()
        monkeypatch.setattr(
            pipeline.scanner,
            "scan_drive",
            MagicMock(side_effect=RuntimeError("Disk failure")),
        )
        events = []
        pipeline.add_transition_listener(lambda evt: events.append(evt))

        with pytest.raises(RuntimeError):
            pipeline.run_pipeline(
                str(tmp_path / "nonexistent_source"),
                str(tmp_path / "nonexistent_target"),
            )
        assert pipeline.state == CROPipelineState.FAILED

        # Ensure FAILED state transition was notified
        failed_events = [
            e for e in events if e.get("new_state") == CROPipelineState.FAILED.value
        ]
        assert len(failed_events) == 1


class TestAsyncMoveEngineLifecycle:
    """Tests for AsyncMoveEngine state validation and telemetry."""

    def test_move_engine_inherits_lifecycle_mixin(self):
        engine = AsyncMoveEngine()
        assert isinstance(engine, LifecycleTelemetryMixin)

    def test_move_engine_state_transitions_and_telemetry(self):
        engine = AsyncMoveEngine()
        assert engine.state == MoveEngineState.IDLE

        events = []
        engine.add_transition_listener(lambda evt: events.append(evt))

        engine.transition_to(MoveEngineState.PREPARING)
        assert engine.state == MoveEngineState.PREPARING
        assert len(events) == 1
        assert events[0]["old_state"] == MoveEngineState.IDLE.value
        assert events[0]["new_state"] == MoveEngineState.PREPARING.value
        assert events[0]["event_type"] == "MOVE_ENGINE_STATE_CHANGED"

        engine.transition_to(MoveEngineState.MOVING_CHUNK)
        assert engine.state == MoveEngineState.MOVING_CHUNK
        assert len(events) == 2

    def test_move_engine_invalid_transition_raises_error(self):
        engine = AsyncMoveEngine()
        with pytest.raises(InvalidMoveStateError):
            engine.transition_to(MoveEngineState.COMPLETED)

    def test_move_engine_execute_lifecycle_events(self, tmp_path):
        base_dir = str(tmp_path)
        fpath = tmp_path / "source.txt"
        fpath.write_text("hello world")

        plan = {
            "source.txt": {
                "__type__": "file",
                "relative_source": "source.txt",
                "target_filename": "dest.txt",
                "status": "Confirmed",
            }
        }

        class DummyDB:
            def __init__(self):
                self.lock = threading.Lock()

            def get_document(self, base_dir, key):
                return {"file_hash": "dummy_hash"}

            def execute_batch_updates(self, updates):
                pass

            def set_user_verified_target(self, base_dir, file_hash, dest):
                pass

            def update_document_path(self, base_dir, src, dst):
                pass

            def record_transaction_step(self, *args):
                pass

        class DummyHistoryManager:
            def __init__(self):
                self.steps = []
                self.lock = threading.Lock()

            def create_snapshot(self, base_dir):
                return "test-session-123"

            def log_step(self, **kwargs):
                self.steps.append(kwargs)

            def clear_step_ledger(self, session_id):
                pass

        engine = AsyncMoveEngine()
        events = []
        engine.add_transition_listener(lambda evt: events.append(evt))

        summary = engine.execute(
            base_dir,
            plan,
            db=DummyDB(),
            history_manager=DummyHistoryManager(),
        )
        assert not summary.get("cancelled")

        event_types = [e["event_type"] for e in events]
        assert "MOVE_ENGINE_STATE_CHANGED" in event_types
        assert "move_start" in event_types
        assert "move_finish" in event_types


class TestTransactionLedgerLifecycle:
    """Tests for TransactionLedger status changes and telemetry."""

    def test_ledger_inherits_lifecycle_mixin(self, tmp_path):
        ledger = TransactionLedger(db_path=str(tmp_path / "ledger.db"))
        assert isinstance(ledger, LifecycleTelemetryMixin)

    def test_ledger_status_update_emits_telemetry(self, tmp_path):
        ledger = TransactionLedger(db_path=str(tmp_path / "ledger.db"))
        events = []
        ledger.add_transition_listener(lambda evt: events.append(evt))

        entry_id = ledger.log_intent(
            session_id="sess_123",
            base_dir=str(tmp_path),
            source_path=str(tmp_path / "file1.txt"),
            dest_path=str(tmp_path / "dest1.txt"),
            source_rel_path="file1.txt",
            dest_rel_path="dest1.txt",
        )

        ledger.update_status(entry_id, LedgerStatus.MOVED_PHYSICAL)

        status_events = [
            e for e in events if e.get("event_type") == "LEDGER_STATUS_CHANGED"
        ]
        assert len(status_events) == 1
        assert status_events[0]["entry_id"] == entry_id
        assert status_events[0]["old_status"] == LedgerStatus.PENDING.value
        assert status_events[0]["new_status"] == LedgerStatus.MOVED_PHYSICAL.value

    def test_ledger_reconciliation_lifecycle_events(self, tmp_path):
        ledger = TransactionLedger(db_path=str(tmp_path / "ledger.db"))
        events = []
        ledger.add_transition_listener(lambda evt: events.append(evt))

        ledger.log_intent(
            session_id="sess_rec",
            base_dir=str(tmp_path),
            source_path=str(tmp_path / "file_pending.txt"),
            dest_path=str(tmp_path / "dest_pending.txt"),
            source_rel_path="file_pending.txt",
            dest_rel_path="dest_pending.txt",
        )

        reconciled = ledger.reconcile_incomplete_transactions()
        assert reconciled == 1

        event_types = [e["event_type"] for e in events]
        assert "reconciliation_start" in event_types
        assert "reconciliation_finish" in event_types


class TestSessionAndPipelineLifecycle:
    """Tests verifying backward compatibility and mixin inheritance for SessionStateMachine and SortPipeline."""

    def test_session_state_machine_inherits_lifecycle_mixin(self):
        machine = SessionStateMachine()
        assert isinstance(machine, LifecycleTelemetryMixin)

    def test_sort_pipeline_inherits_lifecycle_mixin(self):
        pipeline = SortPipeline(session=MagicMock())
        assert isinstance(pipeline, LifecycleTelemetryMixin)
