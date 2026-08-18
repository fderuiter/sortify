"""Tests for CROMultiStudyPipeline: end-to-end multi-study ingestion, folder structure, and chain of custody."""

import json
import os
import tempfile
import zipfile

from app.core.cro_multi_study_pipeline import CROMultiStudyPipeline


def test_cro_pipeline_end_to_end():
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        # 1. Setup synthetic multi-study source files
        # Study 1 (PROTO-101)
        study1_dir = os.path.join(source_dir, "historical_dump_2022")
        os.makedirs(study1_dir, exist_ok=True)
        with open(os.path.join(study1_dir, "form1572.txt"), "w") as f:
            f.write(
                "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101 Principal Investigator: Dr. John Smith"
            )

        with open(os.path.join(study1_dir, "protocol_v1.txt"), "w") as f:
            f.write(
                "Clinical Study Protocol Version 1.0 Protocol ID: PROTO-101 Inclusion and exclusion criteria Schedule of assessments"
            )

        # Study 2 (ONCO-500) inside a ZIP archive
        zip_path = os.path.join(source_dir, "study2_archive.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "irb_approval.txt",
                "Advarra Institutional Review Board IRB Approval Letter Protocol ID: ONCO-500 Effective Date of Approval FWA00001234",
            )
            zf.writestr(
                "icf_master.txt",
                "Informed Consent Form Subject Consent Protocol ID: ONCO-500 Voluntary participation signature of subject",
            )

        # Unassociated PI CV for Dr. John Smith (should be linked to PROTO-101)
        with open(os.path.join(source_dir, "cv_smith.txt"), "w") as f:
            f.write(
                "Curriculum Vitae of Investigator Dr. John Smith, MD Medical License Number 88921"
            )

        # 2. Run CRO Pipeline
        pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=True)
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
        )

        # 3. Assert Master Pipeline Results
        assert result.total_scanned_files == 5
        assert result.discovered_studies_count == 2
        assert os.path.exists(result.chain_of_custody_manifest_path)

        # Check Chain of Custody Manifest file
        with open(result.chain_of_custody_manifest_path, "r") as f:
            manifest = json.load(f)
            assert manifest["summary_metrics"]["total_documents_scanned"] == 5
            assert manifest["summary_metrics"]["total_studies_discovered"] == 2
            assert len(manifest["document_manifest"]) == 5

        # Check Target Directory Structure
        assert os.path.exists(os.path.join(target_dir, "PROTO_101"))
        assert os.path.exists(os.path.join(target_dir, "ONCO_500"))

        # Verify PROTO-101 has its own HTML compliance audit report
        assert os.path.exists(
            os.path.join(target_dir, "PROTO_101", "compliance_audit_report.html")
        )
        assert os.path.exists(
            os.path.join(target_dir, "ONCO_500", "compliance_audit_report.html")
        )

        # Verify source directory was NOT modified (Non-Destructive Safe Ingest)
        assert os.path.exists(os.path.join(study1_dir, "form1572.txt"))
        assert os.path.exists(zip_path)


def test_cro_pipeline_cleanup_on_exception(mocker):
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        zip_path = os.path.join(source_dir, "archive.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("doc1.txt", "FDA Form 1572 Protocol ID: PROTO-999")

        pipeline = CROMultiStudyPipeline()
        mocker.patch.object(
            pipeline.disambiguator,
            "discover_and_partition_studies",
            side_effect=RuntimeError("Parsing crash"),
        )

        staging_dir = pipeline.scanner.staging_dir

        import pytest

        with pytest.raises(RuntimeError, match="Parsing crash"):
            pipeline.run_pipeline(source_dir, target_dir)

        assert not os.path.exists(staging_dir)


def test_cro_pipeline_cleanup_on_cancellation():
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        zip_path = os.path.join(source_dir, "archive.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("doc1.txt", "FDA Form 1572 Protocol ID: PROTO-999")

        pipeline = CROMultiStudyPipeline()
        staging_dir = pipeline.scanner.staging_dir

        result = pipeline.run_pipeline(
            source_dir, target_dir, cancel_check=lambda: True
        )

        assert result is not None
        assert not os.path.exists(staging_dir)


def test_cro_pipeline_archive_readonly_files_cleanup():
    import stat

    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        zip_path = os.path.join(source_dir, "readonly_archive.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("readonly_doc.txt", "Clinical Protocol PROTO-888")

        pipeline = CROMultiStudyPipeline()

        # Unpack first to make extracted file read-only on disk before run
        staging_dir = pipeline.scanner.staging_dir
        extracted = pipeline.scanner.unpack_archive(
            zip_path, os.path.join(staging_dir, "test_unpack")
        )

        for path in extracted:
            os.chmod(path, stat.S_IREAD)

        result = pipeline.run_pipeline(source_dir, target_dir)

        assert result.total_scanned_files > 0
        assert not os.path.exists(staging_dir)


def test_cro_pipeline_single_study_unassigned_routing():
    """Verify single-study ingestion routes non-matching files to Unassigned_Study_Documents and excludes them from study compliance metrics."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        # 1. Setup synthetic single-study volume with matching and non-matching files
        study_dir = os.path.join(source_dir, "single_study_volume")
        os.makedirs(study_dir, exist_ok=True)

        # Matching clinical documents for PROTO-101
        with open(os.path.join(study_dir, "form1572.txt"), "w") as f:
            f.write(
                "STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101 Principal Investigator: Dr. Jane Doe"
            )

        with open(os.path.join(study_dir, "protocol.txt"), "w") as f:
            f.write(
                "Clinical Study Protocol Version 1.0 Protocol ID: PROTO-101 Inclusion and exclusion criteria"
            )

        # Non-matching administrative / operational files
        with open(os.path.join(study_dir, "taxi_receipt.txt"), "w") as f:
            f.write("Taxi Receipt $18.50 Expense reimbursement claim for travel")

        with open(os.path.join(study_dir, "operational_script.txt"), "w") as f:
            f.write("#!/bin/bash\necho 'Running server backup...'")

        # 2. Run CRO Pipeline
        pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=False)
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
        )

        # 3. Verify single study discovered
        assert result.discovered_studies_count == 1
        assert result.total_scanned_files == 4

        # Verify PROTO_101 folder exists and contains ONLY clinical documents
        proto101_dir = os.path.join(target_dir, "PROTO_101")
        assert os.path.exists(proto101_dir)

        proto101_files = []
        for root, _, files in os.walk(proto101_dir):
            for file in files:
                proto101_files.append(file)

        assert "form1572.txt" in proto101_files
        assert "protocol.txt" in proto101_files
        assert "taxi_receipt.txt" not in proto101_files
        assert "operational_script.txt" not in proto101_files

        # Verify Unassigned_Study_Documents folder exists and contains non-matching files
        unassigned_dir = os.path.join(target_dir, "Unassigned_Study_Documents")
        assert os.path.exists(unassigned_dir)

        unassigned_files = []
        for root, _, files in os.walk(unassigned_dir):
            for file in files:
                unassigned_files.append(file)

        assert "taxi_receipt.txt" in unassigned_files
        assert "operational_script.txt" in unassigned_files
        assert "form1572.txt" not in unassigned_files

        # Verify study compliance report excludes unassigned files
        comp_json_path = os.path.join(proto101_dir, "compliance_audit_report.json")
        assert os.path.exists(comp_json_path)
        with open(comp_json_path, "r") as f:
            comp_data = json.load(f)
            # Only 2 clinical files assigned to PROTO_101 evaluated
            assert comp_data["total_files_scanned"] == 2

