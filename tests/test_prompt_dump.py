"""Tests for debug-gated prompt dumping, path traversal validation, and content scrubbing."""

from pathlib import Path

import pytest

from app.config import get_debug_log_dir
from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    is_debug_active,
    is_prompt_dump_enabled,
    redact_sensitive_text,
    scrub_prompt_text,
    validate_prompt_dump_path,
)


def test_is_debug_active(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    assert not is_debug_active()

    for val in ("1", "true", "TRUE", "Yes", "on", "  1  "):
        monkeypatch.setenv("DEBUG", val)
        assert is_debug_active()

    for val in ("0", "false", "no", "off", "invalid"):
        monkeypatch.setenv("DEBUG", val)
        assert not is_debug_active()


def test_is_prompt_dump_enabled(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("PROMPT_DUMP_FILE", raising=False)
    assert not is_prompt_dump_enabled()

    monkeypatch.setenv("PROMPT_DUMP_FILE", "dump.txt")
    assert not is_prompt_dump_enabled()

    monkeypatch.setenv("DEBUG", "1")
    assert is_prompt_dump_enabled()

    monkeypatch.setenv("PROMPT_DUMP_FILE", "")
    assert not is_prompt_dump_enabled()


def test_validate_prompt_dump_path_valid():
    debug_dir = get_debug_log_dir()
    valid_paths = [
        "dump.txt",
        "logs/debug/dump.txt",
        "subfolder/dump.txt",
        str(debug_dir / "valid_dump.txt"),
    ]
    for path in valid_paths:
        resolved = validate_prompt_dump_path(path)
        assert resolved.is_relative_to(debug_dir)


def test_validate_prompt_dump_path_traversal():
    traversal_paths = [
        "../dump.txt",
        "a/../b/dump.txt",
        "/tmp/../etc/passwd",
        "..",
        "folder/..",
    ]
    for path in traversal_paths:
        with pytest.raises(ValueError, match="relative directory traversal"):
            validate_prompt_dump_path(path)


def test_validate_prompt_dump_path_external_rejection():
    external_paths = [
        "/tmp/prompt_dump.txt",
        "/var/log/dump.txt",
        "C:\\temp\\dump.txt",
    ]
    for path in external_paths:
        with pytest.raises(ValueError, match="resolves outside"):
            validate_prompt_dump_path(path)


def test_validate_prompt_dump_path_illegal_chars():
    illegal_paths = [
        "dump\0file.txt",
        "dump<file>.txt",
        "dump?file.txt",
        "dump*file.txt",
        "dump|file.txt",
        "",
        "   ",
    ]
    for path in illegal_paths:
        with pytest.raises(ValueError):
            validate_prompt_dump_path(path)


def test_redact_sensitive_text():
    prompt = (
        "Here are some historical examples of documents and their corresponding user-corrected folder names:\n\n"
        "Example 1:\n"
        "Document: Confidential Medical Report for Subject 101\n"
        "Folder Name: Medical\n\n"
        "Now, generate a short, descriptive natural language folder name for a folder containing these documents.\n"
        "Documents: Patient blood pressure and lab diagnostic results\n"
        "Folder Name:"
    )
    redacted = redact_sensitive_text(prompt)

    assert "Confidential Medical Report for Subject 101" not in redacted
    assert "Patient blood pressure and lab diagnostic results" not in redacted
    assert "[REDACTED_HISTORICAL_SNIPPET: 43 chars]" in redacted
    assert "[REDACTED_DOCUMENT_TEXT: 49 chars]" in redacted
    assert "Folder Name: Medical" in redacted


def test_scrub_prompt_text():
    home_dir = str(Path.home())
    home_fwd = home_dir.replace("\\", "/")
    home_back = home_dir.replace("/", "\\")

    prompt = f"Analyze document at {home_fwd}/docs/report.pdf and {home_back}\\data\\secret.txt.\nDocuments: Secret doc\nFolder Name:"
    scrubbed = scrub_prompt_text(prompt)

    assert home_fwd not in scrubbed
    assert home_back not in scrubbed
    assert "<USER_HOME>/docs/report.pdf" in scrubbed or "<USER_HOME>" in scrubbed
    assert "<USER_HOME>" in scrubbed
    assert "Secret doc" not in scrubbed
    assert "[REDACTED_DOCUMENT_TEXT: 10 chars]" in scrubbed


def test_prompt_dump_blocked_when_debug_off(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    dump_file = "dump_off.txt"
    monkeypatch.setenv("PROMPT_DUMP_FILE", dump_file)

    strategy = GenerativeNamingStrategy()
    strategy._gguf_active = False

    res = strategy._run_prompt("Test prompt body\nDocuments: confidential content\nFolder Name:", 15)
    expected_path = get_debug_log_dir() / dump_file
    assert not expected_path.exists()
    assert res != "Mock Generated Folder Name"


def test_prompt_dump_success_under_debug(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    dump_filename = "test_dump_success.txt"
    monkeypatch.setenv("PROMPT_DUMP_FILE", dump_filename)

    home_dir = str(Path.home())
    prompt_content = (
        f"Target path is {home_dir}/documents/file.txt for analysis.\n"
        "Documents: Patient confidential lab analysis\n"
        "Folder Name:"
    )

    strategy = GenerativeNamingStrategy()
    res = strategy._run_prompt(prompt_content, 15)

    assert res == "Mock Generated Folder Name"
    expected_path = get_debug_log_dir() / dump_filename
    assert expected_path.exists()

    file_content = expected_path.read_text(encoding="utf-8")
    assert home_dir not in file_content
    assert "<USER_HOME>/documents/file.txt" in file_content
    assert "Patient confidential lab analysis" not in file_content
    assert "[REDACTED_DOCUMENT_TEXT: 33 chars]" in file_content
    assert "===PROMPT_END===" in file_content

    # Clean up created debug log file
    expected_path.unlink(missing_ok=True)


def test_prompt_dump_traversal_rejection_under_debug(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setenv("PROMPT_DUMP_FILE", "../dump.txt")

    strategy = GenerativeNamingStrategy()
    with pytest.raises(ValueError, match="relative directory traversal"):
        strategy._run_prompt("Test prompt body", 15)


def test_prompt_dump_external_path_rejection_under_debug(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setenv("PROMPT_DUMP_FILE", "/tmp/external_dump.txt")

    strategy = GenerativeNamingStrategy()
    with pytest.raises(ValueError, match="resolves outside"):
        strategy._run_prompt("Test prompt body", 15)


def test_get_cluster_keywords_fallback_when_dump_disabled(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setenv("PROMPT_DUMP_FILE", "dump.txt")

    strategy = GenerativeNamingStrategy()
    strategy.generator = None
    strategy._gguf_active = False

    keywords = strategy._get_cluster_keywords(["doc1.txt", "doc2.txt"])
    assert keywords is not None
