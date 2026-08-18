"""Tests for clinical document renamer and metadata extraction."""

from app.core.clinical_renamer import ClinicalRenamer


def test_extract_protocol_id_from_filename():
    fn = "PROTO_2024_001_raw_document.pdf"
    proto = ClinicalRenamer.extract_protocol_id("", fn)
    assert proto == "PROTO_2024_001"


def test_extract_protocol_id_from_text():
    text = "Clinical Study Protocol\nProtocol ID: ABC-9902\nSponsor: Acme Therapeutics"
    proto = ClinicalRenamer.extract_protocol_id(text, "scan001.pdf")
    assert proto == "ABC_9902"


def test_extract_investigator_name():
    text = "STATEMENT OF INVESTIGATOR\nPrincipal Investigator: Dr. John Smith, MD\nSite 101"
    pi = ClinicalRenamer.extract_investigator_name(text)
    assert pi == "PI_John_Smith"


def test_extract_date_or_version():
    fn = "protocol_amendment_v3.2_final.pdf"
    ver = ClinicalRenamer.extract_date_or_version("", fn)
    assert ver == "v3.2"

    text = "Approval Date: 2024-06-15"
    date_val = ClinicalRenamer.extract_date_or_version(text, "approval.pdf")
    assert date_val == "20240615"


def test_generate_standard_filename():
    doc_text = """
    DEPARTMENT OF HEALTH AND HUMAN SERVICES
    FOOD AND DRUG ADMINISTRATION
    STATEMENT OF INVESTIGATOR (Form FDA 1572)
    Study Protocol ID: PROTO-101
    Principal Investigator: Dr. Jane Doe
    Date: 2024-03-20
    """
    standard_name = ClinicalRenamer.generate_standard_filename(
        "Scan_098234.pdf",
        "Form FDA 1572",
        doc_text,
    )
    assert standard_name == "PROTO_101_Form_FDA_1572_PI_Jane_Doe_20240320.pdf"
