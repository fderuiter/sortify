"""Unit tests for PatternTokenFormatter in app/core/pattern_formatter.py."""

from datetime import datetime

from app.core.pattern_formatter import PatternTokenFormatter, format_pattern


def test_parse_tokens():
    pattern = "{date}_{category}_{original}_{extension}_{seq}"
    tokens = PatternTokenFormatter.parse_tokens(pattern)
    assert set(tokens) == {"date", "category", "original", "extension", "seq"}


def test_format_pattern_standard():
    meta = {
        "date": "2026-10-07",
        "category": "Invoices",
        "original": "invoice_123",
        "extension": ".pdf",
        "seq": 1,
    }
    pattern = "{date}_{category}_{original}"
    result = format_pattern(pattern, metadata=meta)
    assert result == "2026-10-07_Invoices_invoice_123.pdf"


def test_format_pattern_with_explicit_extension_token():
    meta = {
        "date": "2026-10-07",
        "category": "Reports",
        "original": "quarterly",
        "extension": ".docx",
        "seq": 2,
    }
    pattern = "{date}_{category}_{original}{extension}"
    result = format_pattern(pattern, metadata=meta)
    assert result == "2026-10-07_Reports_quarterly.docx"


def test_format_pattern_missing_metadata_fallback():
    # Category is missing
    meta = {
        "date": "2026-10-07",
        "original": "document",
        "extension": ".pdf",
    }
    pattern = "{date}_{category}_{original}"
    result = format_pattern(pattern, metadata=meta)
    assert result == "2026-10-07_uncategorized_document.pdf"


def test_format_pattern_unknown_token_fallback():
    meta = {
        "date": "2026-10-07",
        "original": "doc",
        "extension": ".txt",
    }
    pattern = "{date}_{unknown}_{original}"
    result = format_pattern(pattern, metadata=meta)
    assert result == "2026-10-07_unknown_doc.txt"


def test_format_pattern_sequence_formatting():
    meta = {
        "date": "2026-10-07",
        "category": "Docs",
        "original": "item",
        "extension": ".pdf",
    }
    pattern = "{date}_{category}_{seq}_{original}"
    result = format_pattern(pattern, metadata=meta, seq=5)
    assert result == "2026-10-07_Docs_05_item.pdf"


def test_format_pattern_sanitization_special_chars():
    meta = {
        "date": "2026-10-07",
        "category": "Tax:2026*",
        "original": "my<file>?name",
        "extension": ".pdf",
    }
    pattern = "{date}_{category}_{original}"
    result = format_pattern(pattern, metadata=meta)
    # Invalid characters like :, *, <, >, ? should be sanitized to underscores/safe chars
    assert ":" not in result
    assert "*" not in result
    assert "<" not in result
    assert ">" not in result
    assert "?" not in result
    assert result.endswith(".pdf")


def test_format_pattern_fallback_to_original_on_empty_pattern():
    fallback = "original_report.pdf"
    result = format_pattern("", fallback_original=fallback)
    assert result == "original_report.pdf"


def test_format_pattern_fallback_to_original_on_invalid_eval():
    fallback = "fallback_doc.pdf"
    # Even if pattern triggers error or is weird
    result = format_pattern(None, fallback_original=fallback)
    assert result == "fallback_doc.pdf"


def test_format_pattern_datetime_object():
    now = datetime(2026, 10, 7)
    meta = {
        "date": now,
        "category": "Finance",
        "original": "statement",
        "extension": ".csv",
    }
    pattern = "{date}_{category}_{original}"
    result = format_pattern(pattern, metadata=meta)
    assert result == "2026-10-07_Finance_statement.csv"
