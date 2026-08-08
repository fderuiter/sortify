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


def test_inject_bootstrap_paths():
    """Verify search paths are properly added to sys.path and OS PATH env variables."""
    bin_dir = get_bootstrap_bin_dir()
    sqlcipher3_path = bin_dir / "sqlcipher3"

    with (
        patch("pathlib.Path.exists", return_value=True),
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
    with open(linux_dir / "__init__.py", "w", encoding="utf-8") as f:
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
