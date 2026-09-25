"""Unit tests for in-place PII pattern scrubbing in path utilities and GenerativeNamingStrategy."""

from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.path_utils import (
    sanitize_folder_key,
    sanitize_name,
    scrub_pii_from_filename,
)


def test_scrub_pii_from_filename_ssn():
    """Verify SSN pattern scrubbing from filenames."""
    raw = "Tax Statement 123-45-6789 2023.pdf"
    clean = scrub_pii_from_filename(raw)
    assert "123-45-6789" not in clean
    assert "Tax Statement" in clean
    assert "2023.pdf" in clean


def test_scrub_pii_from_filename_credit_card():
    """Verify credit card number scrubbing from filenames."""
    raw = "Invoice 4532-0150-1234-5678 Paid.docx"
    clean = scrub_pii_from_filename(raw)
    assert "4532-0150-1234-5678" not in clean
    assert "Invoice" in clean
    assert "Paid.docx" in clean


def test_scrub_pii_from_filename_email():
    """Verify email pattern scrubbing from filenames."""
    raw = "Contact Info john.doe@example.com Notes.txt"
    clean = scrub_pii_from_filename(raw)
    assert "john.doe@example.com" not in clean
    assert "Contact Info" in clean
    assert "Notes.txt" in clean


def test_scrub_pii_from_filename_phone():
    """Verify phone number scrubbing from filenames."""
    raw = "Call Logs 555-123-4567 Details"
    clean = scrub_pii_from_filename(raw)
    assert "555-123-4567" not in clean
    assert "Call Logs" in clean
    assert "Details" in clean


def test_scrub_pii_from_filename_medical_record():
    """Verify medical record / patient ID pattern scrubbing."""
    raw1 = "Patient MRN-98765 Lab Results"
    clean1 = scrub_pii_from_filename(raw1)
    assert "MRN-98765" not in clean1
    assert "Lab Results" in clean1

    raw2 = "Patient ID 12345 Healthcare File"
    clean2 = scrub_pii_from_filename(raw2)
    assert "Patient ID 12345" not in clean2
    assert "Healthcare File" in clean2


def test_sanitize_name_invokes_pii_scrubbing():
    """Verify sanitize_name scrubs PII before standard OS sanitization."""
    raw = "User john.doe@example.com Record"
    sanitized = sanitize_name(raw)
    assert "john.doe@example.com" not in sanitized
    assert "User" in sanitized
    assert "Record" in sanitized


def test_sanitize_folder_key_invokes_pii_scrubbing():
    """Verify sanitize_folder_key scrubs PII from folder keys."""
    raw = "Folder/123-45-6789/Documents"
    sanitized_key, transformed = sanitize_folder_key(raw)
    assert transformed is True
    assert "123-45-6789" not in sanitized_key
    assert "Folder" in sanitized_key
    assert "Documents" in sanitized_key


def test_generative_naming_strategy_scrubs_pii_and_retains_descriptive_name():
    """Verify GenerativeNamingStrategy scrubs PII from generated folder candidates."""
    strategy = GenerativeNamingStrategy()
    strategy.stop_words = {"the", "and"}
    strategy.max_features = 3
    strategy.generator = MagicMock()

    documents = ["Medical tax statements for 2023", "Hospital visit records"]

    with patch.object(strategy, "_run_prompt", return_value="Medical Tax Statements 123-45-6789"):
        folder_name = strategy._get_cluster_keywords(documents)
        assert "123-45-6789" not in folder_name
        assert "Medical Tax Statements" in folder_name


def test_generative_naming_strategy_over_scrubbed_fallback():
    """Verify over-scrubbed candidate (only PII) falls back to non-generative TF-IDF keyphrase extraction."""
    strategy = GenerativeNamingStrategy()
    strategy.stop_words = {"the", "and"}
    strategy.max_features = 3
    strategy.generator = MagicMock()

    documents = ["Healthcare report for annual checkup", "Medical laboratory assessment"]

    # Prompt returns candidate that consists entirely of PII
    with patch.object(strategy, "_run_prompt", return_value="john.doe@example.com 123-45-6789"):
        folder_name = strategy._get_cluster_keywords(documents)
        # Should fall back to super()._get_cluster_keywords(documents) (TF-IDF extraction)
        assert "john.doe@example.com" not in folder_name
        assert "123-45-6789" not in folder_name
        assert len(folder_name) >= 2
        assert folder_name != "Unnamed_safe"
