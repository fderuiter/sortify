"""Tests for Jev Fast-Path Triage Engine, SharedModelRegistry integration, and Watchdog Daemon triage pipeline."""

import threading
import time

import pytest

from app.config import AppSettings
from app.core.analyzer import FileAnalyzer, SortingPlan
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.jev_classifier import JevClassificationResult, JevClassifierEngine
from app.core.shared_registry import SharedModelRegistry


def test_shared_model_registry_jev_classifier():
    """Verify SharedModelRegistry initializes and caches a thread-safe JevClassifierEngine instance."""
    registry = SharedModelRegistry.get_instance()
    engine1 = registry.get_jev_classifier()
    engine2 = registry.get_jev_classifier()

    assert isinstance(engine1, JevClassifierEngine)
    assert engine1 is engine2

    # Verify thread safety with concurrent requests
    instances = []

    def fetch_instance():
        instances.append(registry.get_jev_classifier())

    threads = [threading.Thread(target=fetch_instance) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(instances) == 10
    assert all(inst is engine1 for inst in instances)


def test_jev_classifier_engine_classification_schema_and_sla(tmp_path):
    """Verify JevClassifierEngine outputs typed schemas within sub-150 ms SLA."""
    engine = JevClassifierEngine()

    # Create dummy files for testing
    financial_file = tmp_path / "invoice_2026_q1.csv"
    financial_file.write_text("Invoice ID, Amount, Tax, Total\n1001, $500, $50, $550\n")

    legal_file = tmp_path / "nda_agreement_signed.pdf"
    legal_file.write_text("Non-disclosure agreement and terms of confidentiality.")

    medical_file = tmp_path / "patient_lab_report.txt"
    medical_file.write_text("Patient clinical report and lab diagnosis.")

    unclassified_file = tmp_path / "unknown_binary_data.dat"
    unclassified_file.write_bytes(b"\x00\x01\x02\x03\x04")

    # SLA and Schema Check: Financial File
    t0 = time.perf_counter()
    res_fin = engine.classify(str(financial_file))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 150.0  # SLA < 150 ms
    assert isinstance(res_fin, JevClassificationResult)
    assert res_fin.is_classified is True
    assert res_fin.confidence >= 0.5
    assert "Financial" in res_fin.category
    assert res_fin.sensitivity_rating in ("HIGH", "CRITICAL")
    assert res_fin.archival_priority == 1
    assert res_fin["category"] == res_fin.category  # dict-like bracket access check
    assert res_fin.get("sensitivity_rating") == res_fin.sensitivity_rating

    # SLA and Schema Check: Legal File
    t0 = time.perf_counter()
    res_leg = engine.classify(str(legal_file))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 150.0
    assert res_leg.is_classified is True
    assert "Legal" in res_leg.category
    assert res_leg.sensitivity_rating in ("HIGH", "CRITICAL")

    # SLA and Schema Check: Medical File
    t0 = time.perf_counter()
    res_med = engine.classify(str(medical_file))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 150.0
    assert res_med.is_classified is True
    assert "Medical" in res_med.category
    assert res_med.sensitivity_rating in ("CRITICAL", "HIGH")

    # Fallback / Unclassified Check
    res_unclassified = engine.classify(str(unclassified_file))
    assert res_unclassified.is_classified is False
    assert res_unclassified.category == "Unclassified"
    assert res_unclassified.confidence < 0.5


def test_jev_classifier_engine_exception_safety():
    """Verify JevClassifierEngine handles unexpected exceptions safely without failing."""
    engine = JevClassifierEngine()

    class FaultyPath:
        def __str__(self):
            raise RuntimeError("Forced path string conversion failure")

    res = engine.classify(FaultyPath())  # type: ignore
    assert isinstance(res, JevClassificationResult)
    assert res.is_classified is False
    assert res.confidence == 0.0


def test_file_analyzer_generate_sorting_plan_with_jev_results(tmp_path):
    """Verify FileAnalyzer.generate_sorting_plan incorporates Jev typed results."""
    from unittest.mock import Mock

    mock_db = Mock()
    mock_db.get_model_metadata.return_value = None
    mock_db.get_all_documents.return_value = [
        ("invoice_2026.csv", "Invoice ID, Amount", "hash123", None)
    ]

    analyzer = FileAnalyzer(max_folders=5, stop_words=set(), db=mock_db)

    jev_res = JevClassificationResult(
        category="Financial Reports",
        sensitivity_rating="HIGH",
        sensitivity_score=0.85,
        archival_priority=1,
        archival_priority_score=0.90,
        confidence=0.95,
        is_classified=True,
    )

    jev_results = {"invoice_2026.csv": jev_res}

    plan = analyzer.generate_sorting_plan(
        str(tmp_path), fast_path_only=True, jev_results=jev_results
    )

    assert isinstance(plan, SortingPlan)
    assert "Financial Reports" in plan
    node = plan["Financial Reports"]["invoice_2026.csv"]
    assert node["routed_by"] == "jev_classifier"
    assert node["category"] == "Financial Reports"
    assert node["sensitivity_rating"] == "HIGH"
    assert node["archival_priority"] == 1


@pytest.mark.anyio
async def test_daemon_triage_file_path_jev_fast_path(tmp_path):
    """Verify ContinuousWatchdogDaemon._triage_file_path routes via Jev fast-path triage."""
    base_dir = tmp_path / "monitored"
    base_dir.mkdir()

    invoice_file = base_dir / "invoice_2026.csv"
    invoice_file.write_text("Invoice ID, Total\n1, 100")

    settings = AppSettings()
    daemon = ContinuousWatchdogDaemon(settings, str(base_dir))
    daemon._is_running = True

    await daemon._triage_file_path(str(invoice_file))

    # Verify file was categorized and moved into Financial Reports target directory
    expected_moved_file = base_dir / "Financial Reports" / "invoice_2026.csv"
    assert expected_moved_file.exists()
    assert not invoice_file.exists()


@pytest.mark.anyio
async def test_daemon_triage_unclassified_fallback(tmp_path):
    """Verify ContinuousWatchdogDaemon._triage_file_path falls back cleanly when unclassified."""
    base_dir = tmp_path / "monitored"
    base_dir.mkdir()

    unclassified_file = base_dir / "random_file.dat"
    unclassified_file.write_bytes(b"1234567890")

    settings = AppSettings()
    daemon = ContinuousWatchdogDaemon(settings, str(base_dir))
    daemon._is_running = True

    # Execution should not throw error and fall through to slow path gracefully
    await daemon._triage_file_path(str(unclassified_file))
