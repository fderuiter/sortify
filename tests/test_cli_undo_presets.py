"""Tests for integrated undo subcommand and preset CLI flags."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.test_cli_subcommands import create_sample_corpus, run_cli


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_default_to_latest(tmp_path, monkeypatch):
    """Verify executing 'app/main.py undo' without flags defaults to rolling back --latest."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    src_dir = tmp_path / "workspace"
    create_sample_corpus(src_dir)

    # Execute sort
    code_sort, stdout_sort, stderr_sort = run_cli(["sort", str(src_dir), "--json"])
    assert code_sort == 0, f"Sort failed: {stderr_sort}"

    # Execute undo with no subcommand flags (should default to --latest)
    code_undo, stdout_undo, stderr_undo = run_cli(["undo", "--json"])
    assert code_undo == 0, f"Undo default failed: {stderr_undo}"
    data = json.loads(stdout_undo)
    assert data["status"] == "success"


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_latest_explicit_flag(tmp_path, monkeypatch):
    """Verify executing 'app/main.py undo --latest' rolls back the last sorting session."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    src_dir = tmp_path / "workspace"
    create_sample_corpus(src_dir)

    # Execute sort
    code_sort, stdout_sort, stderr_sort = run_cli(["sort", str(src_dir), "--json"])
    assert code_sort == 0, f"Sort failed: {stderr_sort}"

    # Execute undo --latest --json
    code_undo, stdout_undo, stderr_undo = run_cli(["undo", "--latest", "--json"])
    assert code_undo == 0, f"Undo latest failed: {stderr_undo}"
    data = json.loads(stdout_undo)
    assert data["status"] == "success"


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_no_history_error(tmp_path, monkeypatch):
    """Verify undo when no history sessions exist returns non-zero code and error message."""
    app_dir = tmp_path / "empty_app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    code, stdout, stderr = run_cli(["undo", "--latest", "--json"])
    assert code == 1
    data = json.loads(stdout)
    assert data["status"] == "error"
    assert "No historical sorting sessions" in data["message"]


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_preset_documents_resolution(tmp_path, monkeypatch):
    """Verify --preset documents resolves to ~/Documents."""
    fake_home = tmp_path / "user_home"
    docs_dir = fake_home / "Documents"
    create_sample_corpus(docs_dir)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    code, stdout, stderr = run_cli(["scan", "--preset", "documents", "--json"])
    assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert data["status"] == "success"
    assert Path(data["target_directory"]).resolve() == docs_dir.resolve()
