"""Tests for ClinicalTMFStrategy in TMF and ISF modes with clustering registry integration."""

import os
import tempfile

from app.core.analyzer_strategies import clustering_registry
from app.core.clinical_strategy import ClinicalTMFStrategy


def test_clinical_strategies_registered():
    """Verify clinical_tmf and clinical_isf are registered and retrievable."""
    strat_tmf = clustering_registry.get_strategy("clinical_tmf")
    assert strat_tmf is not None
    assert isinstance(strat_tmf, ClinicalTMFStrategy)
    assert strat_tmf.mode == "tmf"

    strat_isf = clustering_registry.get_strategy("clinical_isf")
    assert strat_isf is not None
    assert isinstance(strat_isf, ClinicalTMFStrategy)
    assert strat_isf.mode == "isf"


def test_tmf_mode_plan_generation():
    """Test generating a sorting plan with Sponsor TMF Zone/Section hierarchy."""
    strategy = ClinicalTMFStrategy(
        mode="tmf", smart_renaming=False, generate_audit_report=False
    )

    filenames = [
        "1572_form_signed.pdf",
        "Clinical_Study_Protocol_v2.docx",
        "IRB_approval_letter.pdf",
        "hotel_expense_receipt.pdf",
        "random_unrelated_file.txt",
    ]
    documents = [
        "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION STATEMENT OF INVESTIGATOR Form FDA 1572 OMB No. 0910-0014",
        "Clinical Study Protocol Version 2.0 Protocol Number PROTO-101 Schedule of assessments inclusion criteria exclusion criteria",
        "Institutional Review Board IRB Approval Letter Effective Date of Approval FWA00001234",
        "Hotel lodging receipt and flight ticket invoice reimbursement",
        "Lorem ipsum dolor sit amet consectetur adipiscing elit.",
    ]

    plan, error = strategy.generate_plan(filenames, documents)
    assert error == 0.0

    # Verify TMF hierarchy: Zone > Section
    assert "05 Site Management" in plan
    assert "05.02 Form FDA 1572 and Agreements" in plan["05 Site Management"]
    assert (
        "1572_form_signed.pdf"
        in plan["05 Site Management"]["05.02 Form FDA 1572 and Agreements"]
    )

    assert "02 Central Trial Documents" in plan
    assert "02.01 Protocol and Amendments" in plan["02 Central Trial Documents"]
    assert (
        "Clinical_Study_Protocol_v2.docx"
        in plan["02 Central Trial Documents"]["02.01 Protocol and Amendments"]
    )

    assert "04 IRB and IEC" in plan
    assert "04.01 Ethics Committee Approvals" in plan["04 IRB and IEC"]
    assert (
        "IRB_approval_letter.pdf"
        in plan["04 IRB and IEC"]["04.01 Ethics Committee Approvals"]
    )

    # Verify Ancillary and Unclassified review folders
    assert "Ancillary_Non_TMF" in plan
    assert "hotel_expense_receipt.pdf" in plan["Ancillary_Non_TMF"]

    assert "Unclassified_Review" in plan
    assert "random_unrelated_file.txt" in plan["Unclassified_Review"]

    # Verify compliance result was computed
    assert strategy.last_compliance_result is not None
    assert strategy.last_compliance_result["total_files_scanned"] == 5


def test_isf_mode_plan_generation_with_smart_renaming():
    """Test generating a sorting plan with Site ISF Regulatory Binder structure and smart renaming."""
    strategy = ClinicalTMFStrategy(
        mode="isf", smart_renaming=True, generate_audit_report=False
    )

    filenames = [
        "raw_1572.pdf",
        "delegation_sheet.pdf",
    ]
    documents = [
        "STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-999 Principal Investigator: Dr. Alice Walker Date: 2024-05-10",
        "Delegation of Authority Responsibilities Log Site Signature and Delegation Log Principal Investigator: Dr. Alice Walker",
    ]

    plan, error = strategy.generate_plan(filenames, documents)
    assert error == 0.0

    # In ISF mode, folder is the ISF Section
    assert "03_FDA_Form_1572_and_Agreements" in plan
    assert "11_Delegation_of_Authority_Log" in plan

    # Verify smart renaming output
    node_1572 = plan["03_FDA_Form_1572_and_Agreements"]["raw_1572.pdf"]
    assert isinstance(node_1572, dict)
    assert node_1572["__type__"] == "file"
    assert node_1572["relative_source"] == "raw_1572.pdf"
    assert (
        "PROTO_999_Form_FDA_1572_PI_Alice_Walker_20240510.pdf"
        == node_1572["target_filename"]
    )


def test_audit_report_file_generation():
    """Test that audit report files are written when base_dir is supplied."""
    with tempfile.TemporaryDirectory() as tmpdir:
        strategy = ClinicalTMFStrategy(
            mode="tmf", smart_renaming=False, generate_audit_report=True
        )
        strategy.base_dir = tmpdir

        filenames = ["1572.pdf"]
        documents = ["STATEMENT OF INVESTIGATOR Form FDA 1572 OMB No. 0910-0014"]

        plan, _ = strategy.generate_plan(filenames, documents)

        assert os.path.exists(os.path.join(tmpdir, "compliance_audit_report.json"))
        assert os.path.exists(os.path.join(tmpdir, "compliance_audit_report.html"))
