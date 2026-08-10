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

    with (
        patch("sys.platform", "linux"),
        patch.dict("sys.modules", {"sqlcipher3": mock_sqlcipher}),
    ):
        assert verify_sqlcipher_encryption() is False


def test_inject_bootstrap_paths(tmp_path):
    """Verify search paths are properly added to sys.path and OS PATH env variables."""
    bin_dir = tmp_path / "binaries"
    bin_dir.mkdir()
    sqlcipher3_path = bin_dir / "sqlcipher3"
    sqlcipher3_path.mkdir()

    real_path = os.environ.get("PATH", "")

    with (
        patch(
            "app.core.user_space_bootstrap.get_bootstrap_bin_dir", return_value=bin_dir
        ),
        patch.object(sys, "path", sys.path.copy()) as mock_sys_path,
        patch("os.add_dll_directory", create=True) as mock_add_dll,
        patch.dict("os.environ", {"PATH": "existing_path" + os.pathsep + real_path}),
    ):
        inject_bootstrap_paths()
        assert str(bin_dir) in mock_sys_path
        if sys.platform == "win32":
            mock_add_dll.assert_any_call(str(bin_dir))
            mock_add_dll.assert_any_call(str(sqlcipher3_path))
            assert str(bin_dir) in os.environ["PATH"]


def test_bootstrap_binaries_bypass_if_cached(tmp_path):
    """Verify that if binaries are cached and verified, we bypass all download and fallback steps."""

    mock_app_dir = tmp_path / ".autosorter"
    mock_sqlcipher = mock_app_dir / "binaries" / "sqlcipher3"
    mock_sqlcipher.mkdir(parents=True)

    with (
        patch("sys.platform", "linux"),
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


def test_bootstrap_binaries_manifest_missing(tmp_path):
    """Verify that if the binaries manifest is missing, startup is rejected."""
    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    with (
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
    ):
        with pytest.raises(
            RuntimeError,
            match="Startup validation failed: local binaries manifest is missing",
        ):
            bootstrap_binaries(force_download=True)


def test_bootstrap_binaries_file_missing(tmp_path):
    """Verify that if any of the platform files are missing, startup is rejected."""
    import json

    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    # Create manifest
    manifest = {"linux": {"sqlcipher3/_sqlite3.so": "somehash"}}
    with open(mock_binaries_root / "manifest.json", "w") as f:
        json.dump(manifest, f)

    # Note that platform_binaries_dir ("linux") is created, but the required file is missing
    (mock_binaries_root / "linux").mkdir()

    with (
        patch("sys.platform", "linux"),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
    ):
        with pytest.raises(
            RuntimeError,
            match="Startup validation failed: local packaged binary file .* is missing",
        ):
            bootstrap_binaries(force_download=True)


def test_bootstrap_binaries_file_modified(tmp_path):
    """Verify that if a packaged file is modified, startup is rejected."""
    import json

    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    # Create manifest with incorrect hash
    manifest = {"linux": {"sqlcipher3/_sqlite3.so": "incorrect_hash_value"}}
    with open(mock_binaries_root / "manifest.json", "w") as f:
        json.dump(manifest, f)

    linux_dir = mock_binaries_root / "linux" / "sqlcipher3"
    linux_dir.mkdir(parents=True)
    with open(linux_dir / "_sqlite3.so", "w") as f:
        f.write("unmodified file content")

    with (
        patch("sys.platform", "linux"),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
    ):
        with pytest.raises(
            RuntimeError,
            match="Startup validation failed: local packaged binary file .* has been modified",
        ):
            bootstrap_binaries(force_download=True)


def test_bootstrap_binaries_successful_offline_flow(tmp_path):
    """Verify the complete offline bootstrap validation and path injection flow on success."""
    import hashlib
    import json

    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    content = b"valid precompiled dynamic library content"
    expected_hash = hashlib.sha256(content).hexdigest()

    manifest = {"linux": {"sqlcipher3/_sqlite3.so": expected_hash}}
    with open(mock_binaries_root / "manifest.json", "w") as f:
        json.dump(manifest, f)

    linux_dir = mock_binaries_root / "linux" / "sqlcipher3"
    linux_dir.mkdir(parents=True)
    with open(linux_dir / "_sqlite3.so", "wb") as f:
        f.write(content)

    with (
        patch("sys.platform", "linux"),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths") as mock_inject,
    ):
        res = bootstrap_binaries(force_download=True)
        assert res is True
        mock_inject.assert_called_once_with(mock_binaries_root / "linux")


def test_bootstrap_binaries_failed_verification(tmp_path):
    """Verify that if encryption verification fails after bootstrapping, we raise a RuntimeError."""
    import hashlib
    import json

    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    content = b"valid content"
    expected_hash = hashlib.sha256(content).hexdigest()

    manifest = {"linux": {"sqlcipher3/_sqlite3.so": expected_hash}}
    with open(mock_binaries_root / "manifest.json", "w") as f:
        json.dump(manifest, f)

    linux_dir = mock_binaries_root / "linux" / "sqlcipher3"
    linux_dir.mkdir(parents=True)
    with open(linux_dir / "_sqlite3.so", "wb") as f:
        f.write(content)

    with (
        patch("sys.platform", "linux"),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=False,
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths"),
    ):
        with pytest.raises(RuntimeError, match="Startup verification failed"):
            bootstrap_binaries(force_download=True)


def test_bootstrap_binaries_newline_and_bom_insensitivity(tmp_path):
    """Verify that text files with CRLF and/or BOM are correctly matched against LF-only/no-BOM hashes."""
    import hashlib
    import json

    mock_binaries_root = tmp_path / "binaries"
    mock_binaries_root.mkdir()

    # The original text with LF endings and no BOM
    original_text = "print('hello world')\n"
    expected_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()

    manifest = {"linux": {"sqlcipher3/__init__.py": expected_hash}}
    with open(mock_binaries_root / "manifest.json", "w") as f:
        json.dump(manifest, f)

    linux_dir = mock_binaries_root / "linux" / "sqlcipher3"
    linux_dir.mkdir(parents=True)

    # Write on-disk file with BOM and CRLF line endings
    crlf_text_with_bom = "\ufeffprint('hello world')\r\n"
    with open(linux_dir / "__init__.py", "w", encoding="utf-8", newline="") as f:
        f.write(crlf_text_with_bom)

    with (
        patch("sys.platform", "linux"),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths") as mock_inject,
    ):
        res = bootstrap_binaries(force_download=True)
        assert res is True
        mock_inject.assert_called_once_with(mock_binaries_root / "linux")


def test_bootstrap_binaries_pyinstaller_internal_fallback(tmp_path):
    """Verify that if sys._MEIPASS is defined, we correctly resolve files inside _internal if present."""
    import hashlib
    import json

    mock_meipass = tmp_path / "meipass"
    mock_meipass.mkdir()

    mock_binaries_root = mock_meipass / "_internal" / "app" / "binaries"
    mock_binaries_root.mkdir(parents=True)

    content = b"pyd content"
    expected_hash = hashlib.sha256(content).hexdigest()

    manifest = {"linux": {"sqlcipher3/_sqlite3.so": expected_hash}}
    with open(mock_binaries_root / "manifest.json", "w") as f:
        json.dump(manifest, f)

    linux_dir = mock_binaries_root / "linux" / "sqlcipher3"
    linux_dir.mkdir(parents=True)
    with open(linux_dir / "_sqlite3.so", "wb") as f:
        f.write(content)

    with (
        patch("sys.platform", "linux"),
        patch("sys.frozen", True, create=True),
        patch("sys._MEIPASS", str(mock_meipass), create=True),
        patch(
            "app.core.user_space_bootstrap.__file__",
            str(tmp_path / "core" / "user_space_bootstrap.py"),
        ),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
        patch("app.core.user_space_bootstrap.inject_bootstrap_paths") as mock_inject,
    ):
        res = bootstrap_binaries(force_download=True)
        assert res is True
        mock_inject.assert_called_once_with(mock_binaries_root / "linux")


def test_sqlite3_mock_fallback_on_import_error():
    """Verify that when standard sqlite3 and sqlcipher3 both raise ImportError, a functional dummy module is registered."""
    import importlib
    import sys
    from unittest.mock import patch

    import app.core.db_conn

    # Remove standard modules from sys.modules temporarily to simulate clean import environment
    original_sqlite3 = sys.modules.get("sqlite3")
    original_sqlcipher3 = sys.modules.get("sqlcipher3")

    sys.modules.pop("sqlite3", None)
    sys.modules.pop("sqlcipher3", None)

    real_import = __import__

    def mock_import(name, *args, **kwargs):
        if (
            name in ("sqlite3", "sqlcipher3")
            or name.startswith("sqlite3.")
            or name.startswith("sqlcipher3.")
        ):
            raise ImportError("Simulated import failure")
        return real_import(name, *args, **kwargs)

    try:
        with patch("builtins.__import__", side_effect=mock_import):
            # Reload db_conn which triggers the sqlite3 mapping logic
            # Since it raises ImportError, it should execute the custom fallback mapping
            importlib.reload(app.core.db_conn)

        import sqlite3

        assert sqlite3 is not None
        assert hasattr(sqlite3, "connect")
        assert hasattr(sqlite3, "Error")

        with pytest.raises(RuntimeError, match="SQLCipher library is missing"):
            sqlite3.connect(":memory:")
    finally:
        # Restore original modules to prevent test pollution
        if original_sqlite3:
            sys.modules["sqlite3"] = original_sqlite3
        else:
            sys.modules.pop("sqlite3", None)
        if original_sqlcipher3:
            sys.modules["sqlcipher3"] = original_sqlcipher3
        else:
            sys.modules.pop("sqlcipher3", None)

        # Re-reload db_conn to restore its state for other tests!
        importlib.reload(app.core.db_conn)


def test_bootstrap_binaries_windows_direct_import_fallback():
    """Verify that on Windows, if not frozen, bootstrap_binaries discovers and injects DLL search paths before verification."""
    mock_add_dll = MagicMock()
    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = ["C:\\site-packages\\sqlcipher3"]

    expected_mock_dirs = {
        os.path.abspath("C:\\site-packages\\sqlcipher3").lower().replace("\\", "/"),
        os.path.abspath("C:\\venv").lower().replace("\\", "/"),
        os.path.abspath("C:\\venv\\Library\\bin").lower().replace("\\", "/"),
        os.path.abspath("C:\\venv\\Scripts").lower().replace("\\", "/"),
        os.path.abspath("C:\\venv\\DLLs").lower().replace("\\", "/"),
        os.path.abspath("C:\\venv\\Lib\\site-packages\\sqlcipher3")
        .lower()
        .replace("\\", "/"),
        os.path.abspath("C:\\Program Files\\OpenSSL-Win64\\bin")
        .lower()
        .replace("\\", "/"),
        os.path.abspath("C:\\Program Files\\OpenSSL\\bin").lower().replace("\\", "/"),
        os.path.abspath("C:\\Program Files\\OpenSSL-Win64").lower().replace("\\", "/"),
        os.path.abspath("C:\\Program Files\\OpenSSL").lower().replace("\\", "/"),
        os.path.abspath("C:\\OpenSSL-Win64\\bin").lower().replace("\\", "/"),
        os.path.abspath("C:\\OpenSSL-Win64").lower().replace("\\", "/"),
        os.path.abspath("C:\\Program Files\\Common Files\\SSL")
        .lower()
        .replace("\\", "/"),
    }

    real_isdir = os.path.isdir

    def mock_isdir(path):
        try:
            p_abs = os.path.abspath(str(path)).lower().replace("\\", "/")
            if p_abs in expected_mock_dirs:
                return True
        except Exception:
            pass
        try:
            return real_isdir(path)
        except Exception:
            return False

    env_mock = os.environ.copy()
    env_mock["VIRTUAL_ENV"] = "C:\\venv"

    with (
        patch("sys.platform", "win32"),
        patch("sys.prefix", "C:\\venv"),
        patch("os.add_dll_directory", mock_add_dll, create=True),
        patch("os.path.isdir", side_effect=mock_isdir),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch.dict("os.environ", env_mock),
        patch(
            "app.core.user_space_bootstrap.verify_sqlcipher_encryption",
            return_value=True,
        ),
    ):
        res = bootstrap_binaries(force_download=False)
        assert res is True
        # Check that os.add_dll_directory was called on the sqlcipher3 package directory and venv dirs
        calls_lower = [str(c[0][0]).lower().replace("\\", "/") for c in mock_add_dll.call_args_list]
        assert "c:/site-packages/sqlcipher3" in calls_lower
        assert os.path.abspath("C:\\venv").lower().replace("\\", "/") in calls_lower
