"""Unit tests for scripts/test_cli.py CLI argument parsing and help flag behavior."""

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from scripts.test_cli import main


def test_cli_smoke_script_help_flag():
    """Verify that python scripts/test_cli.py --help displays help and exits with 0."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script_path = os.path.join(repo_root, "scripts", "test_cli.py")

    env = os.environ.copy()
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, script_path, "--help"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "Dedicated CLI smoke test script" in result.stdout
    assert "usage:" in result.stdout.lower()
    assert "All CLI smoke tests passed successfully." not in result.stdout


def test_cli_smoke_script_h_flag():
    """Verify that python scripts/test_cli.py -h displays help and exits with 0."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script_path = os.path.join(repo_root, "scripts", "test_cli.py")

    env = os.environ.copy()
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, script_path, "-h"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert "Dedicated CLI smoke test script" in result.stdout
    assert "usage:" in result.stdout.lower()


def test_cli_smoke_script_invalid_flag():
    """Verify that passing an unknown flag to scripts/test_cli.py exits with error code 2."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script_path = os.path.join(repo_root, "scripts", "test_cli.py")

    env = os.environ.copy()
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, script_path, "--unrecognized-option"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr.lower()


def test_cli_smoke_script_in_process_help():
    """Verify in-process behavior of main() with --help."""
    with patch("sys.argv", ["test_cli.py", "--help"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
