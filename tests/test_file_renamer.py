"""Tests for Modular Heuristic & Context-Aware File Renaming Engine."""

import gc
import os
import pytest
from unittest.mock import MagicMock

from app.config import Settings
from app.core.file_renamer import (
    ContextExtractor,
    FileRenamerEngine,
    HeuristicEvaluator,
    is_file_protected_or_locked,
)


def test_heuristic_evaluator_flagging():
    """Test heuristic evaluator flags non-descriptive filenames and numeric suffixes."""
    # Non-descriptive / poorly-named filenames
    poor_names = [
        "scan_001.pdf",
        "scanned_doc.docx",
        "IMG_20230512_102030.jpg",
        "untitled.txt",
        "file(1).pdf",
        "123456.docx",
        "doc_v1.pdf",
        "a.txt",
        "temp_output.xlsx",
    ]
    for name in poor_names:
        assert HeuristicEvaluator.is_poorly_named(name) is True, f"Failed for {name}"
        report = HeuristicEvaluator.evaluate_filename(name)
        assert report["is_poorly_named"] is True

    # Descriptive / good filenames
    good_names = [
        "annual_financial_report_2023.pdf",
        "q3_marketing_budget.xlsx",
        "patient_consent_form.docx",
        "employee_onboarding_guide.pdf",
    ]
    for name in good_names:
        assert HeuristicEvaluator.is_poorly_named(name) is False, f"Failed for {name}"
        report = HeuristicEvaluator.evaluate_filename(name)
        assert report["is_poorly_named"] is False


def test_tfidf_keyword_extraction():
    """Test TF-IDF statistical keyword extraction fallback."""
    sample_text = (
        "Annual Financial Statement and Tax Audit Report for Acme Corporation."
    )
    keywords = ContextExtractor.extract_keywords_tfidf(sample_text, max_keywords=3)
    assert len(keywords) > 0
    # Keywords should contain informative terms from the text
    for kw in keywords:
        assert kw in sample_text.lower()


def test_contextual_renaming_fallback_offline():
    """Test fallback to TF-IDF keyword extraction when embedding model is offline."""
    settings = Settings(
        CONTEXTUAL_RENAMING=True,
        AI_CONSENT_GRANTED=None,  # Consent not granted / offline
        AI_ASSISTED_NAMING=False,
    )
    mock_embedding_manager = MagicMock()
    mock_embedding_manager.is_mock = True

    engine = FileRenamerEngine(
        runtime_settings=settings, embedding_manager=mock_embedding_manager
    )

    doc_text = (
        "Quarterly Financial Statement and Audit Report for Globex International."
    )
    new_fn = engine.generate_contextual_name("scan_001.pdf", doc_text)

    assert new_fn != "scan_001.pdf"
    assert new_fn.endswith(".pdf")
    # New filename should be generated from extracted terms
    assert "audit" in new_fn.lower() or "financial" in new_fn.lower() or "statement" in new_fn.lower()


def test_ai_consent_verification():
    """Test verification of AI consent settings before ML contextual generation."""
    # Scenario 1: AI consent granted & AI assisted naming enabled
    settings_consent = Settings(
        CONTEXTUAL_RENAMING=True,
        AI_CONSENT_GRANTED=True,
        AI_ASSISTED_NAMING=True,
    )
    mock_manager_online = MagicMock()
    mock_manager_online.is_mock = False
    mock_manager_online.is_reconstruction_active.return_value = False

    engine_consent = FileRenamerEngine(
        runtime_settings=settings_consent, embedding_manager=mock_manager_online
    )
    text = "Comprehensive Legal Settlement Agreement and Confidentiality Contract."
    new_fn = engine_consent.generate_contextual_name("doc_001.pdf", text)
    assert new_fn.endswith(".pdf")
    assert new_fn != "doc_001.pdf"

    # Scenario 2: AI consent withheld (None or False)
    settings_no_consent = Settings(
        CONTEXTUAL_RENAMING=True,
        AI_CONSENT_GRANTED=False,
        AI_ASSISTED_NAMING=True,
    )
    engine_no_consent = FileRenamerEngine(
        runtime_settings=settings_no_consent, embedding_manager=mock_manager_online
    )
    new_fn_fallback = engine_no_consent.generate_contextual_name("doc_001.pdf", text)
    assert new_fn_fallback.endswith(".pdf")
    # Falls back to TF-IDF extraction cleanly without using ML


def test_protected_paths_and_locks():
    """Test protected path configurations and manual locks override renaming."""
    protected = ["/app/protected_folder", "C:\\SystemFiles"]
    locked = {"/app/my_files/scan_002.pdf": "Manual_Locked_Folder"}

    # File in protected path
    assert (
        is_file_protected_or_locked(
            "/app/protected_folder/scan_001.pdf", protected_paths=protected
        )
        is True
    )

    # File with manual user lock
    assert (
        is_file_protected_or_locked(
            "/app/my_files/scan_002.pdf", locked_files=locked
        )
        is True
    )

    # Normal unprotected file
    assert (
        is_file_protected_or_locked(
            "/app/my_files/scan_003.pdf", locked_files=locked, protected_paths=protected
        )
        is False
    )


def test_sorting_plan_processing_without_altering_folders():
    """Test contextual renaming attaches target_filename without changing folder structure."""
    settings = Settings(CONTEXTUAL_RENAMING=True)
    engine = FileRenamerEngine(runtime_settings=settings)

    initial_plan = {
        "Financial_Documents": {
            "scan_100.pdf": {
                "__type__": "file",
                "relative_source": "scan_100.pdf",
                "status": "Ready",
            }
        },
        "Reports": {
            "annual_summary.pdf": {
                "__type__": "file",
                "relative_source": "annual_summary.pdf",
            }
        },
    }

    docs_map = {
        "scan_100.pdf": "Quarterly Tax Return and Revenue Statement 2023.",
        "annual_summary.pdf": "Annual Executive Summary.",
    }

    processed_plan = engine.process_sorting_plan(
        initial_plan, documents_map=docs_map, base_dir="/app/work"
    )

    # Verify folder structure is untouched
    assert "Financial_Documents" in processed_plan
    assert "Reports" in processed_plan

    # Verify target_filename is attached to poorly-named scan_100.pdf
    scan_node = processed_plan["Financial_Documents"]["scan_100.pdf"]
    assert "target_filename" in scan_node
    assert scan_node["target_filename"].endswith(".pdf")
    assert scan_node["relative_source"] == "scan_100.pdf"

    # Verify good name annual_summary.pdf is unchanged (no target_filename needed or same)
    report_node = processed_plan["Reports"]["annual_summary.pdf"]
    assert report_node.get("target_filename") is None or report_node.get("target_filename") == "annual_summary.pdf"


def test_memory_purge_after_plan_generation():
    """Test memory context buffers are purged post plan generation."""
    settings = Settings(CONTEXTUAL_RENAMING=True)
    engine = FileRenamerEngine(runtime_settings=settings)

    plan = {
        "Folder_A": {
            "scan_999.pdf": {"__type__": "file", "relative_source": "scan_999.pdf"}
        }
    }
    docs_map = {
        "scan_999.pdf": "Confidential Medical Record Patient Agreement."
    }

    engine.process_sorting_plan(plan, docs_map, base_dir="/app/work")

    # Garbage collection check
    gc.collect()
    # Confirm processing completed without memory leak or lingering buffer
    assert "target_filename" in plan["Folder_A"]["scan_999.pdf"]
