"""Tests for clinical compliance gap analysis and audit report generation."""

import json
import os
import tempfile

import pytest

from app.plugins.clinical_compliance.clinical_compliance import (
    ClinicalComplianceEngine,
    ClinicalComplianceResult,
    FoundEssentialDocument,
    MissingEssentialDocument,
)
from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
    MasterPipelineResult,
    StudyIngestSummary,
)


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
    assert res.total_files_scanned == 4
    assert res.total_essential_found == 2
    assert res.total_essential_missing == len(engine.checklist) - 2
    assert res.compliance_score_percent < 50.0
    assert res.audit_readiness_status == "NON_COMPLIANT"
    assert len(res.ancillary_documents) == 1
    assert len(res.unclassified_documents) == 1


def test_compliance_evaluation_all_found():
    engine = ClinicalComplianceEngine()

    # Map all essential items
    classified_all = {
        f"file_{item['key']}.pdf": item["artifact_id"] for item in engine.checklist
    }
    res = engine.evaluate_compliance(classified_all, list(classified_all.keys()))
    assert res.total_essential_found == len(engine.checklist)
    assert res.total_essential_missing == 0
    assert res.compliance_score_percent == 100.0
    assert res.audit_readiness_status == "AUDIT_READY"


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


def test_found_essential_document_dict_compat():
    doc = FoundEssentialDocument(
        key="01_protocol",
        title="Clinical Trial Protocol",
        artifact_id="02.01.01",
        importance="essential",
        gcp_ref="ICH GCP 8.2.1",
        files=["protocol_v1.pdf"],
        count=1,
        extra_note="verified",
    )
    # __getitem__
    assert doc["key"] == "01_protocol"
    assert doc["title"] == "Clinical Trial Protocol"
    assert doc["artifact_id"] == "02.01.01"
    assert doc["count"] == 1
    assert doc["extra_note"] == "verified"
    with pytest.raises(KeyError):
        _ = doc["nonexistent_key"]

    # get
    assert doc.get("key") == "01_protocol"
    assert doc.get("extra_note") == "verified"
    assert doc.get("nonexistent_key") is None
    assert doc.get("nonexistent_key", "default_val") == "default_val"

    # __contains__
    assert "key" in doc
    assert "extra_note" in doc
    assert "nonexistent_key" not in doc

    # dict
    d = doc.dict()
    assert isinstance(d, dict)
    assert d["key"] == "01_protocol"
    assert d["extra_note"] == "verified"


def test_missing_essential_document_dict_compat():
    doc = MissingEssentialDocument(
        key="05_fda_1572",
        title="Form FDA 1572",
        artifact_id="05.02.01",
        importance="essential",
        gcp_ref="ICH GCP 8.2.10",
        custom_tag="urgent",
    )
    # __getitem__
    assert doc["key"] == "05_fda_1572"
    assert doc["title"] == "Form FDA 1572"
    assert doc["custom_tag"] == "urgent"
    with pytest.raises(KeyError):
        _ = doc["missing_field"]

    # get
    assert doc.get("artifact_id") == "05.02.01"
    assert doc.get("custom_tag") == "urgent"
    assert doc.get("missing_field") is None
    assert doc.get("missing_field", "fallback") == "fallback"

    # __contains__
    assert "artifact_id" in doc
    assert "custom_tag" in doc
    assert "missing_field" not in doc

    # dict
    d = doc.dict()
    assert isinstance(d, dict)
    assert d["key"] == "05_fda_1572"
    assert d["custom_tag"] == "urgent"


def test_clinical_compliance_result_dict_compat():
    found_doc = FoundEssentialDocument(
        key="01_protocol",
        title="Protocol",
        artifact_id="02.01.01",
        importance="essential",
        gcp_ref="8.2.1",
        files=["p.pdf"],
        count=1,
    )
    missing_doc = MissingEssentialDocument(
        key="05_fda_1572",
        title="FDA 1572",
        artifact_id="05.02.01",
        importance="essential",
        gcp_ref="8.2.10",
    )
    result = ClinicalComplianceResult(
        total_files_scanned=5,
        total_essential_required=10,
        total_essential_found=1,
        total_essential_missing=9,
        compliance_score_percent=10.0,
        audit_readiness_status="NON_COMPLIANT",
        found_essential_documents=[found_doc],
        missing_essential_documents=[missing_doc],
        ancillary_documents=[],
        unclassified_documents=[],
        base_dir="/tmp/study_dir",
        audit_tag="preliminary",
    )
    # __getitem__
    assert result["total_files_scanned"] == 5
    assert result["compliance_score_percent"] == 10.0
    assert result["audit_readiness_status"] == "NON_COMPLIANT"
    assert result["audit_tag"] == "preliminary"
    with pytest.raises(KeyError):
        _ = result["nonexistent_key"]

    # get
    assert result.get("total_essential_found") == 1
    assert result.get("audit_tag") == "preliminary"
    assert result.get("nonexistent_key") is None
    assert result.get("nonexistent_key", 999) == 999

    # __contains__
    assert "compliance_score_percent" in result
    assert "audit_tag" in result
    assert "nonexistent_key" not in result

    # dict
    d = result.dict()
    assert isinstance(d, dict)
    assert d["total_files_scanned"] == 5
    assert d["audit_tag"] == "preliminary"


def test_study_ingest_summary_dict_compat():
    summary = StudyIngestSummary(
        study_id="STUDY-2026-001",
        total_documents=42,
        compliance_score_percent=95.5,
        audit_readiness_status="AUDIT_READY",
        missing_essential_count=1,
        found_essential_count=20,
        target_directory="/data/studies/001",
        audit_report_html_path="/data/studies/001/report.html",
    )
    # __getitem__
    assert summary["study_id"] == "STUDY-2026-001"
    assert summary["total_documents"] == 42
    assert summary["compliance_score_percent"] == 95.5
    with pytest.raises(KeyError):
        _ = summary["unknown_field"]

    # get
    assert summary.get("audit_readiness_status") == "AUDIT_READY"
    assert summary.get("unknown_field") is None
    assert summary.get("unknown_field", "fallback") == "fallback"

    # __contains__
    assert "study_id" in summary
    assert "target_directory" in summary
    assert "unknown_field" not in summary

    # dict
    d = summary.dict()
    assert isinstance(d, dict)
    assert d["study_id"] == "STUDY-2026-001"
    assert d["found_essential_count"] == 20


def test_master_pipeline_result_dict_compat():
    summary = StudyIngestSummary(
        study_id="STUDY-2026-001",
        total_documents=42,
        compliance_score_percent=95.5,
        audit_readiness_status="AUDIT_READY",
        missing_essential_count=1,
        found_essential_count=20,
        target_directory="/data/studies/001",
        audit_report_html_path="/data/studies/001/report.html",
    )
    pipeline_res = MasterPipelineResult(
        source_root="/raw/drive",
        target_root="/organized/trials",
        timestamp_utc="2026-11-14T12:00:00Z",
        total_scanned_files=50,
        total_unique_documents=48,
        total_duplicates_detected=2,
        discovered_studies_count=1,
        studies_summary=[summary],
        chain_of_custody_manifest_path="/organized/trials/manifest.json",
    )
    # __getitem__
    assert pipeline_res["source_root"] == "/raw/drive"
    assert pipeline_res["total_unique_documents"] == 48
    assert pipeline_res["discovered_studies_count"] == 1
    with pytest.raises(KeyError):
        _ = pipeline_res["unknown_key"]

    # get
    assert pipeline_res.get("target_root") == "/organized/trials"
    assert pipeline_res.get("unknown_key") is None
    assert pipeline_res.get("unknown_key", "default_val") == "default_val"

    # __contains__
    assert "chain_of_custody_manifest_path" in pipeline_res
    assert "unknown_key" not in pipeline_res

    # dict
    d = pipeline_res.dict()
    assert isinstance(d, dict)
    assert d["source_root"] == "/raw/drive"
    assert len(d["studies_summary"]) == 1
