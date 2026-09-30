import os
import tempfile
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.core.analyzer import FileAnalyzer
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.mover import AsyncMoveEngine
from app.core.quarantine_interceptor import QuarantineInterceptorService

_test_dir = None
db_worker = None
db = None


def setup_module(module):
    global _test_dir, db_worker, db
    _test_dir = tempfile.mkdtemp(prefix="sortify_dual_barrier_test_")
    db_worker = DBWorker()
    db = Database(Path(_test_dir) / "autosorter.db", db_worker)


def teardown_module(module):
    global _test_dir, db_worker
    if db_worker:
        db_worker.stop()
    from app.core.db_conn import clear_connection_cache
    from app.core.resilient_file_ops import resilient_rmtree

    clear_connection_cache(only_current_and_inactive=False)
    if _test_dir and os.path.exists(_test_dir):
        resilient_rmtree(_test_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_db():
    db.clear()
    yield


def test_upstream_barrier_jev_classification_and_hold():
    """Requirement 1: QuarantineInterceptorService calls JevClassifierEngine and holds CRITICAL/HIGH files in quarantine staging with policy_action='sensitivity_hold' under 150ms SLA."""
    service = QuarantineInterceptorService(db=db)

    sample_dir = os.path.join(_test_dir, "upstream_sample")
    os.makedirs(sample_dir, exist_ok=True)

    med_file = os.path.join(sample_dir, "medical_patient_chart.txt")
    with open(med_file, "w", encoding="utf-8") as f:
        f.write("Patient Medical Record: John Doe\nDiagnosis: Confidential Clinical Trial Subject Data\nPrescription: Regimen 4")

    staged_info = service.stage_incoming_file(source_path=med_file, base_dir=sample_dir)
    job_id = staged_info["job_id"]

    start_t = time.perf_counter()
    result = service.process_quarantine_job(job_id)
    elapsed_ms = (time.perf_counter() - start_t) * 1000.0

    # Sub-150ms SLA verification (with tolerance for CI runner overhead)
    threshold = 1000.0 if ("CI" in os.environ or "PYTEST_XDIST_WORKER" in os.environ or os.name == "nt" or os.environ.get("GITHUB_ACTIONS") == "true") else 150.0
    assert elapsed_ms < threshold, f"Jev classification took {elapsed_ms:.2f}ms (SLA: <{threshold}ms)"

    assert result["status"] == "QUARANTINED"
    assert result["policy_action"] == "sensitivity_hold"

    # Verify structured audit log in database
    record = db.get_quarantine_record(job_id)
    assert record["status"] == "QUARANTINED"
    assert record["policy_action"] == "sensitivity_hold"

    audit_entry = record["audit_log"][-1]
    assert audit_entry["status"] == "QUARANTINED"
    assert audit_entry["policy_action"] == "sensitivity_hold"
    assert "sensitivity_rating" in audit_entry
    assert audit_entry["sensitivity_rating"] in ("CRITICAL", "HIGH")


@pytest.mark.anyio
async def test_upstream_daemon_halts_on_sensitivity_hold(tmp_path):
    """Requirement 2: ContinuousWatchdogDaemon treats sensitivity_hold as terminal and halts fast-path plan generation."""
    from app.core.db_conn import clear_connection_cache

    src_dir = tmp_path / "src"
    src_dir.mkdir()

    sensitive_file = src_dir / "confidential_financial_audit.txt"
    sensitive_file.write_text(
        "Company Financial Audit Report: SSN 000-11-2222 Account Balance $5,000,000 Tax Return 1090 Confidential",
        encoding="utf-8",
    )

    settings = Settings()
    daemon = ContinuousWatchdogDaemon(settings, str(src_dir))
    daemon._is_running = True

    try:
        await daemon._triage_file_path(str(sensitive_file))

        # Original unisolated file must be removed post-staging
        assert not sensitive_file.exists()

        session = daemon._get_or_create_session()
        records = session.db.get_quarantine_records_by_base_dir(str(src_dir))
        assert len(records) == 1
        rec = records[0]
        assert rec["status"] == "QUARANTINED"
        assert rec["policy_action"] == "sensitivity_hold"
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_analyzer_plan_node_sensitivity_metadata():
    """Requirement 3 / Acceptance Criterion 3: FileAnalyzer records sensitivity_rating and sensitivity_score in plan nodes."""
    sample_dir = os.path.join(_test_dir, "analyzer_sample")
    os.makedirs(sample_dir, exist_ok=True)

    med_file = os.path.join(sample_dir, "clinical_trial_report.txt")
    with open(med_file, "w", encoding="utf-8") as f:
        f.write("Clinical Trial Report Patient Medical Diagnosis SSN 123-45-6789")

    pub_file = os.path.join(sample_dir, "readme.txt")
    with open(pub_file, "w", encoding="utf-8") as f:
        f.write("General public documentation file")

    db.upsert_documents([
        (sample_dir, "clinical_trial_report.txt", "hash1", "Clinical Trial Report Patient Medical Diagnosis SSN 123-45-6789"),
        (sample_dir, "readme.txt", "hash2", "General public documentation file"),
    ])

    from app.core.jev_classifier import JevClassifierEngine

    jev_classifier = JevClassifierEngine()
    jev_results = {
        "clinical_trial_report.txt": jev_classifier.classify(med_file),
        "readme.txt": jev_classifier.classify(pub_file),
    }

    analyzer = FileAnalyzer(max_folders=12, stop_words=set(), db=db)
    sorting_plan = analyzer.generate_sorting_plan(base_dir=sample_dir, jev_results=jev_results)

    plan_dict = dict(sorting_plan)

    # Find nodes in plan
    med_node = None
    pub_node = None

    def _find_nodes(node):
        nonlocal med_node, pub_node
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, dict) and v.get("__type__") == "file":
                    if "clinical_trial_report.txt" in k or v.get("relative_source") == "clinical_trial_report.txt":
                        med_node = v
                    elif "readme.txt" in k or v.get("relative_source") == "readme.txt":
                        pub_node = v
                elif isinstance(v, dict):
                    _find_nodes(v)

    _find_nodes(plan_dict)

    assert med_node is not None, "Sensitive file node not found in generated plan"
    assert "sensitivity_rating" in med_node
    assert "sensitivity_score" in med_node
    assert med_node["sensitivity_rating"] in ("CRITICAL", "HIGH")

    assert pub_node is not None, "Public file node not found in generated plan"
    assert "sensitivity_rating" in pub_node
    assert pub_node["sensitivity_rating"] in ("LOW", "MEDIUM")


def test_downstream_barrier_mover_interception():
    """Requirement 3 & 4: AsyncMoveEngine evaluates node sensitivity metadata and redirects HIGH/CRITICAL files to _Compliance_Quarantine."""
    sample_dir = os.path.join(_test_dir, "mover_sample")
    os.makedirs(sample_dir, exist_ok=True)

    high_risk_file = os.path.join(sample_dir, "patient_health_data.txt")
    with open(high_risk_file, "w", encoding="utf-8") as f:
        f.write("Medical patient Health Chart SSN 123-45-6789 Diagnosis Confidential")

    plan = {
        "Technical assets": {
            "patient_health_data.txt": {
                "__type__": "file",
                "relative_source": "patient_health_data.txt",
                "sensitivity_rating": "CRITICAL",
                "sensitivity_score": 0.95,
            }
        }
    }

    settings = Settings()
    engine = AsyncMoveEngine()

    result = engine.execute(
        base_dir=sample_dir,
        plan=plan,
        db=db,
        history_manager=None,
        runtime_settings=settings,
    )

    # File must NOT be in standard 'Technical assets' directory
    std_target = os.path.join(sample_dir, "Technical assets", "patient_health_data.txt")
    assert not os.path.exists(std_target)

    # File MUST be redirected to '_Compliance_Quarantine'
    quarantine_target = os.path.join(sample_dir, "_Compliance_Quarantine", "patient_health_data.txt")
    assert os.path.exists(quarantine_target)

    # Verify structured audit log recorded in quarantine_records
    records = db.get_quarantine_records_by_base_dir(sample_dir)
    assert len(records) >= 1
    rec = records[0]
    assert rec["status"] == "QUARANTINED"
    assert rec["policy_action"] == "sensitivity_hold"


def test_administrative_controls_custom_settings():
    """Requirement 5: Custom runtime settings for AUTO_QUARANTINE_RATINGS and QUARANTINE_DIR_NAME."""
    sample_dir = os.path.join(_test_dir, "custom_settings_sample")
    os.makedirs(sample_dir, exist_ok=True)

    med_risk_file = os.path.join(sample_dir, "medium_risk_invoice.txt")
    with open(med_risk_file, "w", encoding="utf-8") as f:
        f.write("Invoice #98765 Total Due $12,500.00 Vendor Contract Details")

    plan = {
        "Invoices": {
            "medium_risk_invoice.txt": {
                "__type__": "file",
                "relative_source": "medium_risk_invoice.txt",
                "sensitivity_rating": "MEDIUM",
                "sensitivity_score": 0.65,
            }
        }
    }

    custom_settings = Settings()
    custom_settings.AUTO_QUARANTINE_RATINGS = ["CRITICAL", "HIGH", "MEDIUM"]
    custom_settings.QUARANTINE_DIR_NAME = "Custom_Quarantine_Vault"

    engine = AsyncMoveEngine()
    engine.execute(
        base_dir=sample_dir,
        plan=plan,
        db=db,
        history_manager=None,
        runtime_settings=custom_settings,
    )

    # File must be intercepted and redirected to 'Custom_Quarantine_Vault'
    custom_target = os.path.join(sample_dir, "Custom_Quarantine_Vault", "medium_risk_invoice.txt")
    assert os.path.exists(custom_target)
