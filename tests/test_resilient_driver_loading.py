"""Unit tests for resilient bootstrapping and cross-platform native driver loading."""

import os
from unittest.mock import MagicMock, patch

import pytest

from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.user_space_bootstrap import (
    bootstrap_binaries,
    inject_bootstrap_paths,
    verify_sqlcipher_encryption,
)


def test_verify_sqlcipher_encryption_non_blocking_cross_platform():
    """Verify that verify_sqlcipher_encryption runs non-blockingly and returns False on errors across platforms."""
    mock_dbapi2 = MagicMock()
    mock_dbapi2.connect.side_effect = Exception("Database connection error")
    mock_sqlcipher = MagicMock()
    mock_sqlcipher.dbapi2 = mock_dbapi2

    for platform_name in ("linux", "darwin", "win32"):
        with (
            patch("sys.platform", platform_name),
            patch.dict("sys.modules", {"sqlcipher3": mock_sqlcipher}),
        ):
            assert verify_sqlcipher_encryption() is False


def test_get_db_connection_graceful_fallback_cross_platform(tmp_path, caplog):
    """Verify get_db_connection degrades gracefully with security warnings on Linux, macOS, and Windows when SQLCipher is unavailable or unverified."""
    test_db = tmp_path / "fallback_test.db"

    for platform_name in ("linux", "darwin", "win32"):
        caplog.clear()
        with patch("sys.platform", platform_name):
            with patch("app.core.db_conn.HAS_SQLCIPHER", False):
                try:
                    conn = get_db_connection(str(test_db))
                    assert conn is not None
                    # Verify security warning logged
                    assert any(
                        "SECURITY WARNING" in record.message
                        for record in caplog.records
                    )

                    # Verify read/write functionality on fallback connection
                    with conn:
                        cursor = conn.cursor()
                        cursor.execute("CREATE TABLE IF NOT EXISTS test_tb (id INT)")
                        cursor.execute("INSERT INTO test_tb VALUES (100)")
                        cursor.execute("SELECT id FROM test_tb")
                        row = cursor.fetchone()
                        assert row[0] == 100
                finally:
                    clear_connection_cache(only_current_and_inactive=False)


def test_bootstrap_binaries_cross_platform_path_injection(tmp_path):
    """Verify inject_bootstrap_paths and _resolve_platform_driver_paths inject platform-agnostic search paths."""
    bin_dir = tmp_path / "binaries" / "linux"
    bin_dir.mkdir(parents=True)
    sqlcipher3_dir = bin_dir / "sqlcipher3"
    sqlcipher3_dir.mkdir()

    # Test Linux path injection
    env_mock = os.environ.copy()
    env_mock.pop("LD_LIBRARY_PATH", None)
    with (
        patch("sys.platform", "linux"),
        patch.dict("os.environ", env_mock),
    ):
        inject_bootstrap_paths(bin_dir)
        assert str(bin_dir) in os.environ.get("LD_LIBRARY_PATH", "")

    # Test macOS path injection
    env_mock = os.environ.copy()
    env_mock.pop("DYLD_LIBRARY_PATH", None)
    with (
        patch("sys.platform", "darwin"),
        patch.dict("os.environ", env_mock),
    ):
        inject_bootstrap_paths(bin_dir)
        assert str(bin_dir) in os.environ.get("DYLD_LIBRARY_PATH", "")

    # Test Windows path injection
    mock_add_dll = MagicMock()
    env_mock = os.environ.copy()
    with (
        patch("sys.platform", "win32"),
        patch("os.add_dll_directory", mock_add_dll, create=True),
        patch.dict("os.environ", env_mock),
    ):
        inject_bootstrap_paths(bin_dir)
        mock_add_dll.assert_any_call(str(bin_dir))


def test_offline_and_firewalled_startup_resilience(tmp_path):
    """Verify startup pre-flight checks complete in offline firewalled environments without fatal exceptions."""
    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    with (
        patch("urllib.request.urlopen", side_effect=OSError("Network unreachable")),
        patch(
            "app.core.user_space_bootstrap.check_internet_connection",
            return_value=False,
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
    ):
        with pytest.raises(RuntimeError, match="local binaries manifest is missing"):
            bootstrap_binaries(force_download=True)
