"""Tests for clinical compliance gap analysis and audit report generation."""

import json
import os
import tempfile

from app.core.clinical_compliance import ClinicalComplianceEngine


def test_compliance_evaluation_full_and_partial():
    engine = ClinicalComplianceEngine()

    # Scenario 1: Only Protocol and FDA 1572 found
    classified_partial = {
        "doc1.pdf": "02.01.01",  # Protocol
        "doc2.pdf": "05.02.01",  # FDA 1572
        "receipt.pdf": "99.01.01",  # Ancillary
        "unknown.txt": "unclassified",
    }
    all_files = list(classified_partial.keys())

    res = engine.evaluate_compliance(classified_partial, all_files)
    assert res["total_files_scanned"] == 4
    assert res["total_essential_found"] == 2
    assert res["total_essential_missing"] == len(engine.checklist) - 2
    assert res["compliance_score_percent"] < 50.0
    assert res["audit_readiness_status"] == "NON_COMPLIANT"
    assert len(res["ancillary_documents"]) == 1
    assert len(res["unclassified_documents"]) == 1


def test_compliance_evaluation_all_found():
    engine = ClinicalComplianceEngine()

    # Map all essential items
    classified_all = {
        f"file_{item['key']}.pdf": item["artifact_id"] for item in engine.checklist
    }
    res = engine.evaluate_compliance(classified_all, list(classified_all.keys()))
    assert res["total_essential_found"] == len(engine.checklist)
    assert res["total_essential_missing"] == 0
    assert res["compliance_score_percent"] == 100.0
    assert res["audit_readiness_status"] == "AUDIT_READY"


def test_export_json_and_html_reports():
    engine = ClinicalComplianceEngine()
    classified = {
        "protocol.pdf": "02.01.01",
        "fda_1572.pdf": "05.02.01",
        "irb_approval.pdf": "04.01.01",
    }
    res = engine.evaluate_compliance(classified, list(classified.keys()))

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = os.path.join(tmpdir, "compliance_report.json")
        html_path = os.path.join(tmpdir, "compliance_report.html")

        engine.export_json_report(res, json_path)
        assert os.path.exists(json_path)
        with open(json_path, "r") as f:
            data = json.load(f)
            assert data["total_essential_found"] == 3

        engine.generate_html_report(res, html_path)
        assert os.path.exists(html_path)
        with open(html_path, "r") as f:
            html_text = f.read()
            assert "Clinical Trial Regulatory Compliance Audit Dossier" in html_text
            assert "Form FDA 1572" in html_text
