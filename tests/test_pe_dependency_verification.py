"""Unit tests for PE binary dependency verification and dynamic CRT library discovery."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.verify_pe_dependencies import (
    is_system_dll,
    verify_pe_dependencies,
)


def test_is_system_dll_whitelist():
    """Verify system DLL whitelist matching logic."""
    assert is_system_dll("kernel32.dll") is True
    assert is_system_dll("KERNEL32.DLL") is True
    assert is_system_dll("user32.dll") is True
    assert is_system_dll("advapi32.dll") is True
    assert is_system_dll("api-ms-win-core-synch-l1-1-0.dll") is True
    assert is_system_dll("api-ms-win-crt-runtime-l1-1-0.dll") is True
    assert is_system_dll("ext-ms-win-gdi-desktop-l1-1-0.dll") is True

    # Physical MSVC and UCRT runtime DLLs must NOT be whitelisted as system DLLs
    assert is_system_dll("msvcp140.dll") is False
    assert is_system_dll("vcruntime140.dll") is False
    assert is_system_dll("vcruntime140_1.dll") is False
    assert is_system_dll("ucrtbase.dll") is False
    assert is_system_dll("sqlite3.dll") is False


def test_verify_pe_dependencies_missing_dir(tmp_path: Path):
    """Verify behavior when bundle directory does not exist."""
    non_existent = tmp_path / "does_not_exist"
    success, missing = verify_pe_dependencies(non_existent, allow_empty=True)
    assert success is True
    assert missing == []

    success, missing = verify_pe_dependencies(non_existent, allow_empty=False)
    assert success is False
    assert len(missing) == 1
    assert missing[0][0] == "BUNDLE_DIR"


def test_verify_pe_dependencies_empty_dir(tmp_path: Path):
    """Verify behavior when bundle directory contains no PE files."""
    empty_bundle = tmp_path / "bundle"
    empty_bundle.mkdir()

    success, missing = verify_pe_dependencies(empty_bundle, allow_empty=True)
    assert success is True
    assert missing == []

    success, missing = verify_pe_dependencies(empty_bundle, allow_empty=False)
    assert success is False
    assert len(missing) == 1


def test_verify_pe_dependencies_satisfied(tmp_path: Path):
    """Verify PE dependency check when all dependencies are satisfied."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    # Create dummy exe and bundled DLL
    exe_file = bundle_dir / "app.exe"
    exe_file.write_bytes(b"MZ_dummy")
    bundled_dll = bundle_dir / "custom_runtime.dll"
    bundled_dll.write_bytes(b"MZ_dummy")

    mock_pe = MagicMock()
    mock_entry1 = MagicMock()
    mock_entry1.dll = b"kernel32.dll"
    mock_entry2 = MagicMock()
    mock_entry2.dll = b"custom_runtime.dll"
    mock_pe.DIRECTORY_ENTRY_IMPORT = [mock_entry1, mock_entry2]

    with patch("pefile.PE", return_value=mock_pe):
        success, missing = verify_pe_dependencies(bundle_dir)
        assert success is True
        assert missing == []


def test_verify_pe_dependencies_flag_missing_crt(tmp_path: Path):
    """Verify PE dependency check flags missing MSVC/UCRT dynamic dependencies."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()

    exe_file = bundle_dir / "app.exe"
    exe_file.write_bytes(b"MZ_dummy")

    mock_pe = MagicMock()
    mock_entry1 = MagicMock()
    mock_entry1.dll = b"kernel32.dll"
    mock_entry2 = MagicMock()
    mock_entry2.dll = b"vcruntime140.dll"
    mock_entry3 = MagicMock()
    mock_entry3.dll = b"msvcp140.dll"
    mock_pe.DIRECTORY_ENTRY_IMPORT = [mock_entry1, mock_entry2, mock_entry3]

    with patch("pefile.PE", return_value=mock_pe):
        success, missing = verify_pe_dependencies(bundle_dir)
        assert success is False
        assert len(missing) == 2
        missing_dlls = {m[1] for m in missing}
        assert "vcruntime140.dll" in missing_dlls
        assert "msvcp140.dll" in missing_dlls
