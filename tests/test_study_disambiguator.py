"""Tests for StudyDisambiguator: multi-study discovery and investigator co-occurrence network resolution."""

from app.core.forensic_scanner import DiscoveredDocument
from app.core.study_disambiguator import StudyDisambiguator


def test_discover_multi_studies():
    disambiguator = StudyDisambiguator()

    docs = [
        # Study A (PROTO-101) with PI Dr. John Smith
        DiscoveredDocument(
            source_path="/raw/study1/1572.pdf",
            relative_path="study1/1572.pdf",
            file_name="1572.pdf",
            file_size_bytes=1000,
            sha256_hash="hash1",
            extracted_text="STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101 Principal Investigator: Dr. John Smith Site: 104",
        ),
        # Study B (ONCO-2024) with PI Dr. Alice Wong
        DiscoveredDocument(
            source_path="/raw/study2/protocol.pdf",
            relative_path="study2/protocol.pdf",
            file_name="protocol.pdf",
            file_size_bytes=2000,
            sha256_hash="hash2",
            extracted_text="Clinical Study Protocol Protocol ID: ONCO-2024 Principal Investigator: Dr. Alice Wong Site: 201",
        ),
        # Document for Dr. John Smith with NO protocol ID on page (should link to PROTO-101 via PI co-occurrence!)
        DiscoveredDocument(
            source_path="/raw/misc/cv_smith.pdf",
            relative_path="misc/cv_smith.pdf",
            file_name="cv_smith.pdf",
            file_size_bytes=500,
            sha256_hash="hash3",
            extracted_text="Curriculum Vitae of Investigator Dr. John Smith, MD Medical License Number 88921 Education and Training",
        ),
        # Document for Dr. Alice Wong with NO protocol ID (should link to ONCO-2024)
        DiscoveredDocument(
            source_path="/raw/misc/license_wong.pdf",
            relative_path="misc/license_wong.pdf",
            file_name="license_wong.pdf",
            file_size_bytes=400,
            sha256_hash="hash4",
            extracted_text="State Board of Medical Examiners Physician Medical License Dr. Alice Wong, MD License Number: 99402",
        ),
        # Completely unassigned doc
        DiscoveredDocument(
            source_path="/raw/unrelated.txt",
            relative_path="unrelated.txt",
            file_name="unrelated.txt",
            file_size_bytes=100,
            sha256_hash="hash5",
            extracted_text="General notes on unrelated topics without any study or investigator info.",
        ),
    ]

    partitioned = disambiguator.discover_and_partition_studies(docs)

    assert "PROTO_101" in partitioned
    assert "ONCO_2024" in partitioned

    # Check that Dr. Smith's CV was correctly linked to PROTO_101
    proto101_files = [d.file_name for d in partitioned["PROTO_101"]]
    assert "1572.pdf" in proto101_files
    assert "cv_smith.pdf" in proto101_files

    # Check that Dr. Wong's license was correctly linked to ONCO_2024
    onco_files = [d.file_name for d in partitioned["ONCO_2024"]]
    assert "protocol.pdf" in onco_files
    assert "license_wong.pdf" in onco_files

    # Check unassigned
    assert "Unassigned_Study_Documents" in partitioned
    assert any(
        "unrelated.txt" in d.file_name
        for d in partitioned["Unassigned_Study_Documents"]
    )


def test_cross_study_shared_investigator():
    disambiguator = StudyDisambiguator()

    docs = [
        # Study A with Dr. Smith
        DiscoveredDocument(
            source_path="/raw/studyA/1572.pdf",
            relative_path="studyA/1572.pdf",
            file_name="1572.pdf",
            file_size_bytes=1000,
            sha256_hash="h1",
            extracted_text="STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-A Principal Investigator: Dr. John Smith",
        ),
        # Study B ALSO with Dr. Smith
        DiscoveredDocument(
            source_path="/raw/studyB/1572.pdf",
            relative_path="studyB/1572.pdf",
            file_name="1572.pdf",
            file_size_bytes=1000,
            sha256_hash="h2",
            extracted_text="STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-B Principal Investigator: Dr. John Smith",
        ),
        # Dr. Smith's GCP Training Certificate (no protocol ID) -> should be assigned to Cross_Study_Shared!
        DiscoveredDocument(
            source_path="/raw/shared/gcp_smith.pdf",
            relative_path="shared/gcp_smith.pdf",
            file_name="gcp_smith.pdf",
            file_size_bytes=800,
            sha256_hash="h3",
            extracted_text="CITI Program Good Clinical Practice Certificate of Completion Dr. John Smith",
        ),
    ]

    partitioned = disambiguator.discover_and_partition_studies(docs)

    assert "Cross_Study_Shared" in partitioned
    assert any(
        "gcp_smith.pdf" in d.file_name for d in partitioned["Cross_Study_Shared"]
    )


def test_single_study_unassigned_fallback():
    """Verify that on single-study volumes, non-matching files are routed to Unassigned_Study_Documents."""
    disambiguator = StudyDisambiguator()

    docs = [
        # Clinical study doc (matches single study PROTO-101)
        DiscoveredDocument(
            source_path="/raw/study1/1572.pdf",
            relative_path="study1/1572.pdf",
            file_name="1572.pdf",
            file_size_bytes=1000,
            sha256_hash="hash1",
            extracted_text="STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101 Principal Investigator: Dr. John Smith Site: 104",
        ),
        # Non-matching administrative receipt
        DiscoveredDocument(
            source_path="/raw/study1/receipt.pdf",
            relative_path="study1/receipt.pdf",
            file_name="receipt.pdf",
            file_size_bytes=300,
            sha256_hash="hash2",
            extracted_text="Office Supply Store Receipt Expense Reimbursement $42.50 Paid in full.",
        ),
        # Non-matching operational script
        DiscoveredDocument(
            source_path="/raw/study1/deploy_script.sh",
            relative_path="study1/deploy_script.sh",
            file_name="deploy_script.sh",
            file_size_bytes=150,
            sha256_hash="hash3",
            extracted_text="#!/bin/bash\necho 'Deploying server logs...'",
        ),
    ]

    partitioned = disambiguator.discover_and_partition_studies(docs)

    # Only PROTO_101 discovered as a study entity
    assert len(disambiguator.studies) == 1
    assert "PROTO_101" in disambiguator.studies

    # Clinical doc is mapped to PROTO_101
    proto101_files = [d.file_name for d in partitioned["PROTO_101"]]
    assert "1572.pdf" in proto101_files
    assert "receipt.pdf" not in proto101_files
    assert "deploy_script.sh" not in proto101_files

    # Non-matching files are placed into Unassigned_Study_Documents
    assert "Unassigned_Study_Documents" in partitioned
    unassigned_files = [d.file_name for d in partitioned["Unassigned_Study_Documents"]]
    assert "receipt.pdf" in unassigned_files
    assert "deploy_script.sh" in unassigned_files
    assert "1572.pdf" not in unassigned_files

