"""Tests for CROMultiStudyPipeline: end-to-end multi-study ingestion, folder structure, and chain of custody."""

import json
import os
import tempfile
import zipfile

import pytest

from app.core.exceptions import InvalidStateTransitionError
from app.plugins.clinical_compliance.clinical_compliance import (
    ClinicalComplianceEngine,
)
from app.plugins.clinical_compliance.clinical_strategy import ClinicalTMFStrategy
from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
    _VALID_CRO_TRANSITIONS,
    CROMultiStudyPipeline,
    CROPipelineState,
)

pytestmark = [pytest.mark.slow, pytest.mark.integration]


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
        with open(result.chain_of_custody_manifest_path) as f:
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
        with open(comp_json_path) as f:
            comp_data = json.load(f)
            # Only 2 clinical files assigned to PROTO_101 evaluated
            assert comp_data["total_files_scanned"] == 2


def test_cro_pipeline_naming_collision_handling():
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        # Create two source files that will classify into same study and generate same target filename
        folder1 = os.path.join(source_dir, "folder1")
        folder2 = os.path.join(source_dir, "folder2")
        os.makedirs(folder1, exist_ok=True)
        os.makedirs(folder2, exist_ok=True)

        content1 = "Form FDA 1572 Statement of Investigator Protocol ID: PROTO-COLLISION Site 001"
        content2 = "Form FDA 1572 Statement of Investigator Protocol ID: PROTO-COLLISION Site 002"

        with open(os.path.join(folder1, "form1572.txt"), "w") as f:
            f.write(content1)

        with open(os.path.join(folder2, "form1572.txt"), "w") as f:
            f.write(content2)

        pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=True)
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
        )

        assert result.total_scanned_files == 2
        assert os.path.exists(result.chain_of_custody_manifest_path)

        with open(result.chain_of_custody_manifest_path) as f:
            manifest = json.load(f)

        dest_paths = [rec["destination_path"] for rec in manifest["document_manifest"]]
        assert len(dest_paths) == 2

        # Verify paths are distinct and one has numeric suffix _1
        assert dest_paths[0] != dest_paths[1]
        assert any("_1.txt" in p or "_1" in p for p in dest_paths)

        # Verify both files physically exist on disk with correct content
        for rec in manifest["document_manifest"]:
            abs_dest = os.path.join(target_dir, rec["destination_path"])
            assert os.path.exists(abs_dest)

        contents_on_disk = []
        for p in dest_paths:
            with open(os.path.join(target_dir, p)) as f:
                contents_on_disk.append(f.read())

        assert content1 in contents_on_disk
        assert content2 in contents_on_disk


def test_cro_pipeline_rerun_ingestion_existing_directory():
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        with open(os.path.join(source_dir, "doc1.txt"), "w") as f:
            f.write("Clinical Study Protocol Version 1.0 Protocol ID: PROTO-RERUN")

        pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=True)

        # First run
        result1 = pipeline.run_pipeline(source_root=source_dir, target_root=target_dir)
        with open(result1.chain_of_custody_manifest_path) as f:
            m1 = json.load(f)
        dest1 = m1["document_manifest"][0]["destination_path"]
        assert os.path.exists(os.path.join(target_dir, dest1))

        # Create a second document in source for second run
        with open(os.path.join(source_dir, "doc2.txt"), "w") as f:
            f.write("Clinical Study Protocol Version 2.0 Protocol ID: PROTO-RERUN")

        # Second run into same target_dir
        result2 = pipeline.run_pipeline(source_root=source_dir, target_root=target_dir)
        with open(result2.chain_of_custody_manifest_path) as f:
            m2 = json.load(f)

        # Check second run manifest entries
        m2_dests = [rec["destination_path"] for rec in m2["document_manifest"]]
        assert len(m2_dests) == 2

        # Verify all destination files exist on disk
        for dest in m2_dests:
            assert os.path.exists(os.path.join(target_dir, dest))


def test_cro_pipeline_identical_source_and_target_physical_files():
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        # Create a source file
        src_file = os.path.join(source_dir, "protocol.txt")
        with open(src_file, "w") as f:
            f.write("Clinical Study Protocol Version 1.0 Protocol ID: PROTO-SAMEFILE")

        pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=False)

        # Run pipeline once so target file is generated
        res1 = pipeline.run_pipeline(source_root=source_dir, target_root=target_dir)
        with open(res1.chain_of_custody_manifest_path) as f:
            m1 = json.load(f)
        first_dest_rel = m1["document_manifest"][0]["destination_path"]
        first_dest_abs = os.path.join(target_dir, first_dest_rel)

        # Now run pipeline using target_dir as source_dir where source and target are identical physical files
        res2 = pipeline.run_pipeline(source_root=target_dir, target_root=target_dir)
        assert res2.total_scanned_files >= 1
        assert os.path.exists(first_dest_abs)


def test_cro_pipeline_state_machine_valid_transitions():
    """Verify CROMultiStudyPipeline transitions through allowed paths in _VALID_CRO_TRANSITIONS."""
    pipeline = CROMultiStudyPipeline()
    assert pipeline._VALID_CRO_TRANSITIONS == _VALID_CRO_TRANSITIONS
    assert pipeline.state == CROPipelineState.IDLE

    # Normal full lifecycle
    pipeline.transition_to(CROPipelineState.SCANNING)
    assert pipeline.state == CROPipelineState.SCANNING

    pipeline.transition_to(CROPipelineState.DISAMBIGUATING)
    assert pipeline.state == CROPipelineState.DISAMBIGUATING

    pipeline.transition_to(CROPipelineState.PARTITIONING)
    assert pipeline.state == CROPipelineState.PARTITIONING

    pipeline.transition_to(CROPipelineState.FINALIZING)
    assert pipeline.state == CROPipelineState.FINALIZING

    pipeline.transition_to(CROPipelineState.COMPLETED)
    assert pipeline.state == CROPipelineState.COMPLETED

    # Reset to IDLE
    pipeline.transition_to(CROPipelineState.IDLE)
    assert pipeline.state == CROPipelineState.IDLE

    # Cancellation from scanning
    pipeline.transition_to(CROPipelineState.SCANNING)
    pipeline.transition_to(CROPipelineState.CANCELLED)
    assert pipeline.state == CROPipelineState.CANCELLED

    # Reset to IDLE
    pipeline.transition_to(CROPipelineState.IDLE)
    assert pipeline.state == CROPipelineState.IDLE


def test_cro_pipeline_state_machine_invalid_transitions():
    """Verify attempting an out-of-order state transition raises InvalidStateTransitionError."""
    pipeline = CROMultiStudyPipeline()
    assert pipeline.state == CROPipelineState.IDLE

    # Out of order from IDLE
    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.PARTITIONING)

    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.COMPLETED)

    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.FINALIZING)

    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.DISAMBIGUATING)

    # Transition to SCANNING
    pipeline.transition_to(CROPipelineState.SCANNING)

    # Out of order from SCANNING
    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.PARTITIONING)

    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.COMPLETED)

    with pytest.raises(InvalidStateTransitionError):
        pipeline.transition_to(CROPipelineState.IDLE)


def test_cro_pipeline_cancellation_during_scan_stage():
    """Verify cancellation during scan transitions to CANCELLED and returns MasterPipelineResult with status CANCELLED."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        with open(os.path.join(source_dir, "doc1.txt"), "w") as f:
            f.write("Clinical Trial Protocol PROTO-101")

        pipeline = CROMultiStudyPipeline()

        # cancel_check returns True immediately
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
            cancel_check=lambda: True,
        )

        assert pipeline.state == CROPipelineState.CANCELLED
        assert result.status == CROPipelineState.CANCELLED
        assert result.chain_of_custody_manifest_path == ""
        assert not os.path.exists(
            os.path.join(target_dir, "chain_of_custody_manifest.json")
        )


def test_cro_pipeline_cancellation_during_partitioning_stage():
    """Verify cancellation during partition stops immediately and never creates chain_of_custody_manifest.json."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        for i in range(5):
            with open(os.path.join(source_dir, f"doc_{i}.txt"), "w") as f:
                f.write(
                    f"Form FDA 1572 Statement of Investigator Protocol ID: PROTO-STAGE3 File {i}"
                )

        pipeline = CROMultiStudyPipeline()

        # Cancellation flag triggered when partitioning starts
        cancelled_flag = False

        def conditional_cancel():
            nonlocal cancelled_flag
            if pipeline.state == CROPipelineState.PARTITIONING:
                cancelled_flag = True
                return True
            return False

        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
            cancel_check=conditional_cancel,
        )

        assert cancelled_flag is True
        assert pipeline.state == CROPipelineState.CANCELLED
        assert result.status == CROPipelineState.CANCELLED
        assert not os.path.exists(
            os.path.join(target_dir, "chain_of_custody_manifest.json")
        )


def test_cro_pipeline_no_completion_progress_on_cancellation():
    """Verify pipeline does not emit progress value 1.0 or completion message when cancelled."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        with open(os.path.join(source_dir, "doc1.txt"), "w") as f:
            f.write("Clinical Trial Protocol PROTO-101")

        pipeline = CROMultiStudyPipeline()
        emitted_progress = []

        def track_progress(prog, msg=""):
            emitted_progress.append((prog, msg))

        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
            progress_callback=track_progress,
            cancel_check=lambda: True,
        )

        assert result.status == CROPipelineState.CANCELLED
        for prog, msg in emitted_progress:
            assert prog < 1.0
            assert "complete" not in str(msg).lower()


def test_cro_pipeline_does_not_reclassify_documents(mocker):
    """Verify that run_pipeline reuses cached artifacts and does not re-invoke _classify_document."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        with open(os.path.join(source_dir, "form1572.txt"), "w") as f:
            f.write(
                "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101"
            )
        with open(os.path.join(source_dir, "protocol.txt"), "w") as f:
            f.write(
                "Clinical Study Protocol Version 1.0 Protocol ID: PROTO-101 Schedule of assessments"
            )
        with open(os.path.join(source_dir, "unrelated.txt"), "w") as f:
            f.write(
                "Arbitrary notes without regulatory significance Protocol ID: PROTO-101"
            )

        classify_spy = mocker.spy(ClinicalTMFStrategy, "_classify_document")

        pipeline = CROMultiStudyPipeline(mode="tmf")
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
        )

        assert result.status == CROPipelineState.COMPLETED
        assert result.total_scanned_files == 3

        # Exactly 3 calls during generate_plan (one per document), and ZERO calls during the copy loop
        assert classify_spy.call_count == 3


def test_cro_pipeline_fallback_to_classify_document_on_cache_miss(mocker):
    """Verify that run_pipeline safely falls back to _classify_document if a document key is missing from cache."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        with open(os.path.join(source_dir, "form1572.txt"), "w") as f:
            f.write(
                "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101"
            )
        with open(os.path.join(source_dir, "protocol.txt"), "w") as f:
            f.write(
                "Clinical Study Protocol Version 1.0 Protocol ID: PROTO-101 Schedule of assessments"
            )

        orig_generate_plan = ClinicalTMFStrategy.generate_plan

        def patched_generate_plan(self, filenames, documents, **kwargs):
            res = orig_generate_plan(self, filenames, documents, **kwargs)
            # Simulate a missing cache key for protocol.txt
            if "protocol.txt" in self.last_classified_artifacts:
                del self.last_classified_artifacts["protocol.txt"]
            return res

        mocker.patch.object(ClinicalTMFStrategy, "generate_plan", patched_generate_plan)
        classify_spy = mocker.spy(ClinicalTMFStrategy, "_classify_document")

        pipeline = CROMultiStudyPipeline(mode="tmf")
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
        )

        assert result.status == CROPipelineState.COMPLETED
        assert result.total_scanned_files == 2
        # 2 calls during generate_plan + 1 fallback call for protocol.txt during copy loop = 3 calls
        assert classify_spy.call_count == 3


def test_cro_pipeline_reuses_precomputed_compliance_result(mocker):
    """Verify that run_pipeline reuses strategy.last_compliance_result without re-evaluating compliance."""
    with (
        tempfile.TemporaryDirectory() as source_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        with open(os.path.join(source_dir, "form1572.txt"), "w") as f:
            f.write(
                "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101"
            )

        eval_spy = mocker.spy(ClinicalComplianceEngine, "evaluate_compliance")

        pipeline = CROMultiStudyPipeline(mode="tmf")
        result = pipeline.run_pipeline(
            source_root=source_dir,
            target_root=target_dir,
        )

        assert result.status == CROPipelineState.COMPLETED
        # Exactly 1 evaluate_compliance call inside strategy.generate_plan, 0 calls in pipeline's loop
        assert eval_spy.call_count == 1


def test_clinical_strategy_partial_cache_on_cancel():
    """Verify that ClinicalTMFStrategy preserves processed documents in last_classified_artifacts when cancelled."""
    strategy = ClinicalTMFStrategy(mode="tmf", generate_audit_report=False)
    filenames = ["doc1.txt", "doc2.txt", "doc3.txt"]
    documents = [
        "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101",
        "Clinical Study Protocol Version 1.0 Protocol ID: PROTO-101",
        "Advarra Institutional Review Board IRB Approval Letter Protocol ID: PROTO-101",
    ]

    call_count = 0

    def cancel_after_first():
        nonlocal call_count
        call_count += 1
        return call_count > 1

    strategy.generate_plan(filenames, documents, cancel_check=cancel_after_first)

    # First document was processed before cancellation
    assert "doc1.txt" in strategy.last_classified_artifacts
    assert strategy.last_classified_artifacts["doc1.txt"] is not None
    assert "doc1.txt" in strategy.last_classified_map
    # Subsequent documents were not processed due to cancellation
    assert "doc2.txt" not in strategy.last_classified_artifacts
    assert "doc3.txt" not in strategy.last_classified_artifacts
