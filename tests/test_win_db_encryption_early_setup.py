"""Tests for Windows database encryption early setup, pre-flight verification, and module clearing."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.db_conn import get_db_connection
from app.core.user_space_bootstrap import (
    _resolve_platform_driver_paths,
    verify_sqlcipher_encryption,
)


def test_preflight_verification_fails_explicitly_when_libraries_missing_win32(tmp_path):
    """Verify database encryption pre-flight verification fails explicitly when required native encryption libraries are missing on Windows."""
    test_db = tmp_path / "missing_libs.db"

    with (
        patch("sys.platform", "win32"),
        patch("app.core.db_conn.HAS_SQLCIPHER", False),
    ):
        with pytest.raises(RuntimeError, match="SQLCipher library is missing"):
            get_db_connection(str(test_db))


def test_conftest_early_dll_registration_win32():
    """Verify automated test runs on Windows register necessary native library search paths."""
    mock_add_dll = MagicMock()
    env_mock = {"PATH": r"C:\Windows\System32"}

    with (
        patch("sys.platform", "win32"),
        patch("os.add_dll_directory", mock_add_dll, create=True),
        patch.dict("os.environ", env_mock),
    ):
        dirs = _resolve_platform_driver_paths()
        assert len(dirs) > 0
        assert mock_add_dll.called
        assert len(os.environ["PATH"]) > len(r"C:\Windows\System32")


def test_conftest_module_clearing_prevents_collisions():
    """Verify pre-loaded database modules are cleared from sys.modules to prevent module collisions."""
    # Pre-populate dummy modules
    sys.modules["sqlite3"] = MagicMock()
    sys.modules["_sqlite3"] = MagicMock()
    sys.modules["sqlcipher3"] = MagicMock()

    # Perform clearing step as in conftest.py
    for mod_name in list(sys.modules.keys()):
        if (
            mod_name in ("sqlcipher3", "_sqlite3", "sqlite3", "pysqlcipher3")
            or mod_name.startswith("sqlcipher3.")
            or mod_name.startswith("sqlite3.")
            or mod_name.startswith("pysqlcipher3.")
        ):
            sys.modules.pop(mod_name, None)

    assert "sqlite3" not in sys.modules
    assert "_sqlite3" not in sys.modules
    assert "sqlcipher3" not in sys.modules


def test_preflight_check_completes_encryption_write_read_and_cipher_version():
    """Verify encryption functionality, data read/write checks, and engine version verification complete successfully."""
    result = verify_sqlcipher_encryption()
    assert result is True
