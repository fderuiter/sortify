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
    # psutil and hf_xet link the Performance Data Helper, which ships with Windows
    assert is_system_dll("pdh.dll") is True
    assert is_system_dll("advapi32.dll") is True
    assert is_system_dll("api-ms-win-core-synch-l1-1-0.dll") is True
    assert is_system_dll("ext-ms-win-gdi-desktop-l1-1-0.dll") is True

    # MSVC and UCRT libraries must NOT be whitelisted as system DLLs
    assert is_system_dll("msvcp140.dll") is False
    assert is_system_dll("vcruntime140.dll") is False
    assert is_system_dll("vcruntime140_1.dll") is False
    assert is_system_dll("ucrtbase.dll") is False
    assert is_system_dll("api-ms-win-crt-runtime-l1-1-0.dll") is False
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


def test_build_script_runs_pe_verification_only_on_windows_strict(
    tmp_path: Path, monkeypatch
):
    """Verify scripts/build.py skips PE checks on non-Windows and calls verify_pe_dependencies(allow_empty=False) on Windows."""
    import sys

    from scripts.build import main as build_main

    dist_dir = tmp_path / "dist" / "smart-autosorter"
    dist_dir.mkdir(parents=True)
    (tmp_path / "LICENSE").write_text("GPL\n", encoding="utf-8")
    docs_dist = dist_dir / "docs"
    docs_dist.mkdir()
    for doc in (
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ):
        (docs_dist / doc).write_text(f"# {doc}\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    mock_pyi = MagicMock()
    mock_pyi_main = MagicMock()
    mock_pyi.__main__ = mock_pyi_main
    monkeypatch.setitem(sys.modules, "PyInstaller", mock_pyi)
    monkeypatch.setitem(sys.modules, "PyInstaller.__main__", mock_pyi_main)

    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = ["/mock/sqlcipher3"]

    # 1. Linux/macOS: verify_pe_dependencies is never invoked
    (dist_dir / "smart-autosorter").write_text("elf", encoding="utf-8")
    with (
        patch("sys.platform", "linux"),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch("scripts.verify_pe_dependencies.verify_pe_dependencies") as mock_pe_check,
    ):
        build_main(["--lite"])
        mock_pe_check.assert_not_called()

    # 2. Windows: verify_pe_dependencies is invoked once with allow_empty=False
    (dist_dir / "smart-autosorter.exe").write_bytes(b"MZ_stub")
    with (
        patch("sys.platform", "win32"),
        patch("importlib.util.find_spec", return_value=mock_spec),
        patch(
            "scripts.verify_pe_dependencies.verify_pe_dependencies",
            return_value=(True, []),
        ) as mock_pe_check,
    ):
        build_main(["--lite"])
        mock_pe_check.assert_called_once_with(
            Path("dist") / "smart-autosorter", allow_empty=False
        )
