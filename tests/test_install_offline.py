import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
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


def test_offline_requirements_manifest_specifies_hashes():
    """Verify that all packages in offline_bundle/requirements.txt specify at least one cryptographic hash."""
    repo_root = Path(__file__).resolve().parent.parent
    req_file = repo_root / "offline_bundle" / "requirements.txt"
    assert req_file.is_file(), f"Requirements file not found at {req_file}"

    lines = req_file.read_text(encoding="utf-8").splitlines()
    entries = []
    current_entry = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current_entry.append(stripped.rstrip("\\").strip())
        if not raw_line.rstrip().endswith("\\"):
            entries.append(" ".join(current_entry))
            current_entry = []

    if current_entry:
        entries.append(" ".join(current_entry))

    assert len(entries) > 0, "offline_bundle/requirements.txt must not be empty"

    for entry in entries:
        parts = entry.split()
        package_spec = parts[0]
        hashes = [p for p in parts if p.startswith("--hash=")]
        assert len(hashes) >= 1, (
            f"Package '{package_spec}' does not specify at least one cryptographic hash: {entry}"
        )
        for h in hashes:
            assert h.startswith("--hash=sha256:"), (
                f"Expected sha256 hash for package '{package_spec}', found: {h}"
            )


def test_offline_requirements_manifest_covers_base_and_ml_packages():
    """Verify that offline_bundle/requirements.txt pins all base and ml packages and CPU torch."""
    from scripts.prepare_offline import (
        _load_required_project_packages,
        _parse_manifest_entries,
    )

    repo_root = Path(__file__).resolve().parent.parent
    req_file = repo_root / "offline_bundle" / "requirements.txt"
    pyproject_file = repo_root / "pyproject.toml"

    base_deps, ml_deps = _load_required_project_packages(pyproject_file)
    assert {
        "easyocr",
        "transformers",
        "onnxruntime",
        "llama-cpp-python",
        "torch",
    }.issubset(ml_deps)

    entries = _parse_manifest_entries(req_file.read_text(encoding="utf-8"))
    pinned_names = {str(item["name"]) for item in entries}
    active_names = {str(item["name"]) for item in entries if item["active"]}

    missing_base = base_deps - active_names
    missing_ml = ml_deps - active_names
    assert not missing_base, f"Missing base dependencies in manifest: {missing_base}"
    assert not missing_ml, f"Missing ml dependencies in manifest: {missing_ml}"

    # Verify CPU PyTorch is pinned and no CUDA/NVIDIA packages appear
    torch_specs = [str(item["spec"]) for item in entries if item["name"] == "torch"]
    assert any("+cpu" in spec for spec in torch_specs), (
        f"Expected CPU PyTorch (+cpu) entry in manifest, found: {torch_specs}"
    )
    for name in pinned_names:
        assert not name.startswith(("nvidia-", "cuda-")), (
            f"Unexpected GPU package '{name}' in offline_bundle/requirements.txt"
        )
    for item in entries:
        spec = str(item["spec"])
        assert "+cu" not in spec and "cuda" not in spec.lower(), (
            f"Unexpected CUDA build tag in offline_bundle/requirements.txt: {spec}"
        )


def test_offline_requirements_manifest_sympy_mpmath_compatibility():
    """Verify sympy and mpmath pins in offline_bundle/requirements.txt satisfy sympy's upper bound."""
    from packaging.version import Version

    from scripts.prepare_offline import _parse_manifest_entries

    repo_root = Path(__file__).resolve().parent.parent
    req_file = repo_root / "offline_bundle" / "requirements.txt"
    entries = _parse_manifest_entries(req_file.read_text(encoding="utf-8"))

    pinned_versions = {
        str(item["name"]): str(item["spec"]).split("==", 1)[1].split(";", 1)[0].strip()
        for item in entries
        if item["active"] and "==" in str(item["spec"])
    }
    if "sympy" in pinned_versions:
        assert "mpmath" in pinned_versions, (
            "sympy is present in offline_bundle/requirements.txt, so mpmath must also be pinned"
        )
        sympy_ver = Version(pinned_versions["sympy"].split("+", 1)[0])
        mpmath_ver = Version(pinned_versions["mpmath"].split("+", 1)[0])
        if sympy_ver <= Version("1.14.0"):
            assert Version("1.1.0") <= mpmath_ver < Version("1.4"), (
                f"sympy=={sympy_ver} requires mpmath>=1.1.0,<1.4, "
                f"found mpmath=={mpmath_ver} in offline_bundle/requirements.txt"
            )


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
