"""Unit tests for session lifecycle state machine, transactional rollback error handling, and recovery catalog detection."""

import os
import shutil
import time
from unittest.mock import patch

import pytest

from app.core.db import DBWorker
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.exceptions import InvalidStateTransitionError
from app.core.history import (
    ACTIVE,
    COMPLETED,
    FAILED_STATUS,
    PARTIALLY_ROLLED_BACK,
    ROLLED_BACK,
    UNDO_SNAPSHOT,
    SessionStateMachine,
)
from app.core.session_catalog import find_abandoned_sessions, mark_session_abandoned
from app.core.store import open_store


@pytest.fixture
def db_worker():
    worker = DBWorker()
    yield worker
    worker.stop()


@pytest.fixture
def lifecycle_env(tmp_path, db_worker):
    """Set up an isolated history and cache store in tmp_path."""
    clear_connection_cache()
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    store = open_store(app_dir=app_dir)

    database = store.database(db_worker)
    cache = store.cache(db_worker)
    history_manager = store.history(database, cache)

    base_dir = tmp_path / "workspace"
    base_dir.mkdir(parents=True, exist_ok=True)

    yield {
        "tmp_path": tmp_path,
        "base_dir": str(base_dir),
        "db": database,
        "history": history_manager,
        "store": store,
        "db_path": str(store.path),
    }
    clear_connection_cache()


def test_transition_matrix_valid_transitions():
    """Verify all valid lifecycle transitions pass validation."""
    sm = SessionStateMachine()

    valid_pairs = [
        (ACTIVE, COMPLETED),
        (ACTIVE, ROLLED_BACK),
        (ACTIVE, PARTIALLY_ROLLED_BACK),
        (ACTIVE, FAILED_STATUS),
        (ACTIVE, UNDO_SNAPSHOT),
        (ACTIVE, "abandoned"),
        (ACTIVE, ACTIVE),
        (COMPLETED, ROLLED_BACK),
        (COMPLETED, PARTIALLY_ROLLED_BACK),
        (COMPLETED, FAILED_STATUS),
        (COMPLETED, "abandoned"),
        (COMPLETED, COMPLETED),
        (PARTIALLY_ROLLED_BACK, ROLLED_BACK),
        (PARTIALLY_ROLLED_BACK, PARTIALLY_ROLLED_BACK),
        (PARTIALLY_ROLLED_BACK, FAILED_STATUS),
        (PARTIALLY_ROLLED_BACK, "abandoned"),
        (FAILED_STATUS, ROLLED_BACK),
        (FAILED_STATUS, PARTIALLY_ROLLED_BACK),
        (FAILED_STATUS, FAILED_STATUS),
        (FAILED_STATUS, "abandoned"),
        (FAILED_STATUS, ACTIVE),
        (FAILED_STATUS, COMPLETED),
        (UNDO_SNAPSHOT, ROLLED_BACK),
        (UNDO_SNAPSHOT, FAILED_STATUS),
        (UNDO_SNAPSHOT, "abandoned"),
        (UNDO_SNAPSHOT, UNDO_SNAPSHOT),
        ("abandoned", ROLLED_BACK),
        ("abandoned", PARTIALLY_ROLLED_BACK),
        ("abandoned", FAILED_STATUS),
        ("abandoned", "abandoned"),
        (ROLLED_BACK, ROLLED_BACK),
    ]

    for current_status, new_status in valid_pairs:
        # Must not raise InvalidStateTransitionError
        sm.validate_transition(current_status, new_status)


def test_transition_matrix_invalid_transitions_raise_domain_exception():
    """Verify invalid lifecycle transitions reject and raise InvalidStateTransitionError (Criterion 1)."""
    sm = SessionStateMachine()

    invalid_pairs = [
        (COMPLETED, ACTIVE),
        (COMPLETED, UNDO_SNAPSHOT),
        (ROLLED_BACK, ACTIVE),
        (ROLLED_BACK, COMPLETED),
        (ROLLED_BACK, PARTIALLY_ROLLED_BACK),
        (ROLLED_BACK, FAILED_STATUS),
        (ROLLED_BACK, UNDO_SNAPSHOT),
        (ROLLED_BACK, "abandoned"),
        (UNDO_SNAPSHOT, ACTIVE),
        (UNDO_SNAPSHOT, COMPLETED),
        (UNDO_SNAPSHOT, PARTIALLY_ROLLED_BACK),
        ("abandoned", ACTIVE),
        ("abandoned", COMPLETED),
        ("abandoned", UNDO_SNAPSHOT),
    ]

    for current_status, new_status in invalid_pairs:
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            sm.validate_transition(current_status, new_status)
        assert "Invalid session lifecycle transition" in str(exc_info.value)


def test_transition_validation_performance():
    """Verify transition validation executes in well under 5ms (<5ms constraint)."""
    sm = SessionStateMachine()
    start = time.perf_counter()
    for _ in range(1000):
        sm.validate_transition(ACTIVE, COMPLETED)
        sm.validate_transition(COMPLETED, ROLLED_BACK)
    elapsed = time.perf_counter() - start
    avg_ms = (elapsed / 2000) * 1000
    # Each validation must be far below 5ms
    assert avg_ms < 0.1, f"Validation too slow: {avg_ms:.4f} ms per call"


def test_step_unwind_failure_transitions_to_failed_in_db(lifecycle_env):
    """Verify that when an unwinding step encounters a file lock / PermissionError,

    session status updates to failed in the database and the exception propagates (Criterion 2).
    """
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    # 1. Create a session with files and step ledger entries
    file_src = os.path.join(base_dir, "doc.txt")
    file_dst = os.path.join(base_dir, "archive", "doc.txt")
    os.makedirs(os.path.dirname(file_dst), exist_ok=True)
    with open(file_src, "w") as f:
        f.write("data")

    session_id = history.create_snapshot(base_dir)
    shutil.move(file_src, file_dst)
    history.log_step(session_id, file_src, file_dst)

    # Transition session to completed (simulating sort completion)
    history._transition_session_status(session_id, COMPLETED)

    # 2. Trigger unwind with resilient_move raising PermissionError (file locked)
    with patch(
        "app.core.resilient_file_ops.resilient_move",
        side_effect=PermissionError("File locked by external process"),
    ):
        with pytest.raises(PermissionError) as exc_info:
            history._unwind_session_internal(session_id)
        assert "Recovery incomplete" in str(exc_info.value)

    # 3. Verify session status in database updated to 'failed'
    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"


def test_move_replay_failure_transitions_to_failed_in_db(lifecycle_env):
    """Verify that when move replay backwards encounters a failure,

    session status is updated to failed in the database (Criterion 2).
    """
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    session_id = history.create_snapshot(base_dir)

    # Transition to completed
    history._transition_session_status(session_id, COMPLETED)

    # Add a move record in MoveLedger
    src_file = os.path.join(base_dir, "report.pdf")
    dst_file = os.path.join(base_dir, "sorted", "report.pdf")
    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    with open(dst_file, "w") as f:
        f.write("pdf data")

    seq = history.moves.record_intent(
        session_id, src_file, dst_file, src_sha256="fake_sha", size=len("pdf data")
    )
    history.moves.mark_done(session_id, seq, dst_file)
    moves = history.moves.list_moves(session_id)

    # Mock resilient_move to raise PermissionError
    with patch(
        "app.core.resilient_file_ops.resilient_move",
        side_effect=PermissionError("Locked destination"),
    ):
        with pytest.raises((PermissionError, ValueError)):
            history._replay_moves_backwards(session_id, moves)

    # Verify session status is updated to failed in database
    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"


def test_snapshot_rollback_accepts_is_partial_and_records_status(lifecycle_env):
    """Verify snapshot rollback operation accepts a partial rollback indicator and records corresponding status (Criterion 3)."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    # Test 1: Full rollback records 'rolled_back'
    session_id_full = history.create_snapshot(base_dir)
    history._rollback_from_snapshot_internal(session_id_full, is_partial=False)

    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id_full,)
        ).fetchone()
        assert row[0] == "rolled_back"

    # Test 2: Partial rollback records 'partially_rolled_back'
    session_id_partial = history.create_snapshot(base_dir)
    history._rollback_from_snapshot_internal(session_id_partial, is_partial=True)

    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id_partial,)
        ).fetchone()
        assert row[0] == "partially_rolled_back"


def test_unwind_steps_preserves_partial_status_on_snapshot_fallback(lifecycle_env):
    """Verify partial rollback status is preserved when unwinding falls back to snapshot restoration (Requirement 3)."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    # Create files in base_dir before snapshotting
    file1 = os.path.join(base_dir, "f1.txt")
    file2 = os.path.join(base_dir, "f2.txt")
    with open(file1, "w") as f:
        f.write("1")
    with open(file2, "w") as f:
        f.write("2")

    # Create a session with snapshot files
    session_id = history.create_snapshot(base_dir)

    # In step_ledger, create 2 steps
    history.log_step(session_id, file1, os.path.join(base_dir, "out", "f1.txt"))
    history.log_step(session_id, file2, os.path.join(base_dir, "out", "f2.txt"))

    # Unwind with selected_step_ids targeting only 1 of the 2 steps -> partial rollback
    # Mocking steps_to_unwind empty but snapshot_files present to trigger snapshot fallback
    with patch.object(history, "get_step_ledger", return_value=[]):
        # selected_step_ids specified indicates partial unwind
        res = history._unwind_session_internal(session_id, selected_step_ids=["step-1"])
        assert res >= 0

    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row[0] == "partially_rolled_back"


def test_session_catalog_recognizes_failed_and_partially_rolled_back_sessions(
    lifecycle_env,
):
    """Verify session catalog identifies interrupted, failed, and partially rolled back sessions (Criterion 4 & Requirement 5)."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    store = lifecycle_env["store"]

    # 1. Create a session and set to failed
    sid_failed = history.create_snapshot(base_dir)
    history._transition_session_status(sid_failed, "failed")

    # 2. Create a session and set to partially_rolled_back
    sid_partial = history.create_snapshot(base_dir)
    history._transition_session_status(sid_partial, "partially_rolled_back")

    # 3. Create a completed session
    sid_completed = history.create_snapshot(base_dir)
    history._transition_session_status(sid_completed, "completed")

    # 4. Create a rolled_back session
    sid_rolled_back = history.create_snapshot(base_dir)
    history._transition_session_status(sid_rolled_back, "rolled_back")

    abandoned = find_abandoned_sessions(store=store)
    found_sids = {item["session_id"] for item in abandoned}

    # Must recognize both failed and partially_rolled_back
    assert sid_failed in found_sids
    assert sid_partial in found_sids

    # Completed and fully rolled back sessions must not appear in abandoned sessions
    assert sid_completed not in found_sids
    assert sid_rolled_back not in found_sids

    failed_entry = next(s for s in abandoned if s["session_id"] == sid_failed)
    assert failed_entry["status"] == "failed"

    partial_entry = next(s for s in abandoned if s["session_id"] == sid_partial)
    assert partial_entry["status"] == "partially_rolled_back"


def test_mark_session_abandoned_transitions_status(lifecycle_env):
    """Verify mark_session_abandoned transitions status to abandoned via state machine."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    store = lifecycle_env["store"]
    db_path = lifecycle_env["db_path"]

    sid = history.create_snapshot(base_dir)
    history._transition_session_status(sid, "failed")

    # Mark abandoned
    mark_session_abandoned(sid, store=store)

    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (sid,)
        ).fetchone()
        assert row[0] == "abandoned"

    # Once abandoned, find_abandoned_sessions should no longer list it
    abandoned = find_abandoned_sessions(store=store)
    assert not any(s["session_id"] == sid for s in abandoned)


def test_audit_logs_and_telemetry_events(lifecycle_env):
    """Verify audit logs and structured telemetry events are emitted on every status transition (Requirement 4)."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]

    sid = history.create_snapshot(base_dir)

    events_received = []

    def on_telemetry(evt):
        events_received.append(evt)

    history.state_machine.add_transition_listener(on_telemetry)

    # Active -> Completed
    history._transition_session_status(sid, "completed", metadata={"user": "test_user"})

    # Completed -> Partially Rolled Back
    history._transition_session_status(
        sid, "partially_rolled_back", metadata={"step": 1}
    )

    # Verify telemetry events received
    assert len(events_received) == 2
    assert events_received[0]["event_type"] == "SESSION_STATUS_CHANGED"
    assert events_received[0]["session_id"] == sid
    assert events_received[0]["previous_status"] == "active"
    assert events_received[0]["new_status"] == "completed"
    assert events_received[0]["metadata"] == {"user": "test_user"}

    assert events_received[1]["previous_status"] == "completed"
    assert events_received[1]["new_status"] == "partially_rolled_back"

    # Verify durable audit ledger records
    audit_events = history.state_machine.get_audit_events(sid)
    assert len(audit_events) == 2
    assert audit_events[0]["session_id"] == sid
    assert audit_events[0]["previous_status"] == "active"
    assert audit_events[0]["new_status"] == "completed"
    assert audit_events[1]["new_status"] == "partially_rolled_back"


def test_rollback_from_steps_failure_transitions_to_failed_in_db(lifecycle_env):
    """Verify that when _rollback_from_steps encounters a step error,

    session status is updated to failed in the database (Criterion 2).
    """
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    session_id = history.create_snapshot(base_dir)
    history._transition_session_status(session_id, COMPLETED)

    # Prepare steps for _rollback_from_steps
    step_entry = {
        "step_number": 1,
        "source_path": "file1.txt",
        "destination_path": "sorted/file1.txt",
        "file_hash": "hash123",
        "item_type": "file",
        "metadata": "{}",
    }

    # Simulate step failure by patching _move_aside_blocking_parents to raise PermissionError
    with patch(
        "app.core.history._move_aside_blocking_parents",
        side_effect=PermissionError("Permission denied during step reversal"),
    ):
        with pytest.raises((PermissionError, ValueError)) as exc_info:
            history._rollback_from_steps(session_id, [step_entry], base_dir)
        assert "Rollback halted" in str(exc_info.value) or "Recovery incomplete" in str(
            exc_info.value
        )

    # Verify session status updated to failed
    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
