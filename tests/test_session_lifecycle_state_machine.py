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
        (ROLLED_BACK, ACTIVE),
        (ROLLED_BACK, COMPLETED),
        (ROLLED_BACK, PARTIALLY_ROLLED_BACK),
        (ROLLED_BACK, FAILED_STATUS),
        (ROLLED_BACK, "abandoned"),
        ("abandoned", ACTIVE),
        ("abandoned", COMPLETED),
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

    session_id = history.start_session(base_dir)
    shutil.move(file_src, file_dst)
    seq = history.moves.record_intent(session_id, file_src, file_dst)
    history.moves.mark_done(session_id, seq, file_dst)

    # Transition session to completed (simulating sort completion)
    history.complete_session(session_id)

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

    session_id = history.start_session(base_dir)

    # Transition to completed
    history.complete_session(session_id)

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


def test_rollback_records_full_and_partial_status(lifecycle_env):
    """Verify full and partial rollback record rolled_back and partially_rolled_back statuses (Criterion 3)."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    # Test 1: Full rollback records 'rolled_back'
    session_id_full = history.start_session(base_dir)
    history.complete_session(session_id_full)
    history.rollback(session_id_full)

    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id_full,)
        ).fetchone()
        assert row[0] == "rolled_back"

    # Test 2: Partial rollback records 'partially_rolled_back'
    f1_src = os.path.join(base_dir, "f1.txt")
    f2_src = os.path.join(base_dir, "f2.txt")
    out_dir = os.path.join(base_dir, "out")
    os.makedirs(out_dir, exist_ok=True)
    f1_dst = os.path.join(out_dir, "f1.txt")
    f2_dst = os.path.join(out_dir, "f2.txt")
    with open(f1_dst, "w") as f:
        f.write("1")
    with open(f2_dst, "w") as f:
        f.write("2")

    session_id_partial = history.start_session(base_dir)
    seq1 = history.moves.record_intent(session_id_partial, f1_src, f1_dst)
    history.moves.mark_done(session_id_partial, seq1, f1_dst)
    seq2 = history.moves.record_intent(session_id_partial, f2_src, f2_dst)
    history.moves.mark_done(session_id_partial, seq2, f2_dst)
    history.complete_session(session_id_partial)

    history.rollback(session_id_partial, selected_step_ids=[seq1])

    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id_partial,)
        ).fetchone()
        assert row[0] == "partially_rolled_back"


def test_unwind_empty_selected_steps_preserves_partial_status(lifecycle_env):
    """Verify partial rollback status is recorded even when selected_step_ids matches no recorded moves."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    session_id = history.start_session(base_dir)
    history.complete_session(session_id)

    res = history._unwind_session_internal(session_id, selected_step_ids=["999"])
    assert res == 0

    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row[0] == "partially_rolled_back"


def test_session_catalog_recognizes_failed_and_partially_rolled_back_sessions(
    lifecycle_env,
):
    """Verify session catalog identifies failed sessions and only flags partially_rolled_back when .branches holds trapped files."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    store = lifecycle_env["store"]

    # 1. Create a session and set to failed
    sid_failed = history.start_session(base_dir)
    history._transition_session_status(sid_failed, "failed")

    # 2. Create a settled partially_rolled_back session (no trapped files)
    sid_partial_settled = history.start_session(base_dir)
    history._transition_session_status(sid_partial_settled, "partially_rolled_back")

    # 3. Create a partially_rolled_back session with trapped .branches files
    from pathlib import Path

    sid_partial_trapped = history.start_session(base_dir)
    history._transition_session_status(sid_partial_trapped, "partially_rolled_back")
    trapped_dir = Path(base_dir) / ".branches" / sid_partial_trapped
    trapped_dir.mkdir(parents=True, exist_ok=True)
    (trapped_dir / "trapped.txt").write_text("trapped bytes", encoding="utf-8")

    # 4. Create a completed session
    sid_completed = history.start_session(base_dir)
    history._transition_session_status(sid_completed, "completed")

    # 5. Create a rolled_back session
    sid_rolled_back = history.start_session(base_dir)
    history._transition_session_status(sid_rolled_back, "rolled_back")

    abandoned = find_abandoned_sessions(store=store)
    found_sids = {item["session_id"] for item in abandoned}

    # Must recognize failed and trapped partially_rolled_back, while excluding settled partially_rolled_back
    assert sid_failed in found_sids
    assert sid_partial_trapped in found_sids
    assert sid_partial_settled not in found_sids

    # Completed and fully rolled back sessions must not appear in abandoned sessions
    assert sid_completed not in found_sids
    assert sid_rolled_back not in found_sids

    failed_entry = next(s for s in abandoned if s["session_id"] == sid_failed)
    assert failed_entry["status"] == "failed"

    partial_entry = next(s for s in abandoned if s["session_id"] == sid_partial_trapped)
    assert partial_entry["status"] == "partially_rolled_back"
    assert partial_entry["has_trapped_files"] is True


def test_mark_session_abandoned_transitions_status(lifecycle_env):
    """Verify mark_session_abandoned transitions status to abandoned via state machine."""
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    store = lifecycle_env["store"]
    db_path = lifecycle_env["db_path"]

    sid = history.start_session(base_dir)
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

    sid = history.start_session(base_dir)

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


def test_blocking_parent_failure_transitions_to_failed_in_db(lifecycle_env):
    """Verify that when _move_aside_blocking_parents raises during backwards move replay,

    session status is updated to failed in the database (Criterion 2).
    """
    history = lifecycle_env["history"]
    base_dir = lifecycle_env["base_dir"]
    db_path = lifecycle_env["db_path"]

    src_file = os.path.join(base_dir, "file1.txt")
    dst_file = os.path.join(base_dir, "sorted", "file1.txt")
    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    with open(dst_file, "w") as f:
        f.write("content")

    session_id = history.start_session(base_dir)
    seq = history.moves.record_intent(session_id, src_file, dst_file)
    history.moves.mark_done(session_id, seq, dst_file)
    history.complete_session(session_id)

    moves = history.moves.list_moves(session_id)

    with patch(
        "app.core.history._move_aside_blocking_parents",
        side_effect=PermissionError("Permission denied during step reversal"),
    ):
        with pytest.raises(PermissionError) as exc_info:
            history._replay_moves_backwards(session_id, moves)
        assert "Recovery incomplete" in str(exc_info.value)

    conn = get_db_connection(db_path)
    with conn:
        row = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "failed"
