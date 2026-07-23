import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import scripts.validate_docs
from scripts.validate_docs import compute_sha256, generate, verify


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path, monkeypatch):
    # Set the PROJECT_ROOT and MANIFEST_PATH to use the temp directory
    monkeypatch.setattr("scripts.validate_docs.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        "scripts.validate_docs.MANIFEST_PATH",
        tmp_path / "docs" / "doc_manifest.json",
    )


def test_compute_sha256_normalization(tmp_path):
    file1 = tmp_path / "file1.txt"
    file2 = tmp_path / "file2.txt"

    # Unix line endings
    file1.write_bytes(b"hello\nworld\n")
    # Windows line endings
    file2.write_bytes(b"hello\r\nworld\r\n")

    hash1 = compute_sha256(file1)
    hash2 = compute_sha256(file2)

    assert hash1 == hash2
    assert hash1 == "4a1e67f2fe1d1cc7b31d0ca2ec441da4778203a036a77da10344c85e24ff0f92"


@patch("scripts.validate_docs.run_generation")
def test_generate(mock_run, tmp_path):
    mock_run.return_value = {"docs/test_doc.md"}

    doc_file = tmp_path / "docs" / "test_doc.md"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_text("Hello Documentation", encoding="utf-8")

    generate()

    manifest_file = tmp_path / "docs" / "doc_manifest.json"
    assert manifest_file.exists()

    with open(manifest_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "docs/test_doc.md" in data
    assert data["docs/test_doc.md"] == compute_sha256(doc_file)


@patch("scripts.validate_docs.run_generation")
def test_verify_success(mock_run, tmp_path):
    mock_run.return_value = {"docs/test_doc.md"}
    doc_file = tmp_path / "docs" / "test_doc.md"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_text("Hello Documentation", encoding="utf-8")

    # Generate first to make sure it exists
    generate()

    # Run verify (should succeed without error)
    verify()


@patch("scripts.validate_docs.run_generation")
def test_verify_unlisted_file(mock_run, tmp_path):
    doc_file = tmp_path / "docs" / "test_doc.md"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_text("Hello Documentation", encoding="utf-8")

    # Only test_doc.md is generated at first to generate valid manifest
    mock_run.return_value = {"docs/test_doc.md"}
    generate()

    # But now, generator also produces docs/unlisted.md
    mock_run.return_value = {"docs/test_doc.md", "docs/unlisted.md"}
    unlisted_file = tmp_path / "docs" / "unlisted.md"
    unlisted_file.write_text("Unlisted content", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        verify()

    assert exc_info.value.code == 1


@patch("scripts.validate_docs.run_generation")
def test_verify_orphaned_entry(mock_run, tmp_path):
    doc_file1 = tmp_path / "docs" / "test_doc.md"
    doc_file1.parent.mkdir(parents=True, exist_ok=True)
    doc_file1.write_text("Hello Documentation", encoding="utf-8")

    doc_file2 = tmp_path / "docs" / "orphaned.md"
    doc_file2.write_text("Some text", encoding="utf-8")

    # Both files generated and in manifest
    mock_run.return_value = {"docs/test_doc.md", "docs/orphaned.md"}
    generate()

    # But now, generator only produces test_doc.md
    mock_run.return_value = {"docs/test_doc.md"}

    with pytest.raises(SystemExit) as exc_info:
        verify()

    assert exc_info.value.code == 1


@patch("scripts.validate_docs.run_generation")
def test_verify_pre_commit_out_of_sync(mock_run, tmp_path):
    doc_file = tmp_path / "docs" / "test_doc.md"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_text("Old Documentation Content", encoding="utf-8")

    # Old content is generated and saved to manifest
    mock_run.return_value = {"docs/test_doc.md"}
    generate()

    # During run_generation, the files are regenerated with NEW content (because user changed config but didn't run generate)
    def side_effect_generator():
        doc_file.write_text("New Documentation Content", encoding="utf-8")
        return {"docs/test_doc.md"}

    mock_run.side_effect = side_effect_generator

    with pytest.raises(SystemExit) as exc_info:
        verify()

    assert exc_info.value.code == 1


@patch("scripts.validate_docs.run_generation")
def test_verify_manifest_out_of_sync(mock_run, tmp_path):
    doc_file = tmp_path / "docs" / "test_doc.md"
    doc_file.parent.mkdir(parents=True, exist_ok=True)
    doc_file.write_text("Hello Documentation", encoding="utf-8")

    # Manifest and files generated normally
    mock_run.return_value = {"docs/test_doc.md"}
    generate()

    # Manually corrupt the manifest
    manifest_file = tmp_path / "docs" / "doc_manifest.json"
    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    manifest_data["docs/test_doc.md"] = "incorrecthash"
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    with pytest.raises(SystemExit) as exc_info:
        verify()

    assert exc_info.value.code == 1
