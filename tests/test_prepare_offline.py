import io
import sys
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from scripts.prepare_offline import _load_required_project_packages, main

DUMMY_HASH = "sha256:" + ("a" * 64)


def _build_mock_requirements(
    *,
    omit_packages: set[str] | None = None,
    include_hashes: bool = True,
    torch_lines: list[str] | None = None,
) -> str:
    """Build a mocked requirements.txt covering base and ml packages from pyproject.toml."""
    omit = omit_packages or set()
    base_deps, ml_deps = _load_required_project_packages()
    all_pkgs = sorted((base_deps | ml_deps) - omit - {"torch"})

    hash_suffix = f" --hash={DUMMY_HASH}" if include_hashes else ""
    lines = [f"{pkg}==1.0.0{hash_suffix}" for pkg in all_pkgs]

    if "torch" not in omit:
        if torch_lines is not None:
            lines.extend(torch_lines)
        else:
            lines.extend(
                [
                    f"torch==2.15.0 ; sys_platform == 'darwin'{hash_suffix}",
                    f"torch==2.15.0+cpu ; sys_platform != 'darwin'{hash_suffix}",
                    f"torchvision==0.29.2 ; sys_platform == 'darwin'{hash_suffix}",
                    f"torchvision==0.29.2+cpu ; sys_platform != 'darwin'{hash_suffix}",
                ]
            )
    return "\n".join(lines) + "\n"


def _default_cpu_torch_wheel() -> MagicMock:
    mock_wheel = MagicMock()
    mock_wheel.name = (
        "torch-2.15.0+cpu-cp312-cp312-linux_x86_64.whl"
        if sys.platform in ("win32", "linux")
        else "torch-2.15.0-cp312-none-macosx_11_0_arm64.whl"
    )
    return mock_wheel


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_success(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py compiles --extra ml --universal and succeeds with CPU wheels."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements()

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file
                mock_glob.return_value = [_default_cpu_torch_wheel()]

                main()

                mock_exit.assert_not_called()
                mock_archive.assert_called_once_with(
                    "offline_bundle", "zip", "offline_bundle"
                )

                # Verify subprocess.run invocations (compile, venv, download)
                assert mock_run.call_count == 3

                # Verify uv pip compile uses --extra ml, --universal, --generate-hashes,
                # --index-strategy first-index, and --torch-backend cpu
                compile_cmd = mock_run.call_args_list[0][0][0]
                assert "--extra" in compile_cmd
                assert compile_cmd[compile_cmd.index("--extra") + 1] == "ml"
                assert "--universal" in compile_cmd
                assert "--generate-hashes" in compile_cmd
                assert "--index-strategy" in compile_cmd
                assert (
                    compile_cmd[compile_cmd.index("--index-strategy") + 1]
                    == "first-index"
                )
                assert "--torch-backend" in compile_cmd
                assert compile_cmd[compile_cmd.index("--torch-backend") + 1] == "cpu"

                # Verify pip download uses --require-hashes and --no-deps
                download_cmd = mock_run.call_args_list[2][0][0]
                assert "--require-hashes" in download_cmd
                assert "--no-deps" in download_cmd


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_frozen_skips_compile_and_succeeds(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that --frozen skips uv pip compile and validates tracked requirements.txt."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements()

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file
                mock_glob.return_value = [_default_cpu_torch_wheel()]

                main()

                mock_exit.assert_not_called()
                # Only uv venv --seed and pip download should run (no uv pip compile)
                assert mock_run.call_count == 2
                for call in mock_run.call_args_list:
                    assert "compile" not in call[0][0]


@pytest.mark.parametrize(
    "manifest_kwargs",
    [
        {"omit_packages": {"easyocr", "llama-cpp-python"}},
        {"include_hashes": False},
        {
            "torch_lines": [
                f"torch==2.15.0 ; sys_platform == 'emscripten' --hash={DUMMY_HASH}"
            ]
        },
    ],
    ids=["missing_ml_packages", "missing_hashes", "zero_active_torch_entries"],
)
@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_frozen_fails_on_invalid_manifest(
    mock_print,
    mock_mkdir,
    mock_archive,
    mock_rmtree,
    mock_run,
    manifest_kwargs,
):
    """Test that --cpu --frozen exits with code 1 on missing ml packages, missing hashes, or zero active torch entries."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements(**manifest_kwargs)

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file
                mock_glob.return_value = [_default_cpu_torch_wheel()]

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_fails_on_zero_torch_wheels(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that --cpu fails when zero torch wheels are present in offline_bundle/wheels."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements()

            non_torch_wheel = MagicMock()
            non_torch_wheel.name = "easyocr-1.7.2-py3-none-any.whl"

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob", return_value=[non_torch_wheel]),
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_fails_on_cuda_req(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py fails if standard or CUDA PyTorch is found in requirements."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements(
                torch_lines=[f"torch==2.1.2+cu121 --hash={DUMMY_HASH}"]
            )

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file
                mock_glob.return_value = []

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_fails_on_cuda_wheel(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py fails if a CUDA-enabled wheel is downloaded."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements()

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file

                # Non-CPU wheel name
                mock_wheel = MagicMock()
                mock_wheel.name = "torch-2.1.2+cu121-cp310-cp310-linux_x86_64.whl"
                mock_glob.return_value = [mock_wheel]

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("builtins.print")
def test_prepare_offline_downloads_pep517_build_wheels_for_sdist(
    mock_print, mock_archive, mock_rmtree, mock_run, tmp_path, monkeypatch
):
    """Test that PEP 517 build wheels are downloaded when an sdist like llama-cpp-python is present."""
    monkeypatch.chdir(tmp_path)
    bundle_dir = tmp_path / "offline_bundle"
    bundle_dir.mkdir()
    reqs_file = bundle_dir / "requirements.txt"
    reqs_file.write_text(_build_mock_requirements(), encoding="utf-8")

    def fake_run(cmd, *args, **kwargs):
        if "download" in cmd and "-r" in cmd:
            wheels_dir = bundle_dir / "wheels"
            sdist_path = wheels_dir / "llama_cpp_python-0.3.37.tar.gz"
            pyproject_bytes = (
                b'[build-system]\nrequires = ["scikit-build-core[pyproject]>=0.9.2"]\n'
                b'build-backend = "scikit_build_core.build"\n'
            )
            with tarfile.open(sdist_path, "w:gz") as tf:
                info = tarfile.TarInfo(name="llama_cpp_python-0.3.37/pyproject.toml")
                info.size = len(pyproject_bytes)
                tf.addfile(info, io.BytesIO(pyproject_bytes))

            torch_whl = wheels_dir / _default_cpu_torch_wheel().name
            torch_whl.write_bytes(b"wheel")

    mock_run.side_effect = fake_run

    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        main()

    # Calls: 1) uv venv --seed, 2) pip download -r, 3) pip download --only-binary=:all: for PEP 517 deps
    assert mock_run.call_count == 3
    pep517_cmd = mock_run.call_args_list[2][0][0]
    assert "--only-binary=:all:" in pep517_cmd
    assert "scikit-build-core[pyproject]>=0.9.2" in pep517_cmd
    assert "cmake>=3.15" in pep517_cmd
    assert "ninja>=1.5" in pep517_cmd


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_preserves_models(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py does not delete the model weight subdirectories."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements()

            # We want to check that Path("offline_bundle") is not removed.
            # Only Path("offline_bundle/wheels") should be removed if it exists.
            deleted_paths = []

            def fake_rmtree(path, *args, **kwargs):
                deleted_paths.append(str(path))

            mock_rmtree.side_effect = fake_rmtree

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob") as mock_glob,
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file
                mock_glob.return_value = [_default_cpu_torch_wheel()]

                main()

                mock_exit.assert_not_called()
                # Verify that offline_bundle directory itself was not deleted
                assert "offline_bundle" not in deleted_paths
                # Verify that previous ZIP output was checked and wheels dir specifically deleted
                assert "offline_bundle/wheels" in deleted_paths or any(
                    "wheels" in p for p in deleted_paths
                )
