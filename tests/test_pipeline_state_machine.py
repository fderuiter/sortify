"""Tests for the SortPipeline lifecycle state machine (Issue #708).

Verifies explicit states, transition validation, event hooks, terminal result
encapsulation, mover coordination, and UI mixin concurrency control.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import AppSettings
from app.core.pipeline import (
    IllegalStateTransitionError,
    PipelineLifecycleState,
    SortPipeline,
    SortResult,
)
from app.core.session import AppSession


def _write_corpus(base: Path) -> None:
    (base / "doc1.txt").write_text("Alpha report data for research.")
    (base / "doc2.txt").write_text("Beta financial budget analysis.")
    (base / "doc3.txt").write_text("Gamma technical software code.")


@pytest.fixture
def session(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_corpus(src)
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=str(src))
    try:
        yield s
    finally:
        s.close()


def test_pipeline_lifecycle_state_enum_and_properties():
    """Verify all required enum members and helper properties."""
    expected_states = {
        "created",
        "planning",
        "planned",
        "executing",
        "completed",
        "failed",
        "cancelled",
        "rolling_back",
        "rolled_back",
    }
    actual_states = {s.value for s in PipelineLifecycleState}
    assert expected_states == actual_states

    # Terminal state checks
    assert PipelineLifecycleState.COMPLETED.is_terminal
    assert PipelineLifecycleState.FAILED.is_terminal
    assert PipelineLifecycleState.CANCELLED.is_terminal
    assert PipelineLifecycleState.ROLLED_BACK.is_terminal
    assert not PipelineLifecycleState.CREATED.is_terminal
    assert not PipelineLifecycleState.PLANNING.is_terminal
    assert not PipelineLifecycleState.PLANNED.is_terminal
    assert not PipelineLifecycleState.EXECUTING.is_terminal
    assert not PipelineLifecycleState.ROLLING_BACK.is_terminal

    # Active state checks
    assert PipelineLifecycleState.PLANNING.is_active
    assert PipelineLifecycleState.EXECUTING.is_active
    assert PipelineLifecycleState.ROLLING_BACK.is_active
    assert not PipelineLifecycleState.CREATED.is_active
    assert not PipelineLifecycleState.PLANNED.is_active
    assert not PipelineLifecycleState.COMPLETED.is_active


def test_criterion_1_execute_rejected_when_not_in_planned_state(session):
    """SortPipeline must reject execute() if not in planned state."""
    pipeline = SortPipeline(session)
    assert pipeline.state == PipelineLifecycleState.CREATED

    with pytest.raises(IllegalStateTransitionError) as exc_info:
        pipeline.execute({})
    assert "planned" in str(exc_info.value).lower()


def test_criterion_2_plan_rejected_when_execution_in_progress(session):
    """SortPipeline must reject plan() if execution is in progress."""
    pipeline = SortPipeline(session)
    pipeline.transition_to(PipelineLifecycleState.PLANNED)
    pipeline.transition_to(PipelineLifecycleState.EXECUTING)
    assert pipeline.state == PipelineLifecycleState.EXECUTING

    with pytest.raises(IllegalStateTransitionError) as exc_info:
        pipeline.plan()
    assert "execution" in str(exc_info.value).lower()


def test_criterion_3_sort_result_terminal_states_and_rollback_flag():
    """SortResult exposes terminal state and rollback completion status."""
    # Completed result
    res_completed = SortResult(state=PipelineLifecycleState.COMPLETED)
    assert res_completed.state == PipelineLifecycleState.COMPLETED
    assert res_completed.ok
    assert res_completed.rollback_completed is None

    # Cancelled result
    res_cancelled = SortResult(state=PipelineLifecycleState.CANCELLED)
    assert res_cancelled.state == PipelineLifecycleState.CANCELLED
    assert res_cancelled.cancelled
    assert not res_cancelled.ok

    # Failed result with rollback
    res_failed = SortResult(
        state=PipelineLifecycleState.FAILED,
        rollback_completed=True,
        failed=[("file.txt", "IOError")],
    )
    assert res_failed.state == PipelineLifecycleState.FAILED
    assert not res_failed.ok
    assert res_failed.rollback_completed is True

    # Rolled back result from undo
    res_rolled_back = SortResult(
        state=PipelineLifecycleState.ROLLED_BACK,
        rollback_completed=True,
    )
    assert res_rolled_back.state == PipelineLifecycleState.ROLLED_BACK
    assert res_rolled_back.ok
    assert res_rolled_back.rollback_completed is True

    # Built from summary
    summary = {
        "session_id": "test-sess",
        "cancelled": True,
        "lifecycle_state": PipelineLifecycleState.CANCELLED,
    }
    from_sum = SortResult.from_summary(summary, "_Compliance_Quarantine")
    assert from_sum.state == PipelineLifecycleState.CANCELLED
    assert from_sum.cancelled
    assert not from_sum.ok


def test_state_change_hooks_and_telemetry_broadcast(session):
    """Event hooks receive state transitions and telemetry events safely."""
    pipeline = SortPipeline(session)
    state_transitions = []
    telemetry_events = []

    def on_state_change(old_state, new_state):
        state_transitions.append((old_state, new_state))

    def on_telemetry(event, payload):
        telemetry_events.append((event, payload))

    def faulty_hook(*args):
        raise RuntimeError("Faulty subscriber should not crash pipeline")

    pipeline.add_state_change_hook(on_state_change)
    pipeline.add_state_change_hook(faulty_hook)
    pipeline.add_telemetry_hook(on_telemetry)
    pipeline.add_telemetry_hook(faulty_hook)

    plan, result = pipeline.run()

    # Verified state transitions occurred
    assert (
        PipelineLifecycleState.CREATED,
        PipelineLifecycleState.PLANNING,
    ) in state_transitions
    assert (
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.PLANNED,
    ) in state_transitions
    assert (
        PipelineLifecycleState.PLANNED,
        PipelineLifecycleState.EXECUTING,
    ) in state_transitions
    assert (
        PipelineLifecycleState.EXECUTING,
        PipelineLifecycleState.COMPLETED,
    ) in state_transitions

    # Verified telemetry events were broadcast
    event_names = [e[0] for e in telemetry_events]
    assert "plan_start" in event_names
    assert "plan_finish" in event_names
    assert "execute_start" in event_names
    assert "execute_finish" in event_names
    assert result.ok


def test_undo_state_transitions_and_failure(session):
    """Pipeline transitions through rolling_back to rolled_back or failed."""
    pipeline = SortPipeline(session)
    _plan, result = pipeline.run()
    session_id = result.session_id

    # Normal undo
    undo_result = pipeline.undo(session_id)
    assert undo_result.ok
    assert undo_result.state == PipelineLifecycleState.ROLLED_BACK
    assert undo_result.rollback_completed is True

    # Unknown session undo
    with pytest.raises(ValueError):
        pipeline.undo("non-existent-id")
    assert pipeline.state == PipelineLifecycleState.FAILED


def test_mover_cancellation_coordinates_state(session, monkeypatch):
    """Mover cooperative cancellation transitions pipeline to CANCELLED state."""
    pipeline = SortPipeline(session)
    plan = pipeline.plan()

    # Pass cancel_check that signals cancellation immediately
    result = pipeline.execute(plan, cancel_check=lambda: True)

    assert result.cancelled
    assert result.state == PipelineLifecycleState.CANCELLED
    assert pipeline.state == PipelineLifecycleState.CANCELLED


def test_mover_unhandled_exception_coordinates_rollback_to_failed(session, monkeypatch):
    """Mover execution error triggers rollback and transitions to FAILED state."""
    pipeline = SortPipeline(session)
    plan = pipeline.plan()

    # Patch _process_move_item to fail
    def fail_move(*args, **kwargs):
        raise OSError("Simulated disk error during move")

    monkeypatch.setattr("app.core.mover._process_move_item", fail_move)

    with pytest.raises(OSError):
        pipeline.execute(plan)

    assert pipeline.state == PipelineLifecycleState.FAILED
    assert getattr(pipeline, "_rollback_completed", False) is True


def test_ui_mixin_concurrency_uses_lifecycle_state():
    """SortPipelineMixin queries lifecycle state instead of thread names."""
    from app.ui.mixins.pipeline import SortPipelineMixin

    class DummyApp(SortPipelineMixin):
        def __init__(self):
            self.pipeline = None
            self.app_session = None
            self.announced = []
            self.source_dirs = []
            self.base_dir = "."

        def announce(self, msg, priority="normal"):
            self.announced.append((msg, priority))

    app = DummyApp()
    # Idle state
    assert app._running_pipeline_worker() is None
    assert not app._announce_if_busy("test_action")

    # Mock pipeline in planning state
    mock_pipeline = MagicMock()
    mock_pipeline.state = PipelineLifecycleState.PLANNING
    app.pipeline = mock_pipeline

    assert app._running_pipeline_worker() == "run_scan_worker"
    assert app._announce_if_busy("test_action")
    assert "scan is still running" in app.announced[-1][0]

    # Mock pipeline in executing state
    mock_pipeline.state = PipelineLifecycleState.EXECUTING
    assert app._running_pipeline_worker() == "run_execute_worker"
    assert app._announce_if_busy("test_action")
    assert "still being moved" in app.announced[-1][0]

    # Mock pipeline in rolling back state
    mock_pipeline.state = PipelineLifecycleState.ROLLING_BACK
    assert app._running_pipeline_worker() == "run_execute_worker"
    assert app._announce_if_busy("test_action")
    assert "rolled back" in app.announced[-1][0]


def test_session_lifecycle_validation_and_status(session):
    """AppSession tracks lifecycle_state and validates workflow invocations."""
    assert session.lifecycle_state == PipelineLifecycleState.CREATED

    # Cannot execute before planning
    with pytest.raises(IllegalStateTransitionError):
        session.execute_moves({"test": {"f.txt": {"__type__": "file"}}})

    plan = session.generate_sorting_plan()
    assert session.lifecycle_state == PipelineLifecycleState.PLANNED

    summary = session.execute_moves(plan)
    assert session.lifecycle_state == PipelineLifecycleState.COMPLETED
    assert not summary.get("cancelled")


def test_session_execution_transitions_attached_pipeline_terminal_states(
    session, monkeypatch
):
    """Session execution transitions attached pipeline and invokes state hooks for all terminal states."""
    # 1. Terminal state: COMPLETED
    pipeline = SortPipeline(session)
    plan = pipeline.plan()
    assert session.lifecycle_state == PipelineLifecycleState.PLANNED
    assert pipeline.state == PipelineLifecycleState.PLANNED

    state_transitions = []
    pipeline.add_state_change_hook(
        lambda old, new: state_transitions.append((old, new))
    )

    session.execute_moves(plan)
    assert session.lifecycle_state == PipelineLifecycleState.COMPLETED
    assert pipeline.state == PipelineLifecycleState.COMPLETED
    assert (
        PipelineLifecycleState.EXECUTING,
        PipelineLifecycleState.COMPLETED,
    ) in state_transitions


def test_session_execution_transitions_attached_pipeline_cancelled(session):
    """Session execution cancellation transitions attached pipeline to CANCELLED and triggers hook."""
    pipeline = SortPipeline(session)
    plan = pipeline.plan()
    state_transitions = []

    pipeline.add_state_change_hook(
        lambda old, new: state_transitions.append((old, new))
    )

    cancel_summary = session.execute_moves(plan, cancel_check=lambda: True)
    assert cancel_summary.get("cancelled")
    assert session.lifecycle_state == PipelineLifecycleState.CANCELLED
    assert pipeline.state == PipelineLifecycleState.CANCELLED
    assert (
        PipelineLifecycleState.EXECUTING,
        PipelineLifecycleState.CANCELLED,
    ) in state_transitions


def test_session_execution_transitions_attached_pipeline_failed_summary(
    session, monkeypatch
):
    """Session execution error summary transitions attached pipeline to FAILED and triggers hook."""
    pipeline = SortPipeline(session)
    plan = pipeline.plan()
    state_transitions = []

    pipeline.add_state_change_hook(
        lambda old, new: state_transitions.append((old, new))
    )

    monkeypatch.setattr(
        "app.core.mover.execute_moves",
        lambda *args, **kwargs: {
            "error": "Disk I/O error",
            "lifecycle_state": PipelineLifecycleState.FAILED,
        },
    )
    fail_summary = session.execute_moves(plan)
    assert fail_summary.get("error")
    assert session.lifecycle_state == PipelineLifecycleState.FAILED
    assert pipeline.state == PipelineLifecycleState.FAILED
    assert (
        PipelineLifecycleState.EXECUTING,
        PipelineLifecycleState.FAILED,
    ) in state_transitions


def test_session_execution_transitions_attached_pipeline_failed_exception(
    session, monkeypatch
):
    """Session execution exception transitions attached pipeline to FAILED and triggers hook."""
    pipeline = SortPipeline(session)
    plan = pipeline.plan()
    state_transitions = []

    pipeline.add_state_change_hook(
        lambda old, new: state_transitions.append((old, new))
    )

    def raise_move_error(*args, **kwargs):
        raise OSError("Critical hardware failure")

    monkeypatch.setattr("app.core.mover.execute_moves", raise_move_error)
    with pytest.raises(OSError, match="Critical hardware failure"):
        session.execute_moves(plan)

    assert session.lifecycle_state == PipelineLifecycleState.FAILED
    assert pipeline.state == PipelineLifecycleState.FAILED
    assert (
        PipelineLifecycleState.EXECUTING,
        PipelineLifecycleState.FAILED,
    ) in state_transitions


def test_execute_moves_rejected_from_unvalidated_states(session):
    """execute_moves rejects invocations from any unvalidated lifecycle state."""
    plan = {"dummy": {"file.txt": {"__type__": "file"}}}
    unvalidated_states = [
        PipelineLifecycleState.CREATED,
        PipelineLifecycleState.PLANNING,
        PipelineLifecycleState.COMPLETED,
        PipelineLifecycleState.FAILED,
        PipelineLifecycleState.CANCELLED,
        PipelineLifecycleState.ROLLING_BACK,
        PipelineLifecycleState.ROLLED_BACK,
    ]

    # Test standalone session (no pipeline attached)
    for state in unvalidated_states:
        session.lifecycle_state = state
        with pytest.raises(IllegalStateTransitionError) as exc_info:
            session.execute_moves(plan)
        assert "planned" in str(exc_info.value).lower()

    # Test with attached pipeline
    pipeline = SortPipeline(session)
    for state in unvalidated_states:
        session.lifecycle_state = state
        pipeline._state = state
        with pytest.raises(IllegalStateTransitionError) as exc_info:
            session.execute_moves(plan)
        assert "planned" in str(exc_info.value).lower()


def test_session_standalone_transition_validation(session):
    """Sessions operating without an attached SortPipeline validate transitions locally."""
    assert session.pipeline is None
    assert session.lifecycle_state == PipelineLifecycleState.CREATED

    # Invalid move directly from CREATED to EXECUTING
    with pytest.raises(IllegalStateTransitionError):
        session.transition_to(PipelineLifecycleState.EXECUTING)

    # Valid transition to PLANNING
    session.transition_to(PipelineLifecycleState.PLANNING)
    assert session.lifecycle_state == PipelineLifecycleState.PLANNING

    # No-op when transitioning to current state
    session.transition_to(PipelineLifecycleState.PLANNING)
    assert session.lifecycle_state == PipelineLifecycleState.PLANNING

    # Valid transition to PLANNED
    session.transition_to(PipelineLifecycleState.PLANNED)
    assert session.lifecycle_state == PipelineLifecycleState.PLANNED

    # Invalid transition from PLANNED to CREATED
    with pytest.raises(IllegalStateTransitionError):
        session.transition_to(PipelineLifecycleState.CREATED)

    # Valid transition to EXECUTING
    session.transition_to(PipelineLifecycleState.EXECUTING)
    assert session.lifecycle_state == PipelineLifecycleState.EXECUTING

    # Valid transition to COMPLETED
    session.transition_to(PipelineLifecycleState.COMPLETED)
    assert session.lifecycle_state == PipelineLifecycleState.COMPLETED


def test_session_standalone_exception_transitions_to_failed(session, monkeypatch):
    """Standalone session execution exception transitions session to FAILED before re-raising."""
    assert session.pipeline is None
    plan = session.generate_sorting_plan()
    assert session.lifecycle_state == PipelineLifecycleState.PLANNED

    monkeypatch.setattr(
        "app.core.mover.execute_moves",
        MagicMock(side_effect=RuntimeError("Mover failure without pipeline")),
    )
    with pytest.raises(RuntimeError, match="Mover failure without pipeline"):
        session.execute_moves(plan)

    assert session.lifecycle_state == PipelineLifecycleState.FAILED
