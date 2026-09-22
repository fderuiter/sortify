import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.policy_engine import PolicyEngine
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.scanner import get_files_recursively

_test_dir = None
db_worker = None
db = None


def setup_module(module):
    global _test_dir, db_worker, db
    _test_dir = tempfile.mkdtemp(prefix="sortify_quarantine_test_")
    db_worker = DBWorker()
    db = Database(Path(_test_dir) / "autosorter.db", db_worker)


def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()
    if _test_dir and os.path.exists(_test_dir):
        shutil.rmtree(_test_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield


def test_quarantine_staging_ingestion():
    """Test that incoming documents are immediately placed in _Quarantine_Staging with STAGED status and job ID."""
    service = QuarantineInterceptorService(db=db)

    # Create dummy source file
    sample_dir = os.path.join(_test_dir, "ingest_sample")
    os.makedirs(sample_dir, exist_ok=True)
    sample_file = os.path.join(sample_dir, "patient_record.txt")
    with open(sample_file, "w", encoding="utf-8") as f:
        f.write("Patient Name: John Doe\nDiagnosis: Confidential Medical Report for Subject 101")

    res = service.stage_incoming_file(source_path=sample_file, base_dir=sample_dir)

    assert res["status"] == "STAGED"
    assert res["job_id"].startswith("qjob_")
    assert "_Quarantine_Staging" in res["staged_filepath"]
    assert os.path.exists(res["staged_filepath"])

    # Verify DB record creation
    record = db.get_quarantine_record(res["job_id"])
    assert record is not None
    assert record["status"] == "STAGED"
    assert len(record["audit_log"]) >= 1
    assert record["audit_log"][0]["status"] == "STAGED"


def test_quarantine_isolation_in_scanner():
    """Test that unreleased files in _Quarantine_Staging remain isolated from standard scanner folder views."""
    sample_dir = os.path.join(_test_dir, "scanner_isolation")
    os.makedirs(sample_dir, exist_ok=True)

    # Standard document
    regular_file = os.path.join(sample_dir, "public_doc.txt")
    with open(regular_file, "w", encoding="utf-8") as f:
        f.write("Regular public document content")

    # Staged document in quarantine
    service = QuarantineInterceptorService(db=db)
    service.stage_incoming_file(source_path=regular_file, base_dir=sample_dir)

    # Scan directory recursively
    files = get_files_recursively(sample_dir)

    assert "public_doc.txt" in files
    for f in files:
        assert "_Quarantine_Staging" not in f


def test_extended_policy_engine_actions():
    """Test policy engine support for validate_policy_action and lifecycle action directives."""
    assert PolicyEngine.validate_policy_action("redact") is True
    assert PolicyEngine.validate_policy_action("archive") is True
    assert PolicyEngine.validate_policy_action("quarantine") is True
    assert PolicyEngine.validate_policy_action("retain") is True
    assert PolicyEngine.validate_policy_action("invalid_action") is False

    policies = [
        {
            "type": "keyword",
            "expression": "confidential",
            "action": "redact",
            "target_path": "Sanitized_Folder",
            "priority": 100,
        },
        {
            "type": "keyword",
            "expression": "legacy",
            "action": "archive",
            "target_path": "Archive_Folder",
            "priority": 80,
        },
    ]

    rule1 = PolicyEngine.evaluate_policies("doc.txt", "This is confidential info", "", policies)
    assert rule1 is not None
    assert rule1["action"] == "redact"
    assert rule1["target_path"] == "Sanitized_Folder"

    rule2 = PolicyEngine.evaluate_policies("old_file.txt", "This is legacy data", "", policies)
    assert rule2 is not None
    assert rule2["action"] == "archive"


def test_pii_scrubbing_and_release_pipeline():
    """Test background worker execution of PII scrubbing and document release for action: redact."""
    policies = [
        {
            "type": "keyword",
            "expression": "subject",
            "action": "redact",
            "target_path": "Clean_Out",
            "priority": 100,
        }
    ]

    service = QuarantineInterceptorService(db=db, policies=policies)

    sample_dir = os.path.join(_test_dir, "redact_pipeline")
    os.makedirs(sample_dir, exist_ok=True)
    sensitive_file = os.path.join(sample_dir, "trial_data.txt")
    with open(sensitive_file, "w", encoding="utf-8") as f:
        f.write("Confidential Medical Report for Subject 101 with sensitive findings")

    staged_info = service.stage_incoming_file(source_path=sensitive_file, base_dir=sample_dir)
    job_id = staged_info["job_id"]

    # Process job synchronously in test
    result_record = service.process_quarantine_job(job_id)

    assert result_record["status"] == "RELEASED"
    assert result_record["policy_action"] == "redact"

    # Verify audit log recorded state transitions: STAGED -> IN_INSPECTION -> REDACTED -> RELEASED
    audit_statuses = [entry["status"] for entry in result_record["audit_log"]]
    assert "STAGED" in audit_statuses
    assert "IN_INSPECTION" in audit_statuses
    assert "REDACTED" in audit_statuses
    assert "RELEASED" in audit_statuses

    # Verify sanitized file exists at target destination
    released_path = os.path.join(sample_dir, "Clean_Out", "trial_data.txt")
    assert os.path.exists(released_path)

    with open(released_path, "r", encoding="utf-8") as f:
        sanitized_content = f.read()

    assert "[REDACTED_HISTORICAL_SNIPPET:" in sanitized_content or "[REDACTED_DOCUMENT_TEXT:" in sanitized_content or "Confidential" not in sanitized_content or "Medical Report" not in sanitized_content


def test_policy_action_archive():
    """Test background worker execution for action: archive."""
    policies = [
        {
            "type": "keyword",
            "expression": "obsolete",
            "action": "archive",
            "target_path": "Deep_Storage",
            "priority": 50,
        }
    ]
    service = QuarantineInterceptorService(db=db, policies=policies)

    sample_dir = os.path.join(_test_dir, "archive_test")
    os.makedirs(sample_dir, exist_ok=True)
    obsolete_file = os.path.join(sample_dir, "obsolete_record.txt")
    with open(obsolete_file, "w", encoding="utf-8") as f:
        f.write("This file is obsolete and should be archived")

    staged_info = service.stage_incoming_file(source_path=obsolete_file, base_dir=sample_dir)
    result = service.process_quarantine_job(staged_info["job_id"])

    assert result["status"] == "ARCHIVED"
    assert result["policy_action"] == "archive"
    assert os.path.exists(os.path.join(sample_dir, "Deep_Storage", "obsolete_record.txt"))


def test_worker_timeout_and_dead_letter_queue():
    """Test that forensic scanning jobs exceeding timeout threshold trigger TimeoutError and move to DLQ."""
    service = QuarantineInterceptorService(db=db, worker_timeout=0.001)  # 1 ms timeout to force timeout exception

    sample_dir = os.path.join(_test_dir, "dlq_test")
    os.makedirs(sample_dir, exist_ok=True)
    heavy_file = os.path.join(sample_dir, "heavy_archive.txt")
    with open(heavy_file, "w", encoding="utf-8") as f:
        f.write("Heavy archive data simulation")

    staged_info = service.stage_incoming_file(source_path=heavy_file, base_dir=sample_dir)
    job_id = staged_info["job_id"]

    # Process job with forced 0 second timeout override
    time.sleep(0.01)  # Ensure time elapsed > 0.001s
    result = service.process_quarantine_job(job_id, timeout_override=0.0001)

    assert result["status"] == "DEAD_LETTER_QUEUE" or result["status"] == "MANUAL_REVIEW_REQUIRED"
    assert len(service.dlq_records) >= 1
    assert service.dlq_records[0]["job_id"] == job_id

    # Check DB record status and audit log
    record = db.get_quarantine_record(job_id)
    assert record["status"] == "MANUAL_REVIEW_REQUIRED"
    assert "timeout" in record["error_message"].lower()
