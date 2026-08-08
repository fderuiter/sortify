import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.user_space_bootstrap import (
    bootstrap_binaries,
    check_internet_connection,
    get_bootstrap_bin_dir,
    inject_bootstrap_paths,
    verify_sqlcipher_encryption,
)


def test_get_bootstrap_bin_dir():
    """Verify that the bootstrap bin dir is correctly placed within the app configuration directory."""
    from app.config import get_app_dir

    bin_dir = get_bootstrap_bin_dir()
    assert bin_dir == get_app_dir() / "binaries"


def test_check_internet_connection():
    """Verify that internet connection check detects connectivity correctly."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = MagicMock()
        assert check_internet_connection() is True

    with patch("urllib.request.urlopen", side_effect=Exception("Timeout")):
        assert check_internet_connection() is False


def test_verify_sqlcipher_encryption_success():
    """Verify sqlcipher verification detects dynamic loading and works on a successful scenario."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [
        ("secure_data",),  # row select
        ("4.5.1",),  # version check
    ]
    mock_conn.cursor.return_value = mock_cursor

    mock_dbapi2 = MagicMock()
    mock_dbapi2.connect.return_value = mock_conn
    mock_sqlcipher = MagicMock()
    mock_sqlcipher.dbapi2 = mock_dbapi2

    with patch.dict("sys.modules", {"sqlcipher3": mock_sqlcipher}):
        assert verify_sqlcipher_encryption() is True


def test_verify_sqlcipher_encryption_failure():
    """Verify sqlcipher verification correctly catches malfunctions or missing cipher options."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [
        None,  # row select empty
    ]
    mock_conn.cursor.return_value = mock_cursor

    mock_dbapi2 = MagicMock()
    mock_dbapi2.connect.return_value = mock_conn
    mock_sqlcipher = MagicMock()
    mock_sqlcipher.dbapi2 = mock_dbapi2

    with patch.dict("sys.modules", {"sqlcipher3": mock_sqlcipher}):
        assert verify_sqlcipher_encryption() is False


def test_inject_bootstrap_paths(tmp_path):
    """Verify search paths are properly added to sys.path and OS PATH env variables."""
    bin_dir = tmp_path / "binaries"
    sqlcipher3_path = bin_dir / "sqlcipher3"
    sqlcipher3_path.mkdir(parents=True, exist_ok=True)

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=bin_dir,
        ),
        patch.object(sys, "path", sys.path.copy()) as mock_sys_path,
        patch("os.add_dll_directory", create=True) as mock_add_dll,
        patch.dict("os.environ", {"PATH": "existing_path"}),
    ):
        inject_bootstrap_paths()
        assert str(bin_dir) in mock_sys_path
        if sys.platform == "win32":
            mock_add_dll.assert_any_call(str(bin_dir))
            mock_add_dll.assert_any_call(str(sqlcipher3_path))
            assert str(bin_dir) in os.environ["PATH"]
            assert str(sqlcipher3_path) in os.environ["PATH"]


def test_bootstrap_binaries_bypass_if_cached(tmp_path):
    """Verify that if binaries are cached and verified, we bypass all download and fallback steps."""

    mock_app_dir = tmp_path / ".autosorter"
    mock_sqlcipher = mock_app_dir / "binaries" / "sqlcipher3"
    mock_sqlcipher.mkdir(parents=True)

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=mock_app_dir / "binaries",
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
        patch("app.core.user_space_bootstrap.check_internet_connection") as mock_check,
        patch("importlib.util.find_spec") as mock_find,
    ):
        res = bootstrap_binaries()
        assert res is True
        mock_check.assert_not_called()
        mock_find.assert_not_called()


def test_bootstrap_binaries_download_flow(tmp_path):
    """Verify that when binaries are missing, we bypass download and resolve locally instead."""
    mock_bin_dir = tmp_path / "binaries"
    mock_bin_dir.mkdir()

    mock_local_sqlcipher_dir = tmp_path / "local_sqlcipher"
    mock_local_sqlcipher_dir.mkdir()
    ext = ".pyd" if sys.platform == "win32" else ".so"
    native_filename = f"native_module{ext}"
    (mock_local_sqlcipher_dir / native_filename).write_text("compiled code")

    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [str(mock_local_sqlcipher_dir)]

    mock_urlopen = MagicMock()

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=mock_bin_dir,
        ),
        patch(
            "app.core.user_space_bootstrap.check_internet_connection",
            return_value=True,
        ),
        patch("urllib.request.urlopen", mock_urlopen),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths"),
    ):
        res = bootstrap_binaries(force_download=True)
        assert res is True
        mock_urlopen.assert_not_called()
        assert (mock_bin_dir / "sqlcipher3").exists()
        assert (mock_bin_dir / "sqlcipher3" / native_filename).exists()


def test_bootstrap_binaries_download_failed_fallback(tmp_path):
    """Verify that when download fails, we fall back to copy cached local native libraries."""
    mock_bin_dir = tmp_path / "binaries"
    mock_bin_dir.mkdir()

    mock_local_sqlcipher_dir = tmp_path / "local_sqlcipher"
    mock_local_sqlcipher_dir.mkdir()
    ext = ".pyd" if sys.platform == "win32" else ".so"
    native_filename = f"native_module{ext}"
    (mock_local_sqlcipher_dir / native_filename).write_text("compiled code")

    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [str(mock_local_sqlcipher_dir)]

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=mock_bin_dir,
        ),
        patch(
            "app.core.user_space_bootstrap.check_internet_connection", return_value=True
        ),
        # force urlopen to fail
        patch("urllib.request.urlopen", side_effect=Exception("Download error")),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            side_effect=[False, True],
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths"),
    ):
        res = bootstrap_binaries()
        assert res is True
        assert (mock_bin_dir / "sqlcipher3").exists()
        assert (mock_bin_dir / "sqlcipher3" / native_filename).exists()


def test_bootstrap_binaries_failed_verification(tmp_path):
    """Verify that if encryption verification fails after bootstrapping, we raise a RuntimeError."""
    mock_bin_dir = tmp_path / "binaries"
    mock_bin_dir.mkdir()

    mock_local_sqlcipher_dir = tmp_path / "local_sqlcipher"
    mock_local_sqlcipher_dir.mkdir()
    ext = ".pyd" if sys.platform == "win32" else ".so"
    native_filename = f"native_module{ext}"
    (mock_local_sqlcipher_dir / native_filename).write_text("compiled code")

    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [str(mock_local_sqlcipher_dir)]

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=mock_bin_dir,
        ),
        patch(
            "app.core.user_space_bootstrap.check_internet_connection",
            return_value=False,
        ),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths"),
    ):
        with pytest.raises(RuntimeError, match="Startup verification failed"):
            bootstrap_binaries()


def test_bootstrap_binaries_missing_wheels(tmp_path):
    """Verify that if local SQLCipher wheels are missing, we raise a descriptive RuntimeError."""
    mock_bin_dir = tmp_path / "binaries"
    mock_bin_dir.mkdir()

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=mock_bin_dir,
        ),
        patch("importlib.util.find_spec", return_value=None),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
    ):
        with pytest.raises(RuntimeError, match="SQLCipher local native library/wheels are missing"):
            bootstrap_binaries()


def test_bootstrap_binaries_incompatible_architecture(tmp_path):
    """Verify that if local SQLCipher binaries exist but are incompatible, we raise a descriptive RuntimeError."""
    mock_bin_dir = tmp_path / "binaries"
    mock_bin_dir.mkdir()

    mock_local_sqlcipher_dir = tmp_path / "local_sqlcipher"
    mock_local_sqlcipher_dir.mkdir()

    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = [str(mock_local_sqlcipher_dir)]

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir",
            return_value=mock_bin_dir,
        ),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
    ):
        with pytest.raises(RuntimeError, match="incompatible with current platform/architecture"):
            bootstrap_binaries()


def test_bootstrap_binaries_frozen_success(tmp_path):
    """Verify that in frozen PyInstaller mode, bootstrapping successfully verifies encryption directly."""
    with (
        patch("sys.frozen", True, create=True),
        patch("sys._MEIPASS", str(tmp_path), create=True),
        patch("os.add_dll_directory", create=True) as mock_add_dll,
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
    ):
        res = bootstrap_binaries()
        assert res is True


def test_bootstrap_binaries_frozen_failure(tmp_path):
    """Verify that in frozen PyInstaller mode, if verification fails, we raise RuntimeError."""
    with (
        patch("sys.frozen", True, create=True),
        patch("sys._MEIPASS", str(tmp_path), create=True),
        patch("os.add_dll_directory", create=True) as mock_add_dll,
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
    ):
        with pytest.raises(RuntimeError, match="SQLCipher database encryption failed to verify"):
            bootstrap_binaries()
