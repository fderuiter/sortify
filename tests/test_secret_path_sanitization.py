"""Unit tests for secret pattern scrubbing across path utilities and renamers."""

import os
from unittest.mock import patch

from app.core.file_renamer import ContextExtractor, FileRenamerEngine
from app.core.forensic_scanner import DiscoveredDocument
from app.core.path_utils import sanitize_folder_key, sanitize_name
from app.plugins.clinical_compliance.clinical_renamer import ClinicalRenamer
from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
    CROMultiStudyPipeline,
)


def test_sanitize_name_strips_secret_patterns():
    """Verify sanitize_name in path_utils.py strips API keys, Bearer tokens, private keys, and high-entropy secrets."""
    # 1. API key in name
    res1 = sanitize_name("Financial_sk_live_51Nxabc123XYZ4567890abcdef_Report.pdf")
    assert "sk_live_" not in res1
    assert "Financial" in res1
    assert "Report.pdf" in res1

    # 2. Bearer token in name
    res2 = sanitize_name(
        "Token_Bearer_eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c_Notes.docx"
    )
    assert "Bearer" not in res2
    assert "Token" in res2
    assert "Notes.docx" in res2

    # 3. Private key header
    res3 = sanitize_name("Key_-----BEGIN PRIVATE KEY-----_Doc.txt")
    assert "BEGIN" not in res3
    assert "PRIVATE" not in res3

    # 4. Pure secret -> empty whitespace -> fallback to Unnamed_safe
    res4 = sanitize_name("sk_live_51Nxabc123XYZ4567890abcdef")
    assert res4 == "Unnamed_safe"


def test_sanitize_folder_key_strips_secret_patterns():
    """Verify sanitize_folder_key in path_utils.py strips secrets from folder keys."""
    # Folder key with secret
    raw_key = "Invoices/sk_live_51Nxabc123XYZ4567890abcdef/2026"
    safe_key, transformed = sanitize_folder_key(raw_key)

    assert transformed is True
    assert "sk_live_" not in safe_key
    assert "Invoices" in safe_key
    assert "2026" in safe_key

    # Pure secret folder key -> Unnamed_safe
    pure_secret_key = "ghp_1234567890abcdef1234567890abcdef123456"
    safe_pure, trans_pure = sanitize_folder_key(pure_secret_key)
    assert trans_pure is True
    assert safe_pure == "Unnamed_safe"


def test_context_extractor_filters_high_entropy_secret_tokens():
    """Verify ContextExtractor filters out high-entropy secret tokens before selecting keywords."""
    text_with_secrets = (
        "Project financial summary quarterly revenue sk_live_51Nxabc123XYZ4567890abcdef "
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
        "a8F93b12C4d5E6f7A8b9C0d1e2f3a4b5 quarterly revenue summary"
    )

    keywords = ContextExtractor.extract_keywords_tfidf(text_with_secrets)
    assert len(keywords) > 0
    for kw in keywords:
        assert "sk_live" not in kw
        assert "bearer" not in kw
        assert "eyjhb" not in kw
        assert "a8f93" not in kw


def test_file_renamer_engine_generate_contextual_name_scrubs_secrets():
    """Verify FileRenamerEngine.generate_contextual_name produces clean filenames when document text contains secrets."""
    engine = FileRenamerEngine()
    doc_text = (
        "Critical technical notes sk_live_51Nxabc123XYZ4567890abcdef "
        "containing API credentials and financial budget summaries for deployment."
    )

    new_fn = engine.generate_contextual_name("scan_001.pdf", doc_text)
    assert "sk_live" not in new_fn
    assert new_fn != "scan_001.pdf"
    assert new_fn.endswith(".pdf")


def test_clinical_renamer_generate_standard_filename_scrubs_secrets():
    """Verify ClinicalRenamer.generate_standard_filename produces clean clinical names without embedded credentials."""
    doc_text = "Protocol ID: PROT-1001 Principal Investigator Dr. Smith Version 1.0"
    art_name = "Form FDA 1572 sk_live_51Nxabc123XYZ4567890abcdef"

    std_name = ClinicalRenamer.generate_standard_filename(
        "doc123.pdf", art_name, doc_text
    )

    assert "sk_live" not in std_name
    assert "PROT_1001" in std_name
    assert "PI_Smith" in std_name or "Smith" in std_name


def test_cro_multi_study_pipeline_sanitizes_folder_names_and_filenames(tmp_path):
    """Verify CROMultiStudyPipeline.run_pipeline sanitizes study folder names, TMF/ISF section names, and target filenames."""
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()

    # Create dummy source file
    src_file = source_dir / "study_doc.pdf"
    src_file.write_bytes(b"%PDF-1.4 dummy content")

    pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=True)

    # Mock scanner output with secret in study ID, artifact names, etc.
    mock_doc = DiscoveredDocument(
        source_path=str(src_file),
        relative_path="study_doc.pdf",
        file_name="study_doc_sk_live_51Nxabc123XYZ4567890abcdef.pdf",
        file_size_bytes=100,
        sha256_hash="dummyhash",
        extracted_text="Protocol ID: PROT-2002 Clinical Trial Results with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    )

    with patch.object(pipeline.scanner, "scan_drive", return_value=[mock_doc]):
        with patch.object(
            pipeline.disambiguator,
            "discover_and_partition_studies",
            return_value={"STUDY_sk_live_51Nxabc123XYZ4567890abcdef": [mock_doc]},
        ):
            res = pipeline.run_pipeline(str(source_dir), str(target_dir))

            assert res is not None
            # Verify created study directory in target_dir does not contain secret pattern
            target_subdirs = [
                d
                for d in os.listdir(target_dir)
                if os.path.isdir(os.path.join(target_dir, d))
            ]
            for sd in target_subdirs:
                assert "sk_live" not in sd

            # Verify files inside target_dir do not contain secret patterns in their paths
            for root, dirs, files in os.walk(target_dir):
                for f in files:
                    assert "sk_live" not in f
                    assert "eyJhbGci" not in f
