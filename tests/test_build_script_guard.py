import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure PyInstaller is mocked before importing main
mock_pyinstaller = MagicMock()
mock_pyinstaller_main = MagicMock()
mock_pyinstaller.__main__ = mock_pyinstaller_main
sys.modules["PyInstaller"] = mock_pyinstaller
sys.modules["PyInstaller.__main__"] = mock_pyinstaller_main

from scripts.build import main  # noqa: E402


def test_build_script_lite_no_checks():
    """Verify that a lite build skips the heavy ML checks entirely."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--lite"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
        ):
            # Setup sqlcipher3 mock so it passes
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            main()

            # Since lite is enabled, we should NOT have searched for 'torch'
            # Let's verify that 'torch' is not checked.
            checked_modules = [call.args[0] for call in mock_find_spec.call_args_list]
            assert "torch" not in checked_modules
            mock_exit.assert_not_called()
            mock_pyinstaller_main.run.assert_called_once_with(
                ["smart-autosorter.spec", "--noconfirm", "--clean"]
            )


def test_build_script_standard_all_present():
    """Verify that a standard build passes if all ML packages are present."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("scripts.build.download_and_prepare_weights") as mock_download,
            patch("scripts.build.verify_scientific_binaries") as mock_verify_sci,
        ):
            # All find_spec calls return a valid spec
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            main()

            # Let's verify that 'torch', 'scipy', and other ML libraries were checked
            checked_modules = [call.args[0] for call in mock_find_spec.call_args_list]
            assert "torch" in checked_modules
            assert "scipy" in checked_modules
            assert "sklearn" in checked_modules
            assert "llama_cpp" in checked_modules

            mock_exit.assert_not_called()
            mock_download.assert_called_once()
            mock_pyinstaller_main.run.assert_called_once_with(
                ["smart-autosorter.spec", "--noconfirm", "--clean"]
            )


def test_build_script_standard_missing_package():
    """Verify that a standard build fails and prints missing package if any ML package is missing."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("builtins.print") as mock_print,
        ):

            def mock_find(name):
                # Mock torch as missing, but others as present
                if name == "torch":
                    return None
                mock_spec = MagicMock()
                mock_spec.submodule_search_locations = ["/some/path"]
                return mock_spec

            mock_find_spec.side_effect = mock_find

            with pytest.raises(SystemExit):
                main()

            # It should have called sys.exit(1) due to missing PyTorch
            mock_exit.assert_called_once_with(1)
            mock_pyinstaller_main.run.assert_not_called()

            # Check if it printed the missing package name
            printed_messages = "".join(
                [call.args[0] for call in mock_print.call_args_list]
            )
            assert "PyTorch" in printed_messages


def test_build_script_cpu_profile_success():
    """Verify that a CPU build passes if PyTorch is indeed CPU-only."""
    import os

    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--cpu"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("scripts.build.download_and_prepare_weights") as mock_download,
            patch("os.path.exists", return_value=True),
            patch("scripts.build.bundle_license"),
            patch(
                "os.walk",
                return_value=[
                    ("dist/smart-autosorter", [], []),
                    ("dist/smart-autosorter/scipy", [], ["_sparsetools.so"]),
                ],
            ),  # No GPU binaries
        ):
            # Setup sqlcipher3 and torch mock
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            # Mock torch module
            mock_torch = MagicMock()
            if sys.platform in ("win32", "linux"):
                mock_torch.__version__ = "2.1.2+cpu"
            else:  # darwin
                mock_torch.__version__ = "2.1.2"
                mock_torch.cuda.is_available.return_value = False

            with patch.dict("sys.modules", {"torch": mock_torch}):
                main()

                # Verify that CPU_BUILD env var is set
                assert os.environ.get("CPU_BUILD") == "1"

                mock_exit.assert_not_called()
                mock_download.assert_called_once()
                mock_pyinstaller_main.run.assert_called_once_with(
                    ["smart-autosorter.spec", "--noconfirm", "--clean"]
                )


def test_build_script_cpu_profile_failure_cuda():
    """Verify that a CPU build fails if PyTorch is CUDA-enabled."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--cpu"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("builtins.print") as mock_print,
        ):
            # Setup sqlcipher3 and torch mock
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            # Mock torch module
            mock_torch = MagicMock()
            if sys.platform in ("win32", "linux"):
                mock_torch.__version__ = "2.1.2+cu121"
            else:  # darwin
                mock_torch.__version__ = "2.1.2"
                mock_torch.cuda.is_available.return_value = True

            with patch.dict("sys.modules", {"torch": mock_torch}):
                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)
                # Check printed error messages
                printed_messages = "".join(
                    [call.args[0] for call in mock_print.call_args_list if call.args]
                )
                assert "Non-CPU PyTorch detected" in printed_messages


def test_build_script_cpu_profile_scan_fails_if_gpu_binaries_exist():
    """Verify that post-build scan fails if GPU binaries are found."""
    mock_pyinstaller_main.run.reset_mock()
    with patch("sys.argv", ["build.py", "--cpu"]):
        with (
            patch("importlib.util.find_spec") as mock_find_spec,
            patch("sys.exit", side_effect=SystemExit) as mock_exit,
            patch("scripts.build.download_and_prepare_weights") as mock_download,
            patch("os.path.exists", return_value=True),
            patch("scripts.build.bundle_license"),
            patch(
                "os.walk",
                return_value=[
                    ("dist/smart-autosorter", [], ["libcudart.so"]),
                    ("dist/smart-autosorter/scipy", [], ["_sparsetools.so"]),
                ],
            ),
            patch("builtins.print") as mock_print,
        ):
            # Setup sqlcipher3 and torch mock
            mock_spec = MagicMock()
            mock_spec.submodule_search_locations = ["/some/path"]
            mock_find_spec.return_value = mock_spec

            # Mock torch module
            mock_torch = MagicMock()
            if sys.platform in ("win32", "linux"):
                mock_torch.__version__ = "2.1.2+cpu"
            else:  # darwin
                mock_torch.__version__ = "2.1.2"
                mock_torch.cuda.is_available.return_value = False

            with patch.dict("sys.modules", {"torch": mock_torch}):
                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)
                printed_messages = "".join(
                    [call.args[0] for call in mock_print.call_args_list if call.args]
                )
                assert (
                    "Standalone bundle contains GPU/CUDA/cuDNN binaries"
                    in printed_messages
                )


def test_build_script_target_verification_missing_executable(tmp_path):
    """Verify that build fails if dist directory exists but is missing target executable."""
    import os

    dist_dir = tmp_path / "dist" / "smart-autosorter"
    dist_dir.mkdir(parents=True, exist_ok=True)
    # dist_dir exists but smart-autosorter is missing
    exe_ext = ".exe" if sys.platform == "win32" else ""

    original_cwd = os.getcwd()
    os.chdir(tmp_path)

    mock_pyinstaller_main.run.reset_mock()
    try:
        with patch("sys.argv", ["build.py", "--lite"]):
            with (
                patch("importlib.util.find_spec") as mock_find_spec,
                patch("sys.exit", side_effect=SystemExit) as mock_exit,
                patch("builtins.print") as mock_print,
            ):
                mock_spec = MagicMock()
                mock_spec.submodule_search_locations = ["/some/path"]
                mock_find_spec.return_value = mock_spec

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)
                printed_messages = "".join(
                    [call.args[0] for call in mock_print.call_args_list if call.args]
                )
                assert "Missing expected executable target" in printed_messages
                assert f"smart-autosorter{exe_ext}" in printed_messages
    finally:
        os.chdir(original_cwd)


def test_build_script_target_verification_success(tmp_path):
    """Verify that build passes when executable target exists."""
    import os

    dist_dir = tmp_path / "dist" / "smart-autosorter"
    dist_dir.mkdir(parents=True, exist_ok=True)
    exe_ext = ".exe" if sys.platform == "win32" else ""
    (dist_dir / f"smart-autosorter{exe_ext}").write_text("mock_cli")
    (tmp_path / "LICENSE").write_text("GNU GENERAL PUBLIC LICENSE\n")

    # Populate mock documentation assets in the bundle dist directory
    docs_dist = dist_dir / "docs"
    docs_dist.mkdir(parents=True, exist_ok=True)
    for doc in (
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ):
        (docs_dist / doc).write_text(f"# {doc}\n")
    diagrams_dist = docs_dist / "assets" / "diagrams"
    diagrams_dist.mkdir(parents=True, exist_ok=True)
    (diagrams_dist / "user_guide_setup_wizard.svg").write_text("<svg></svg>")

    original_cwd = os.getcwd()
    os.chdir(tmp_path)

    mock_pyinstaller_main.run.reset_mock()
    try:
        with patch("sys.argv", ["build.py", "--lite"]):
            with (
                patch("importlib.util.find_spec") as mock_find_spec,
                patch("sys.exit", side_effect=SystemExit) as mock_exit,
                patch("builtins.print") as mock_print,
            ):
                mock_spec = MagicMock()
                mock_spec.submodule_search_locations = ["/some/path"]
                mock_find_spec.return_value = mock_spec

                main()

                mock_exit.assert_not_called()
                printed_messages = "".join(
                    [call.args[0] for call in mock_print.call_args_list if call.args]
                )
                assert "Verification passed" in printed_messages
                assert not (dist_dir / "smart-autosorter.desktop").exists()
                assert (dist_dir / "LICENSE").exists()
    finally:
        os.chdir(original_cwd)


def test_package_sidecar_models(tmp_path):
    """Verify that package_sidecar_models packages offline_bundle files into smart-autosorter-models.zip."""
    import os
    import zipfile

    from scripts.build import package_sidecar_models

    offline_bundle = tmp_path / "offline_bundle"
    model_dir = offline_bundle / "model"
    easyocr_dir = offline_bundle / "easyocr"
    model_dir.mkdir(parents=True)
    easyocr_dir.mkdir(parents=True)

    (model_dir / "model.onnx").write_text("onnx_data")
    (easyocr_dir / "english_g2.pth").write_text("pth_data")

    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        package_sidecar_models()

        zip_path = tmp_path / "dist" / "smart-autosorter-models.zip"
        root_zip = tmp_path / "smart-autosorter-models.zip"
        assert zip_path.exists()
        assert root_zip.exists()

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            assert (
                "model/model.onnx" in names
                or "model/model.onnx".replace("/", os.sep) in names
            )
            assert (
                "easyocr/english_g2.pth" in names
                or "easyocr/english_g2.pth".replace("/", os.sep) in names
            )
    finally:
        os.chdir(original_cwd)


def test_verify_scientific_binaries_success(tmp_path):
    """Verify verify_scientific_binaries returns True when SciPy package and binaries exist."""
    from scripts.build import verify_scientific_binaries

    dist_dir = tmp_path / "dist" / "smart-autosorter"
    scipy_dir = dist_dir / "scipy" / "sparse"
    scipy_dir.mkdir(parents=True, exist_ok=True)
    (scipy_dir / "_sparsetools.so").write_bytes(b"mock_so_data")

    res = verify_scientific_binaries(str(dist_dir))
    assert res is True


def test_verify_scientific_binaries_missing_dir(tmp_path):
    """Verify verify_scientific_binaries fails if SciPy directory is missing."""
    from scripts.build import verify_scientific_binaries

    dist_dir = tmp_path / "dist" / "smart-autosorter"
    dist_dir.mkdir(parents=True, exist_ok=True)

    with patch("sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            verify_scientific_binaries(str(dist_dir))
        mock_exit.assert_called_once_with(1)


def test_verify_scientific_binaries_missing_binaries(tmp_path):
    """Verify verify_scientific_binaries fails if SciPy directory exists but contains no native C-extensions."""
    from scripts.build import verify_scientific_binaries

    dist_dir = tmp_path / "dist" / "smart-autosorter"
    scipy_dir = dist_dir / "scipy" / "sparse"
    scipy_dir.mkdir(parents=True, exist_ok=True)
    (scipy_dir / "__init__.py").write_text("print('hello')", encoding="utf-8")

    with patch("sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            verify_scientific_binaries(str(dist_dir))
        mock_exit.assert_called_once_with(1)


def test_bundle_license_copies_license_into_bundle(tmp_path, monkeypatch):
    from scripts.build import bundle_license

    (tmp_path / "LICENSE").write_text("GNU GENERAL PUBLIC LICENSE\n", encoding="utf-8")
    bundle = tmp_path / "dist" / "smart-autosorter"
    bundle.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    bundle_license(str(bundle))

    assert (bundle / "LICENSE").read_text(encoding="utf-8").startswith("GNU GENERAL")


def test_bundle_license_fails_without_license(tmp_path, monkeypatch):
    from scripts.build import bundle_license

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit):
        bundle_license(str(bundle))
    assert not (bundle / "LICENSE").exists()


def test_verify_documentation_assets_success(tmp_path, monkeypatch):
    """Verify that verify_documentation_assets passes when all required docs and diagrams exist."""
    from scripts.build import verify_documentation_assets

    bundle = tmp_path / "dist" / "smart-autosorter"
    docs_dist = bundle / "docs"
    docs_dist.mkdir(parents=True, exist_ok=True)
    for doc in (
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ):
        (docs_dist / doc).write_text(f"# {doc}\n")

    # Create source diagrams and corresponding bundled diagrams
    src_diagrams = tmp_path / "docs" / "assets" / "diagrams"
    src_diagrams.mkdir(parents=True, exist_ok=True)
    (src_diagrams / "test_diagram.svg").write_text("<svg></svg>")

    dist_diagrams = docs_dist / "assets" / "diagrams"
    dist_diagrams.mkdir(parents=True, exist_ok=True)
    (dist_diagrams / "test_diagram.svg").write_text("<svg></svg>")

    monkeypatch.chdir(tmp_path)
    assert verify_documentation_assets(str(bundle)) is True


def test_verify_documentation_assets_internal_docs_success(tmp_path, monkeypatch):
    """Verify that verify_documentation_assets passes when docs are placed in _internal/docs."""
    from scripts.build import verify_documentation_assets

    bundle = tmp_path / "dist" / "smart-autosorter"
    docs_dist = bundle / "_internal" / "docs"
    docs_dist.mkdir(parents=True, exist_ok=True)
    for doc in (
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ):
        (docs_dist / doc).write_text(f"# {doc}\n")

    # Create source diagrams and corresponding bundled diagrams
    src_diagrams = tmp_path / "docs" / "assets" / "diagrams"
    src_diagrams.mkdir(parents=True, exist_ok=True)
    (src_diagrams / "test_diagram.svg").write_text("<svg></svg>")

    dist_diagrams = docs_dist / "assets" / "diagrams"
    dist_diagrams.mkdir(parents=True, exist_ok=True)
    (dist_diagrams / "test_diagram.svg").write_text("<svg></svg>")

    monkeypatch.chdir(tmp_path)
    assert verify_documentation_assets(str(bundle)) is True


def test_verify_documentation_assets_missing_docs_dir(tmp_path, monkeypatch):
    """Verify that verify_documentation_assets exits with code 1 if docs directory is missing."""
    from scripts.build import verify_documentation_assets

    bundle = tmp_path / "dist" / "smart-autosorter"
    bundle.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)

    with patch("sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            verify_documentation_assets(str(bundle))
        mock_exit.assert_called_once_with(1)


def test_verify_documentation_assets_missing_required_md(tmp_path, monkeypatch):
    """Verify that verify_documentation_assets exits with code 1 if a required markdown guide is missing."""
    from scripts.build import verify_documentation_assets

    bundle = tmp_path / "dist" / "smart-autosorter"
    docs_dist = bundle / "docs"
    docs_dist.mkdir(parents=True, exist_ok=True)
    # Only populate some of the required docs
    for doc in ("user_guide.md", "admin_guide.md"):
        (docs_dist / doc).write_text(f"# {doc}\n")

    monkeypatch.chdir(tmp_path)

    with patch("sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            verify_documentation_assets(str(bundle))
        mock_exit.assert_called_once_with(1)


def test_verify_documentation_assets_missing_diagrams(tmp_path, monkeypatch):
    """Verify that verify_documentation_assets exits with code 1 if source diagrams exist but missing in bundle."""
    from scripts.build import verify_documentation_assets

    bundle = tmp_path / "dist" / "smart-autosorter"
    docs_dist = bundle / "docs"
    docs_dist.mkdir(parents=True, exist_ok=True)
    for doc in (
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ):
        (docs_dist / doc).write_text(f"# {doc}\n")

    # Source has a diagram
    src_diagrams = tmp_path / "docs" / "assets" / "diagrams"
    src_diagrams.mkdir(parents=True, exist_ok=True)
    (src_diagrams / "required_flow.svg").write_text("<svg></svg>")

    monkeypatch.chdir(tmp_path)

    with patch("sys.exit", side_effect=SystemExit) as mock_exit:
        with pytest.raises(SystemExit):
            verify_documentation_assets(str(bundle))
        mock_exit.assert_called_once_with(1)


def test_verify_documentation_assets_empty_diagrams_graceful(tmp_path, monkeypatch):
    """Verify that verify_documentation_assets gracefully succeeds when source diagram directory is empty."""
    from scripts.build import verify_documentation_assets

    bundle = tmp_path / "dist" / "smart-autosorter"
    docs_dist = bundle / "docs"
    docs_dist.mkdir(parents=True, exist_ok=True)
    for doc in (
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ):
        (docs_dist / doc).write_text(f"# {doc}\n")

    # Source has empty diagram dir
    src_diagrams = tmp_path / "docs" / "assets" / "diagrams"
    src_diagrams.mkdir(parents=True, exist_ok=True)

    monkeypatch.chdir(tmp_path)
    assert verify_documentation_assets(str(bundle)) is True


def test_download_file_hash_verification_success(tmp_path):
    """Verify download_file succeeds when downloaded content matches expected_sha256."""
    import hashlib
    import io

    from scripts.build import download_file

    payload = b"trusted content payload"
    expected_hash = hashlib.sha256(payload).hexdigest()
    dest_path = tmp_path / "model" / "verified.bin"

    mock_response = MagicMock()
    mock_response.__enter__.return_value = io.BytesIO(payload)
    mock_response.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=mock_response):
        download_file(
            "https://example.com/verified.bin",
            dest_path,
            expected_sha256=expected_hash,
        )

    assert dest_path.exists()
    assert dest_path.read_bytes() == payload


def test_download_file_hash_mismatch_cleans_up_and_raises(tmp_path):
    """Verify download_file removes partial/downloaded file and raises ValueError on hash mismatch."""
    import io

    from scripts.build import download_file

    payload = b"corrupted or tampered content"
    dest_path = tmp_path / "model" / "tampered.bin"

    mock_response = MagicMock()
    mock_response.__enter__.return_value = io.BytesIO(payload)
    mock_response.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=mock_response):
        with patch("time.sleep"):
            with pytest.raises(ValueError, match="Hash mismatch"):
                download_file(
                    "https://example.com/tampered.bin",
                    dest_path,
                    max_retries=2,
                    expected_sha256="0000000000000000000000000000000000000000000000000000000000000000",
                )

    # Ensure the file was cleaned up and does not remain on disk
    assert not dest_path.exists()


def test_safe_extract_archive_success(tmp_path):
    """Verify safe_extract_archive correctly extracts safe archive members."""
    import zipfile

    from scripts.build import safe_extract_archive

    zip_path = tmp_path / "safe.zip"
    extract_dir = tmp_path / "extracted"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("model.pth", b"safe model weights")
        zf.writestr("subdir/subfile.txt", b"sub content")

    safe_extract_archive(zip_path, extract_dir)

    assert (extract_dir / "model.pth").is_file()
    assert (extract_dir / "model.pth").read_bytes() == b"safe model weights"
    assert (extract_dir / "subdir" / "subfile.txt").is_file()


def test_safe_extract_archive_rejects_relative_traversal(tmp_path):
    """Verify safe_extract_archive rejects archives containing relative path traversal before extracting."""
    import zipfile

    from scripts.build import safe_extract_archive

    zip_path = tmp_path / "traversal.zip"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    outside_file = tmp_path / "escaped.txt"

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../escaped.txt", b"malicious content")

    with pytest.raises(ValueError, match="traversal|escapes"):
        safe_extract_archive(zip_path, extract_dir)

    assert not outside_file.exists()
    assert not list(extract_dir.iterdir())


def test_safe_extract_archive_rejects_subpath_traversal(tmp_path):
    """Verify safe_extract_archive rejects archives with parent navigation inside subpath."""
    import zipfile

    from scripts.build import safe_extract_archive

    zip_path = tmp_path / "sub_traversal.zip"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("sub/../../escaped.txt", b"malicious content")

    with pytest.raises(ValueError, match="traversal|escapes"):
        safe_extract_archive(zip_path, extract_dir)

    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_archive_rejects_absolute_path(tmp_path):
    """Verify safe_extract_archive rejects absolute path archive entries."""
    import zipfile

    from scripts.build import safe_extract_archive

    zip_path = tmp_path / "abs_path.zip"
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/evil_root.txt", b"malicious root content")

    with pytest.raises(ValueError, match="absolute|traversal"):
        safe_extract_archive(zip_path, extract_dir)


def test_download_and_prepare_weights_pinned_hashes_success(tmp_path, monkeypatch):
    """Verify download_and_prepare_weights succeeds and writes registry when all weights match pinned hashes."""
    from scripts.build import PINNED_HASHES, download_and_prepare_weights

    monkeypatch.chdir(tmp_path)

    model_dir = tmp_path / "offline_bundle" / "model"
    easyocr_dir = tmp_path / "offline_bundle" / "easyocr"
    model_dir.mkdir(parents=True)
    easyocr_dir.mkdir(parents=True)

    with patch("scripts.build.package_sidecar_models") as mock_package:
        with patch("scripts.build.resilient_file_hash") as mock_hash:

            def fake_hash(path):
                name = path.name
                if name in PINNED_HASHES["generative_naming"]:
                    return PINNED_HASHES["generative_naming"][name]
                if name in PINNED_HASHES["easyocr"]:
                    return PINNED_HASHES["easyocr"][name]
                return "unknown"

            mock_hash.side_effect = fake_hash

            for fname in PINNED_HASHES["generative_naming"]:
                (model_dir / fname).write_text("data")
            for fname in PINNED_HASHES["easyocr"]:
                (easyocr_dir / fname).write_text("data")

            download_and_prepare_weights()

            mock_package.assert_called_once()
            hashes_registry_path = tmp_path / "app" / "core" / "hashes_registry.py"
            assert hashes_registry_path.exists()
            content = hashes_registry_path.read_text(encoding="utf-8")
            assert "HASHES =" in content
            assert "florence-2" in content
            assert "model_download" in content


def test_download_and_prepare_weights_aborts_on_tampered_model(tmp_path, monkeypatch):
    """Verify download_and_prepare_weights raises ValueError and aborts if a file hash does not match pinned hash."""
    from scripts.build import PINNED_HASHES, download_and_prepare_weights

    monkeypatch.chdir(tmp_path)
    model_dir = tmp_path / "offline_bundle" / "model"
    easyocr_dir = tmp_path / "offline_bundle" / "easyocr"
    model_dir.mkdir(parents=True)
    easyocr_dir.mkdir(parents=True)

    for fname in PINNED_HASHES["generative_naming"]:
        (model_dir / fname).write_text("data")
    for fname in PINNED_HASHES["easyocr"]:
        (easyocr_dir / fname).write_text("data")

    with patch("scripts.build.package_sidecar_models"):
        with patch("scripts.build.resilient_file_hash") as mock_hash:

            def fake_hash(path):
                if path.name == "model.onnx":
                    return "corrupted_onnx_hash"
                name = path.name
                if name in PINNED_HASHES["generative_naming"]:
                    return PINNED_HASHES["generative_naming"][name]
                if name in PINNED_HASHES["easyocr"]:
                    return PINNED_HASHES["easyocr"][name]
                return "unknown"

            mock_hash.side_effect = fake_hash
            with pytest.raises(
                ValueError,
                match="Computed file hash does not match pinned registry hash",
            ):
                download_and_prepare_weights()


def test_download_and_prepare_weights_rejects_tampered_cached_easyocr(
    tmp_path, monkeypatch
):
    """Verify download_and_prepare_weights rejects a tampered cache copy of easyocr weights."""
    from scripts.build import PINNED_HASHES, download_and_prepare_weights

    monkeypatch.chdir(tmp_path)
    home_cache = tmp_path / ".EasyOCR" / "model"
    home_cache.mkdir(parents=True)
    corrupted_cached = home_cache / "craft_mlt_25k.pth"
    corrupted_cached.write_text("tampered cached weights")

    model_dir = tmp_path / "offline_bundle" / "model"
    model_dir.mkdir(parents=True)
    for fname in PINNED_HASHES["generative_naming"]:
        (model_dir / fname).write_text("data")

    with patch("pathlib.Path.home", return_value=tmp_path):
        with patch("scripts.build.resilient_file_hash") as mock_hash:

            def fake_hash(path):
                if path == corrupted_cached:
                    return "bad_cache_hash"
                name = path.name
                if name in PINNED_HASHES["generative_naming"]:
                    return PINNED_HASHES["generative_naming"][name]
                if name in PINNED_HASHES["easyocr"]:
                    return PINNED_HASHES["easyocr"][name]
                return "unknown"

            mock_hash.side_effect = fake_hash
            with pytest.raises(ValueError, match="Cached asset hash mismatch"):
                download_and_prepare_weights()


def test_download_and_prepare_weights_cleans_up_on_corrupted_extracted_archive(
    tmp_path, monkeypatch
):
    """Verify that if an extracted archive produces a weight with mismatched hash, it cleans up and raises."""
    import zipfile

    from scripts.build import PINNED_HASHES, download_and_prepare_weights

    monkeypatch.chdir(tmp_path)
    model_dir = tmp_path / "offline_bundle" / "model"
    easyocr_dir = tmp_path / "offline_bundle" / "easyocr"
    model_dir.mkdir(parents=True)
    easyocr_dir.mkdir(parents=True)

    for fname in PINNED_HASHES["generative_naming"]:
        (model_dir / fname).write_text("data")

    def fake_download_file(url, dest_path, **kwargs):
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.writestr("craft_mlt_25k.pth", b"corrupted extracted data")

    with patch("scripts.build.download_file", side_effect=fake_download_file):
        with patch("scripts.build.resilient_file_hash") as mock_hash:

            def fake_hash(path):
                if path.name == "craft_mlt_25k.pth":
                    return "bad_extracted_hash"
                name = path.name
                if name in PINNED_HASHES["generative_naming"]:
                    return PINNED_HASHES["generative_naming"][name]
                if name in PINNED_HASHES["easyocr"]:
                    return PINNED_HASHES["easyocr"][name]
                return "unknown"

            mock_hash.side_effect = fake_hash
            with pytest.raises(ValueError, match="Extracted archive hash mismatch"):
                download_and_prepare_weights()

            assert not (easyocr_dir / "craft_mlt_25k.pth").exists()
            assert not (easyocr_dir / "craft_mlt_25k.pth.zip").exists()
