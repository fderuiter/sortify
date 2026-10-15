"""Unit tests for SQLCipher native extension fallback loading and dynamic C library exception handling on Linux."""

import importlib.util
import types
from unittest.mock import MagicMock, patch

import pytest


class DummyRow:
    """Dummy Row class for ABC registration testing."""
    pass


def create_mock_sqlite3():
    mod = types.ModuleType("mock_sqlite3")
    mod.version = "2.6.0"
    mod.sqlite_version = "3.45.0"
    mod.Row = DummyRow
    mod.register_adapter = MagicMock()
    mod.register_converter = MagicMock()
    mod.__all__ = ["version", "sqlite_version", "Row", "register_adapter", "register_converter"]
    return mod


def test_load_sqlcipher_native_libraries_failure():
    """Verify that _load_sqlcipher_native_libraries handles ctypes LoadLibrary failures gracefully."""
    spec = importlib.util.spec_from_file_location(
        "linux_dbapi2_fail_libs",
        "/app/sortify/app/binaries/linux/sqlcipher3/dbapi2.py",
    )
    dbapi2 = importlib.util.module_from_spec(spec)

    mock_sqlite3 = create_mock_sqlite3()

    with (
        patch("ctypes.cdll.LoadLibrary", side_effect=OSError("libsqlcipher.so not found")),
        patch.dict("sys.modules", {"sqlcipher3._sqlite3": mock_sqlite3}),
    ):
        spec.loader.exec_module(dbapi2)
        loaded, errors = dbapi2._load_sqlcipher_native_libraries()
        assert loaded is False
        assert len(errors) == 3
        assert all("libsqlcipher.so not found" in err for err in errors)


def test_dbapi2_fallback_to_standard_sqlite3_on_loadlibrary_failure():
    """Verify fallback import paths when dynamic shared object loading fails but fallback symbols are available."""
    spec = importlib.util.spec_from_file_location(
        "linux_dbapi2_fallback",
        "/app/sortify/app/binaries/linux/sqlcipher3/dbapi2.py",
    )
    module = importlib.util.module_from_spec(spec)

    mock_sqlite3 = create_mock_sqlite3()

    with (
        patch("ctypes.cdll.LoadLibrary", side_effect=OSError("Shared library missing")),
        patch.dict("sys.modules", {"sqlcipher3._sqlite3": None, "_sqlite3": mock_sqlite3}),
    ):
        spec.loader.exec_module(module)
        assert module.apilevel == "2.0"
        assert module.paramstyle == "qmark"
        assert module.version_info == (2, 6, 0)
        assert module.sqlite_version_info == (3, 45, 0)


def test_dbapi2_raises_descriptive_importerror_on_missing_dependencies():
    """Verify that missing SQLCipher library dependencies raise a descriptive ImportError."""
    spec = importlib.util.spec_from_file_location(
        "linux_dbapi2_error",
        "/app/sortify/app/binaries/linux/sqlcipher3/dbapi2.py",
    )
    module = importlib.util.module_from_spec(spec)

    with (
        patch("ctypes.cdll.LoadLibrary", side_effect=OSError("libsqlcipher.so.0: cannot open shared object file")),
        patch.dict("sys.modules", {"sqlcipher3._sqlite3": None, "_sqlite3": None}),
    ):
        with pytest.raises(ImportError, match="Failed to load SQLCipher native extension or shared library dependencies"):
            spec.loader.exec_module(module)


def test_database_wrapper_setup_exception_propagation():
    """Verify exception propagation during database connection wrapper setup when SQLCipher fails to load."""
    from app.core.user_space_bootstrap import verify_sqlcipher_encryption

    mock_dbapi2 = MagicMock()
    mock_dbapi2.connect.side_effect = ImportError("Failed to load SQLCipher native extension")
    mock_sqlcipher = MagicMock()
    mock_sqlcipher.dbapi2 = mock_dbapi2

    with (
        patch("sys.platform", "linux"),
        patch.dict("sys.modules", {"sqlcipher3": mock_sqlcipher, "sqlcipher3.dbapi2": mock_dbapi2}),
    ):
        result = verify_sqlcipher_encryption()
        assert result is False


def test_load_sqlcipher_native_libraries_success():
    """Verify _load_sqlcipher_native_libraries behavior when ctypes LoadLibrary succeeds."""
    spec = importlib.util.spec_from_file_location(
        "linux_dbapi2_success",
        "/app/sortify/app/binaries/linux/sqlcipher3/dbapi2.py",
    )
    dbapi2 = importlib.util.module_from_spec(spec)

    mock_sqlite3 = create_mock_sqlite3()
    mock_load = MagicMock()

    with (
        patch("ctypes.cdll.LoadLibrary", mock_load),
        patch.dict("sys.modules", {"sqlcipher3._sqlite3": mock_sqlite3}),
    ):
        spec.loader.exec_module(dbapi2)
        mock_load.reset_mock()
        loaded, errors = dbapi2._load_sqlcipher_native_libraries()
        assert loaded is True
        assert len(errors) == 0
        assert mock_load.call_count == 3
