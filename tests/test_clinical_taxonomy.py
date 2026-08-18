"""Tests for clinical taxonomy, TMF reference models, and regex signatures."""

from app.core.clinical_taxonomy import (
    CLINICAL_ARTIFACTS,
    ICH_GCP_ESSENTIAL_CHECKLIST,
    ISF_SECTIONS,
    TMF_ZONES,
    get_artifact_by_id,
)


def test_tmf_zones_and_isf_sections_defined():
    """Verify standard TMF zones and ISF sections are populated."""
    assert len(TMF_ZONES) >= 10
    assert "02" in TMF_ZONES
    assert "05" in TMF_ZONES
    assert len(ISF_SECTIONS) >= 15
    assert "01" in ISF_SECTIONS
    assert "03" in ISF_SECTIONS


def test_clinical_artifacts_have_valid_structure():
    """Verify all defined clinical artifacts have valid zone, section, and regex signatures."""
    assert len(CLINICAL_ARTIFACTS) >= 15

    for art in CLINICAL_ARTIFACTS:
        assert art.artifact_id
        assert art.name
        assert art.tmf_zone
        assert art.tmf_section
        assert art.isf_section
        assert art.description
        assert len(art.keywords) > 0


def test_get_artifact_by_id():
    """Test retrieving an artifact by its ID."""
    fda_1572 = get_artifact_by_id("05.02.01")
    assert fda_1572 is not None
    assert "1572" in fda_1572.name
    assert fda_1572.is_essential_gcp is True

    unknown = get_artifact_by_id("99.99.99")
    assert unknown is None


def test_fda_form_1572_regex_matching():
    """Verify Form FDA 1572 signature regexes match realistic form text."""
    sample_1572_text = """
    DEPARTMENT OF HEALTH AND HUMAN SERVICES
    FOOD AND DRUG ADMINISTRATION
    STATEMENT OF INVESTIGATOR (Form FDA 1572)
    OMB No. 0910-0014
    1. NAME AND ADDRESS OF INVESTIGATOR: Dr. John Smith, MD
    6. NAMES OF SUBINVESTIGATORS WHO WILL BE ASSISTING THE INVESTIGATOR
    """
    fda_1572 = get_artifact_by_id("05.02.01")
    matched = False
    for sig in fda_1572.regex_signatures:
        if sig.search(sample_1572_text):
            matched = True
            break
    assert matched is True


def test_irb_approval_regex_matching():
    """Verify IRB approval letter regex matches standard approval text."""
    sample_irb_text = """
    Advarra Institutional Review Board
    IRB Approval Letter - Protocol Number PROTO-2024-001
    Effective Date of Approval: 15-JAN-2024
    Expiration Date of Approval: 14-JAN-2025
    FederalWide Assurance (FWA): FWA00001234
    """
    irb = get_artifact_by_id("04.01.01")
    matched = False
    for sig in irb.regex_signatures:
        if sig.search(sample_irb_text):
            matched = True
            break
    assert matched is True


def test_essential_checklist_integrity():
    """Verify all items in ICH_GCP_ESSENTIAL_CHECKLIST point to valid artifacts."""
    for item in ICH_GCP_ESSENTIAL_CHECKLIST:
        art = get_artifact_by_id(item["artifact_id"])
        assert art is not None, (
            f"Checklist item {item['key']} points to missing artifact {item['artifact_id']}"
        )
        assert art.is_essential_gcp is True
