import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.prepare_offline import (
    _load_required_project_packages,
    _validate_manifest,
    main,
)

DUMMY_HASH = "sha256:" + ("a" * 64)
REPO_ROOT = Path(__file__).resolve().parent.parent
TRACKED_OFFLINE_REQS = (
    (REPO_ROOT / "offline_bundle" / "requirements.txt").read_bytes().decode("utf-8")
)


def _build_mock_requirements(
    *,
    omit_packages: set[str] | None = None,
    include_hashes: bool = True,
    torch_lines: list[str] | None = None,
) -> str:
    """Build a mocked requirements.txt based on tracked uv.lock pins with optional test mutations."""
    omit = {p.lower().replace("_", "-") for p in (omit_packages or set())}
    logical_entries: list[str] = []
    cur: list[str] = []
    for raw_line in TRACKED_OFFLINE_REQS.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cont = raw_line.rstrip().endswith("\\")
        part = stripped[:-1].strip() if stripped.endswith("\\") else stripped
        if part:
            cur.append(part)
        if not cont and cur:
            logical_entries.append(" ".join(cur))
            cur = []
    if cur:
        logical_entries.append(" ".join(cur))

    out_lines: list[str] = []
    for entry in logical_entries:
        pkg_prefix = entry.split("==", 1)[0].split("[", 1)[0].strip().lower()
        pkg_norm = pkg_prefix.replace("_", "-").replace(".", "-")
        if pkg_norm in omit:
            continue
        if torch_lines is not None and pkg_norm in ("torch", "torchvision"):
            continue
        if not include_hashes:
            entry = entry.split(" --hash=", 1)[0].strip()
        out_lines.append(entry)

    if "torch" not in omit and torch_lines is not None:
        out_lines.extend(torch_lines)

    # Ensure all required pyproject packages are represented if TRACKED_OFFLINE_REQS was empty
    if not out_lines and not omit:
        base_deps, ml_deps = _load_required_project_packages()
        hash_suffix = f" --hash={DUMMY_HASH}" if include_hashes else ""
        out_lines = [
            f"{pkg}==1.0.0{hash_suffix}" for pkg in sorted(base_deps | ml_deps)
        ]

    return "\n".join(out_lines) + "\n"


def _default_cpu_torch_wheel() -> MagicMock:
    mock_wheel = MagicMock()
    mock_wheel.name = (
        "torch-2.15.0+cpu-cp312-cp312-linux_x86_64.whl"
        if sys.platform in ("win32", "linux")
        else "torch-2.15.0-cp312-none-macosx_11_0_arm64.whl"
    )
    mock_wheel.read_bytes.return_value = b"mock-torch-wheel"
    return mock_wheel


def _default_app_wheel(payload: bytes = b"mock-smart-autosorter-wheel") -> MagicMock:
    mock_wheel = MagicMock()
    mock_wheel.name = "smart_autosorter-0.1.0-py3-none-any.whl"
    mock_wheel.read_bytes.return_value = payload
    return mock_wheel


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_cpu_success(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that prepare_offline.py exports from uv.lock via uv export --frozen and succeeds with CPU wheels."""
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
                app_wheel = _default_app_wheel()
                mock_glob.return_value = [_default_cpu_torch_wheel(), app_wheel]

                main()

                mock_exit.assert_not_called()
                mock_archive.assert_called_once_with(
                    "offline_bundle", "zip", "offline_bundle"
                )

                # Verify subprocess.run invocations (export, venv, download, uv build)
                assert mock_run.call_count == 4

                # Verify uv export uses --frozen --format requirements.txt --extra ml --no-dev --no-emit-project
                export_cmd = mock_run.call_args_list[0][0][0]
                assert export_cmd[:2] == ["uv", "export"]
                assert "--frozen" in export_cmd
                assert "--format" in export_cmd
                assert (
                    export_cmd[export_cmd.index("--format") + 1] == "requirements.txt"
                )
                assert "--extra" in export_cmd
                assert export_cmd[export_cmd.index("--extra") + 1] == "ml"
                assert "--no-dev" in export_cmd
                assert "--no-emit-project" in export_cmd

                # Verify pip download uses --require-hashes and --no-deps
                download_cmd = mock_run.call_args_list[2][0][0]
                assert "--require-hashes" in download_cmd
                assert "--no-deps" in download_cmd

                # Verify uv build packages smart-autosorter wheel into offline_bundle/wheels
                build_cmd = mock_run.call_args_list[3][0][0]
                assert build_cmd == [
                    "uv",
                    "build",
                    "--wheel",
                    "--out-dir",
                    str(Path("offline_bundle") / "wheels"),
                ]

                expected_digest = hashlib.sha256(app_wheel.read_bytes()).hexdigest()
                mock_file.__enter__.return_value.write.assert_called_once_with(
                    f"smart-autosorter==0.1.0 --hash=sha256:{expected_digest}\n"
                )


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_frozen_skips_compile_and_succeeds(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """Test that --frozen skips uv export and validates tracked requirements.txt."""
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
                mock_glob.return_value = [
                    _default_cpu_torch_wheel(),
                    _default_app_wheel(),
                ]

                main()

                mock_exit.assert_not_called()
                # Only uv venv --seed, pip download, and uv build --wheel should run (no uv export / compile)
                assert mock_run.call_count == 3
                for call in mock_run.call_args_list:
                    assert "compile" not in call[0][0]
                    assert "export" not in call[0][0]
                assert mock_run.call_args_list[2][0][0][:3] == [
                    "uv",
                    "build",
                    "--wheel",
                ]


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
    initial_reqs = _build_mock_requirements()
    reqs_file.write_text(initial_reqs, encoding="utf-8")
    app_wheel_bytes = b"built-smart-autosorter-wheel"

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
        elif cmd[:3] == ["uv", "build", "--wheel"]:
            out_dir = Path(cmd[cmd.index("--out-dir") + 1])
            (out_dir / "smart_autosorter-0.1.0-py3-none-any.whl").write_bytes(
                app_wheel_bytes
            )

    mock_run.side_effect = fake_run

    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        main()

    # Calls: 1) uv venv --seed, 2) pip download -r, 3) pip download --only-binary=:all:, 4) uv build --wheel
    assert mock_run.call_count == 4
    pep517_cmd = mock_run.call_args_list[2][0][0]
    assert "--only-binary=:all:" in pep517_cmd
    assert "scikit-build-core[pyproject]>=0.9.2" in pep517_cmd
    assert "cmake>=3.15" in pep517_cmd
    assert "ninja>=1.5" in pep517_cmd

    expected_sha256 = hashlib.sha256(app_wheel_bytes).hexdigest()
    assert (bundle_dir / "app_wheel.txt").read_text(encoding="utf-8") == (
        f"smart-autosorter==0.1.0 --hash=sha256:{expected_sha256}\n"
    )
    assert reqs_file.read_text(encoding="utf-8") == initial_reqs


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
                mock_glob.return_value = [
                    _default_cpu_torch_wheel(),
                    _default_app_wheel(),
                ]

                main()

                mock_exit.assert_not_called()
                # Verify that offline_bundle directory itself was not deleted
                assert "offline_bundle" not in deleted_paths
                # Verify that previous ZIP output was checked and wheels dir specifically deleted
                assert "offline_bundle/wheels" in deleted_paths or any(
                    "wheels" in p for p in deleted_paths
                )


@patch("scripts.prepare_offline.subprocess.run")
def test_verify_lockfile_passes_on_synchronized_repository_manifest(
    mock_run, monkeypatch
):
    """--verify-lockfile validates tracked offline_bundle/requirements.txt against uv.lock without downloading wheels."""
    monkeypatch.chdir(REPO_ROOT)
    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--verify-lockfile"]):
        main()
    mock_run.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        "pin_drift",
        "hash_mismatch",
        "missing_locked_transitive",
        "unexpected_extra_package",
    ],
)
def test_validate_manifest_fails_closed_on_uv_lock_divergence(mutation: str):
    """_validate_manifest must exit(1) whenever offline_bundle/requirements.txt diverges from uv.lock."""
    base_manifest = _build_mock_requirements()
    uv_lock_path = REPO_ROOT / "uv.lock"

    if mutation == "pin_drift":
        mutated = re.sub(
            r"(?m)^attrs==[^\s]+",
            "attrs==1999.1.1",
            base_manifest,
            count=1,
        )
    elif mutation == "hash_mismatch":
        bogus_hash = "sha256:" + ("f" * 64)
        mutated = re.sub(
            r"--hash=sha256:[0-9a-f]{64}",
            f"--hash={bogus_hash}",
            base_manifest,
            count=1,
        )
    elif mutation == "missing_locked_transitive":
        mutated = _build_mock_requirements(omit_packages={"annotated-types"})
    else:
        mutated = base_manifest + f"pytest==9.99.0 --hash={DUMMY_HASH}\n"

    assert mutated != base_manifest
    with pytest.raises(SystemExit) as exc_info:
        _validate_manifest(mutated, is_cpu=True, uv_lock_path=uv_lock_path)
    assert exc_info.value.code == 1


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_fails_when_app_wheel_missing(
    mock_print, mock_mkdir, mock_archive, mock_rmtree, mock_run
):
    """prepare_offline.py must exit(1) before zipping if smart_autosorter-*.whl is absent."""
    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        with patch("sys.exit", side_effect=SystemExit) as mock_exit:
            mock_requirements = _build_mock_requirements()

            with (
                patch("pathlib.Path.exists", return_value=True),
                patch("builtins.open", create=True) as mock_open,
                patch("pathlib.Path.glob", return_value=[_default_cpu_torch_wheel()]),
            ):
                mock_file = MagicMock()
                mock_file.__enter__.return_value.read.return_value = mock_requirements
                mock_open.return_value = mock_file

                with pytest.raises(SystemExit):
                    main()

                mock_exit.assert_called_once_with(1)
                mock_archive.assert_not_called()
