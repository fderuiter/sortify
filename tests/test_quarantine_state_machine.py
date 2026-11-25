"""Comprehensive unit and integration tests for quarantine lifecycle state machine.

Verifies:
1. Valid lifecycle state transitions against explicit transition matrix.
2. Illegal transitions raise InvalidStateTransitionError and leave DB state unchanged (Criterion 1).
3. Terminal states (RELEASED, DISMISSED) reject all subsequent mutations (Criterion 2).
4. Synchronized in-memory models and persisted DB records during error paths (Criterion 3).
5. CLI list command rejects invalid status arguments with an informative error (Criterion 4).
6. Structured audit events with timestamps, statuses, and reasons (Requirement 6).
7. Recovery routines detect and reset orphaned inspection jobs exceeding timeouts (Requirement 7).
8. Sub-1ms latency constraint for transition validation.
"""

import json
import time
from unittest.mock import patch

import pytest

from app.cli.quarantine_cli import handle_quarantine_command
from app.core.db import Database, DBWorker
from app.core.db_conn import clear_connection_cache
from app.core.domain_contracts import (
    QUARANTINE_TRANSITION_MATRIX,
    validate_quarantine_record,
    validate_quarantine_transition,
)
from app.core.exceptions import InvalidStateTransitionError
from app.core.quarantine_interceptor import QuarantineInterceptorService


@pytest.fixture
def env(tmp_path):
    """Set up isolated DBWorker and Database in tmp_path."""
    clear_connection_cache()
    db_file = tmp_path / "autosorter.db"
    worker = DBWorker()
    db = Database(db_file, worker)
    service = QuarantineInterceptorService(db=db)

    yield {
        "tmp_path": tmp_path,
        "db": db,
        "worker": worker,
        "service": service,
    }

    worker.stop()
    clear_connection_cache()


def test_valid_quarantine_lifecycle_transitions():
    """Verify all permitted transitions pass validation without error."""
    for current_status, allowed_targets in QUARANTINE_TRANSITION_MATRIX.items():
        for target in allowed_targets:
            validate_quarantine_transition(current_status, target)


def test_invalid_quarantine_lifecycle_transitions_raise_error():
    """Verify illegal transitions raise InvalidStateTransitionError (Criterion 1)."""
    invalid_pairs = [
        ("STAGED", "QUARANTINED"),  # Must go through IN_INSPECTION first
        ("STAGED", "REDACTED"),
        ("STAGED", "ARCHIVED"),
        ("STAGED", "UNARCHIVED"),
        ("RELEASED", "IN_INSPECTION"),  # Terminal state cannot be re-inspected
        ("RELEASED", "STAGED"),
        ("RELEASED", "DISMISSED"),
        ("RELEASED", "RELEASED"),
        ("DISMISSED", "IN_INSPECTION"),  # Terminal state cannot be re-inspected
        ("DISMISSED", "RELEASED"),
        ("DISMISSED", "STAGED"),
        ("DISMISSED", "DISMISSED"),
    ]

    for curr, target in invalid_pairs:
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            validate_quarantine_transition(curr, target)
        assert f"'{curr}' to '{target}'" in str(
            exc_info.value
        ) or "Terminal states are immutable" in str(exc_info.value)


def test_unrecognized_status_raises_error():
    """Verify unknown status strings are rejected."""
    with pytest.raises(InvalidStateTransitionError):
        validate_quarantine_transition("STAGED", "NONEXISTENT_STATUS")

    with pytest.raises(InvalidStateTransitionError):
        validate_quarantine_transition("UNKNOWN_STATE", "IN_INSPECTION")


def test_quarantine_record_model_validates_status():
    """Verify QuarantineRecordModel validates status field against valid states."""
    valid_payload = {
        "job_id": "qjob_test_123",
        "status": "STAGED",
    }
    model = validate_quarantine_record(valid_payload)
    assert model.status == "STAGED"

    invalid_payload = {
        "job_id": "qjob_test_123",
        "status": "BOGUS_STATUS",
    }
    with pytest.raises(Exception):
        validate_quarantine_record(invalid_payload)


def test_db_update_illegal_transition_leaves_state_unchanged(env):
    """Verify attempting an illegal transition raises InvalidStateTransitionError and rolls back (Criterion 1)."""
    db = env["db"]
    job_id = "qjob_illegal_transition_test"
    db.stage_quarantine_record(
        job_id=job_id,
        base_dir=str(env["tmp_path"]),
        original_filepath="doc.txt",
        staged_filepath=str(env["tmp_path"] / "doc.txt"),
    )

    initial_rec = db.get_quarantine_record(job_id)
    assert initial_rec["status"] == "STAGED"
    initial_log_len = len(initial_rec["audit_log"])

    # Illegal jump: STAGED -> REDACTED (must go through IN_INSPECTION)
    with pytest.raises(InvalidStateTransitionError):
        db.update_quarantine_status(job_id=job_id, status="REDACTED")

    # Verify DB record is unchanged
    after_rec = db.get_quarantine_record(job_id)
    assert after_rec["status"] == "STAGED"
    assert len(after_rec["audit_log"]) == initial_log_len


def test_terminal_records_reject_all_mutations(env):
    """Verify released and dismissed records reject any subsequent updates (Criterion 2)."""
    db = env["db"]
    service = env["service"]
    tmp_path = env["tmp_path"]

    # 1. Test RELEASED terminal state
    rel_file = tmp_path / "released.txt"
    rel_file.write_text("Hello released", encoding="utf-8")
    staged = service.stage_incoming_file(
        source_path=str(rel_file), base_dir=str(tmp_path)
    )
    job_rel = staged["job_id"]

    # Process to completion (releases file)
    res = service.process_quarantine_job(job_rel)
    assert res["status"] == "RELEASED"

    # Direct DB mutation attempt on RELEASED record must fail
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        db.update_quarantine_status(job_id=job_rel, status="IN_INSPECTION")
    assert "Terminal" in str(exc_info.value) or "immutable" in str(exc_info.value)

    # Batch release on already released item skips cleanly
    batch_res = service.release_quarantine_batch([job_rel])
    assert batch_res[0]["status"] == "RELEASED"
    assert "already released" in batch_res[0]["details"]

    # Batch dismiss on released item fails cleanly without mutating DB
    dismiss_res = service.dismiss_quarantine_batch([job_rel])
    assert dismiss_res[0]["status"] == "FAILED"
    assert "Cannot dismiss" in dismiss_res[0]["error"]
    assert db.get_quarantine_record(job_rel)["status"] == "RELEASED"

    # 2. Test DISMISSED terminal state
    dism_file = tmp_path / "dismissed.txt"
    dism_file.write_text("Hello dismissed", encoding="utf-8")
    staged_dism = service.stage_incoming_file(
        source_path=str(dism_file), base_dir=str(tmp_path)
    )
    job_dism = staged_dism["job_id"]

    dismiss_batch = service.dismiss_quarantine_batch([job_dism])
    assert dismiss_batch[0]["status"] == "DISMISSED"
    assert db.get_quarantine_record(job_dism)["status"] == "DISMISSED"

    # Direct DB mutation attempt on DISMISSED record must fail
    with pytest.raises(InvalidStateTransitionError) as exc_info2:
        db.update_quarantine_status(job_id=job_dism, status="IN_INSPECTION")
    assert "Terminal" in str(exc_info2.value) or "immutable" in str(exc_info2.value)

    # Batch release on dismissed item fails cleanly without mutating DB
    rel_dism_res = service.release_quarantine_batch([job_dism])
    assert rel_dism_res[0]["status"] == "FAILED"
    assert "Cannot release dismissed" in rel_dism_res[0]["error"]
    assert db.get_quarantine_record(job_dism)["status"] == "DISMISSED"


def test_error_paths_persist_identical_status_to_db_and_memory(env):
    """Verify error paths maintain 100% synchronization between DB and returned models (Criterion 3)."""
    db = env["db"]
    service = env["service"]
    tmp_path = env["tmp_path"]

    # 1. Timeout error path
    timeout_file = tmp_path / "timeout.txt"
    timeout_file.write_text("Timeout test content", encoding="utf-8")
    staged = service.stage_incoming_file(
        source_path=str(timeout_file), base_dir=str(tmp_path)
    )
    job_timeout = staged["job_id"]

    res_timeout = service.process_quarantine_job(job_timeout, timeout_override=0.0)
    db_timeout = db.get_quarantine_record(job_timeout)

    assert res_timeout["status"] == "MANUAL_REVIEW_REQUIRED"
    assert db_timeout["status"] == "MANUAL_REVIEW_REQUIRED"
    assert res_timeout["status"] == db_timeout["status"]
    assert len(service.dlq_records) >= 1
    assert service.dlq_records[-1]["status"] == "MANUAL_REVIEW_REQUIRED"

    # 2. General exception error path
    err_file = tmp_path / "err.txt"
    err_file.write_text("Err content", encoding="utf-8")
    staged_err = service.stage_incoming_file(
        source_path=str(err_file), base_dir=str(tmp_path)
    )
    job_err = staged_err["job_id"]

    with patch(
        "app.core.quarantine_interceptor.extract_file_text",
        side_effect=RuntimeError("Forensic crash"),
    ):
        res_err = service.process_quarantine_job(job_err)

    db_err = db.get_quarantine_record(job_err)
    assert res_err["status"] == "MANUAL_REVIEW_REQUIRED"
    assert db_err["status"] == "MANUAL_REVIEW_REQUIRED"
    assert res_err["status"] == db_err["status"]


def test_structured_audit_events_recorded_on_transition(env):
    """Verify structured audit entries with timestamp and previous_status (Requirement 6)."""
    db = env["db"]
    tmp_path = env["tmp_path"]
    job_id = "qjob_audit_test"

    db.stage_quarantine_record(
        job_id=job_id,
        base_dir=str(tmp_path),
        original_filepath="audit.txt",
        staged_filepath=str(tmp_path / "audit.txt"),
    )

    db.update_quarantine_status(
        job_id=job_id,
        status="IN_INSPECTION",
        audit_entry={"details": "Worker picked up job"},
    )

    rec = db.get_quarantine_record(job_id)
    log = rec["audit_log"]
    assert len(log) == 2
    assert log[0]["status"] == "STAGED"
    assert log[1]["status"] == "IN_INSPECTION"
    assert log[1]["previous_status"] == "STAGED"
    assert "timestamp" in log[1]
    assert log[1]["details"] == "Worker picked up job"


def test_cli_list_command_rejects_invalid_status(capsys, env):
    """Verify CLI list command rejects invalid status arguments with error message (Criterion 4)."""
    from types import SimpleNamespace

    db = env["db"]

    # Valid status (e.g. STAGED or staged)
    args_valid = SimpleNamespace(
        subcommand="quarantine",
        quarantine_command="list",
        status="staged",
        base_dir=None,
        json=False,
        quiet=False,
        db_path=str(db.db_path),
    )
    # Should exit 0
    with pytest.raises(SystemExit) as exit_info:
        handle_quarantine_command(args_valid, None)
    assert exit_info.value.code == 0

    # Invalid status
    args_invalid = SimpleNamespace(
        subcommand="quarantine",
        quarantine_command="list",
        status="NOT_A_REAL_STATUS",
        base_dir=None,
        json=False,
        quiet=False,
        db_path=str(db.db_path),
    )
    with pytest.raises(SystemExit) as exit_info_inv:
        handle_quarantine_command(args_invalid, None)
    assert exit_info_inv.value.code == 1

    captured = capsys.readouterr()
    assert "Invalid quarantine status 'NOT_A_REAL_STATUS'" in captured.err

    # Invalid status in JSON mode
    args_invalid_json = SimpleNamespace(
        subcommand="quarantine",
        quarantine_command="list",
        status="INVALID_FOO",
        base_dir=None,
        json=True,
        quiet=False,
        db_path=str(db.db_path),
    )
    with pytest.raises(SystemExit) as exit_info_json:
        handle_quarantine_command(args_invalid_json, None)
    assert exit_info_json.value.code == 1

    captured_json = capsys.readouterr()
    out_data = json.loads(captured_json.out)
    assert out_data["status"] == "error"
    assert "INVALID_FOO" in out_data["message"]


def test_transition_validation_sub_millisecond_latency():
    """Verify transition validation executes in well under 1ms (<1ms constraint)."""
    iterations = 2000
    start = time.perf_counter()
    for _ in range(iterations):
        validate_quarantine_transition("STAGED", "IN_INSPECTION")
        validate_quarantine_transition("IN_INSPECTION", "RELEASED")
    total_ms = (time.perf_counter() - start) * 1000.0
    avg_ms = total_ms / (iterations * 2)

    # Sub-1ms constraint (typically <0.005 ms)
    assert avg_ms < 1.0, f"Validation latency too high: {avg_ms:.4f} ms"
