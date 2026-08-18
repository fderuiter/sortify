import os
import stat
import subprocess
import sys
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from scripts.install_offline import (
    _extract_and_install_offline,
    offline_install,
    safe_extract_zip,
)


def test_safe_extract_zip_valid(tmp_path):
    """Test extracting a valid zip archive with no path traversal."""
    zip_path = tmp_path / "valid.zip"
    extract_dir = tmp_path / "extracted"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("test.txt", "hello world")
        zf.writestr("sub/data.txt", "nested content")

    safe_extract_zip(zip_path, extract_dir)

    assert (extract_dir / "test.txt").exists()
    assert (extract_dir / "test.txt").read_text() == "hello world"
    assert (extract_dir / "sub" / "data.txt").exists()
    assert (extract_dir / "sub" / "data.txt").read_text() == "nested content"

    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(extract_dir).st_mode)
        assert mode == 0o700


def test_safe_extract_zip_relative_traversal_aborts(tmp_path):
    """Test that safe_extract_zip aborts when a relative directory traversal path is present."""
    zip_path = tmp_path / "traversal.zip"
    extract_dir = tmp_path / "target"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escaped.txt", "malicious payload")

    with pytest.raises(ValueError, match="Directory traversal"):
        safe_extract_zip(zip_path, extract_dir)

    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_zip_deep_relative_traversal_aborts(tmp_path):
    """Test that safe_extract_zip rejects nested relative directory traversal."""
    zip_path = tmp_path / "nested_traversal.zip"
    extract_dir = tmp_path / "target"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("sub/../../escaped.txt", "malicious payload")

    with pytest.raises(ValueError, match="Directory traversal"):
        safe_extract_zip(zip_path, extract_dir)

    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_zip_absolute_path_aborts(tmp_path):
    """Test that safe_extract_zip rejects absolute member paths."""
    zip_path = tmp_path / "absolute.zip"
    extract_dir = tmp_path / "target"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/tmp/abs_file.txt", "malicious payload")

    with pytest.raises(ValueError, match="Directory traversal or absolute path"):
        safe_extract_zip(zip_path, extract_dir)


def test_safe_extract_zip_windows_drive_letter_aborts(tmp_path):
    """Test that safe_extract_zip rejects Windows drive letter member paths."""
    zip_path = tmp_path / "win_drive.zip"
    extract_dir = tmp_path / "target"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("C:/System32/cmd.exe", "malicious payload")

    with pytest.raises(ValueError, match="Directory traversal or absolute path"):
        safe_extract_zip(zip_path, extract_dir)


@patch("scripts.install_offline.subprocess.run")
@patch("scripts.install_offline.os.path.exists")
@patch("scripts.install_offline.os.path.isdir")
def test_extract_and_install_offline_requires_hashes(mock_isdir, mock_exists, mock_run):
    """Test that _extract_and_install_offline passes --require-hashes to uv pip install."""
    mock_exists.side_effect = lambda path: (
        False if path == "offline_bundle.zip" else True
    )
    mock_isdir.return_value = True

    _extract_and_install_offline("uv")

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    cmd = args[0]

    assert "uv" in cmd
    assert "pip" in cmd
    assert "install" in cmd
    assert "--require-hashes" in cmd
    assert "-r" in cmd
    assert "offline_bundle/requirements.txt" in cmd


@patch("scripts.install_offline.safe_extract_zip")
@patch("scripts.install_offline.os.path.exists", return_value=True)
def test_extract_and_install_offline_traversal_exit(mock_exists, mock_extract):
    """Test that extraction failure due to directory traversal exits with status code 1."""
    mock_extract.side_effect = ValueError("Directory traversal attempt detected")

    with pytest.raises(SystemExit) as exc_info:
        _extract_and_install_offline("uv")

    assert exc_info.value.code == 1


@patch("scripts.install_offline.subprocess.run")
@patch("scripts.install_offline.os.path.exists")
@patch("scripts.install_offline.os.path.isdir")
def test_extract_and_install_offline_hash_mismatch_exit(
    mock_isdir, mock_exists, mock_run
):
    """Test that installation failure (e.g. hash mismatch) exits with status code 1."""
    mock_exists.side_effect = lambda path: (
        False if path == "offline_bundle.zip" else True
    )
    mock_isdir.return_value = True
    mock_run.side_effect = subprocess.CalledProcessError(1, "uv pip install")

    with pytest.raises(SystemExit) as exc_info:
        _extract_and_install_offline("uv")

    assert exc_info.value.code == 1


@patch("scripts.install_offline._extract_and_install_offline")
@patch("scripts.install_offline.get_uv_cmd", return_value="uv")
def test_offline_install_subcommand(mock_get_uv, mock_extract_install):
    """Test offline_install function runner."""
    args = MagicMock()
    offline_install(args)
    mock_extract_install.assert_called_once_with("uv")
