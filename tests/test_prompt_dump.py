"""Tests for debug-gated prompt dumping, path traversal validation, and content scrubbing."""

import os
import pytest
from pathlib import Path
from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    is_debug_active,
    is_prompt_dump_enabled,
    validate_prompt_dump_path,
    scrub_prompt_text,
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

    monkeypatch.setenv("PROMPT_DUMP_FILE", "/tmp/dump.txt")
    assert not is_prompt_dump_enabled()

    monkeypatch.setenv("DEBUG", "1")
    assert is_prompt_dump_enabled()

    monkeypatch.setenv("PROMPT_DUMP_FILE", "")
    assert not is_prompt_dump_enabled()


def test_validate_prompt_dump_path_valid():
    valid_paths = [
        "dump.txt",
        "/tmp/prompt_dump.txt",
        "logs/debug/dump.txt",
        "C:\\temp\\dump.txt",
    ]
    for path in valid_paths:
        # Should not raise any exception
        validate_prompt_dump_path(path)


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


def test_validate_prompt_dump_path_illegal_chars():
    illegal_paths = [
        "/tmp/dump\0file.txt",
        "/tmp/dump<file>.txt",
        "/tmp/dump?file.txt",
        "/tmp/dump*file.txt",
        "/tmp/dump|file.txt",
        "",
        "   ",
    ]
    for path in illegal_paths:
        with pytest.raises(ValueError):
            validate_prompt_dump_path(path)


def test_scrub_prompt_text():
    home_dir = str(Path.home())
    home_fwd = home_dir.replace("\\", "/")
    home_back = home_dir.replace("/", "\\")

    prompt = f"Analyze document at {home_fwd}/docs/report.pdf and {home_back}\\data\\secret.txt."
    scrubbed = scrub_prompt_text(prompt)

    assert home_fwd not in scrubbed
    assert home_back not in scrubbed
    assert "<USER_HOME>/docs/report.pdf" in scrubbed or "<USER_HOME>" in scrubbed
    assert "<USER_HOME>" in scrubbed


def test_prompt_dump_blocked_when_debug_off(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    dump_file = tmp_path / "dump.txt"
    monkeypatch.setenv("PROMPT_DUMP_FILE", str(dump_file))

    strategy = GenerativeNamingStrategy()
    # Mock GGUF / PyTorch execution in _run_prompt
    strategy._gguf_active = False

    # When debug is off, prompt dump should be disabled and _run_prompt should not create dump_file
    res = strategy._run_prompt("Test prompt body", 15)
    assert not dump_file.exists()
    assert res != "Mock Generated Folder Name"


def test_prompt_dump_success_under_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    dump_file = tmp_path / "dump.txt"
    monkeypatch.setenv("PROMPT_DUMP_FILE", str(dump_file))

    home_dir = str(Path.home())
    prompt_content = f"Target path is {home_dir}/documents/file.txt for analysis."

    strategy = GenerativeNamingStrategy()
    res = strategy._run_prompt(prompt_content, 15)

    assert res == "Mock Generated Folder Name"
    assert dump_file.exists()

    file_content = dump_file.read_text(encoding="utf-8")
    assert home_dir not in file_content
    assert "<USER_HOME>/documents/file.txt" in file_content
    assert "===PROMPT_END===" in file_content


def test_prompt_dump_traversal_rejection_under_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG", "1")
    traversal_path = str(tmp_path / "../dump.txt")
    monkeypatch.setenv("PROMPT_DUMP_FILE", traversal_path)

    strategy = GenerativeNamingStrategy()
    with pytest.raises(ValueError, match="relative directory traversal"):
        strategy._run_prompt("Test prompt body", 15)


def test_get_cluster_keywords_fallback_when_dump_disabled(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setenv("PROMPT_DUMP_FILE", "/tmp/dump.txt")

    strategy = GenerativeNamingStrategy()
    strategy.generator = None
    strategy._gguf_active = False

    # Should fall back to super()._get_cluster_keywords
    keywords = strategy._get_cluster_keywords(["doc1.txt", "doc2.txt"])
    assert keywords is not None
