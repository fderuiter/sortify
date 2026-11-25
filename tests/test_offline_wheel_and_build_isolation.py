"""Dedicated tests for offline app wheel bundling, venv isolation, and deterministic hash registries."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.install_offline import (
    _extract_and_install_offline,
    safe_extract_zip,
)
from scripts.install_offline import (
    main as install_offline_main,
)
from scripts.offline_bundle_utils import (
    build_and_pin_app_wheel,
    cleanup_stale_file,
    format_hashes_registry,
    read_existing_hashes,
    resolve_target_venv,
    sync_hashes_registry,
    venv_python_path,
)
from scripts.prepare_offline import (
    NORMALIZED_ARCHIVE_DATE_TIME,
    NORMALIZED_ARCHIVE_EPOCH,
)
from scripts.prepare_offline import (
    main as prepare_offline_main,
)
from tests.test_prepare_offline import (
    _build_mock_requirements,
    _default_cpu_torch_wheel,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_build_and_pin_app_wheel_writes_sha256_manifest(tmp_path: Path) -> None:
    """build_and_pin_app_wheel invokes uv build --wheel and records exact version + SHA-256."""
    wheels_dir = tmp_path / "offline_bundle" / "wheels"
    wheels_dir.mkdir(parents=True)
    manifest_path = tmp_path / "offline_bundle" / "app_wheel.txt"
    wheel_payload = b"deterministic-smart-autosorter-wheel-bytes"

    recorded_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], *, check: bool = False) -> None:
        assert check is True
        recorded_cmds.append(cmd)
        out_dir = Path(cmd[cmd.index("--out-dir") + 1])
        (out_dir / "smart_autosorter-0.1.0-py3-none-any.whl").write_bytes(wheel_payload)

    built_wheel = build_and_pin_app_wheel(wheels_dir, manifest_path, run_cmd=fake_run)
    assert built_wheel == wheels_dir / "smart_autosorter-0.1.0-py3-none-any.whl"
    assert recorded_cmds == [["uv", "build", "--wheel", "--out-dir", str(wheels_dir)]]

    expected_sha256 = hashlib.sha256(wheel_payload).hexdigest()
    assert manifest_path.read_text(encoding="utf-8") == (
        f"smart-autosorter==0.1.0 --hash=sha256:{expected_sha256}\n"
    )


@pytest.mark.parametrize(
    "wheel_filenames",
    [
        [],
        [
            "smart_autosorter-0.1.0-py3-none-any.whl",
            "smart_autosorter-0.2.0-py3-none-any.whl",
        ],
        ["smart_autosorter-bad.whl"],
    ],
    ids=["missing_wheel", "multiple_wheels", "malformed_wheel_filename"],
)
def test_build_and_pin_app_wheel_fails_closed_on_invalid_wheel_output(
    tmp_path: Path, wheel_filenames: list[str]
) -> None:
    """build_and_pin_app_wheel exits 1 when 0, >1, or malformed smart_autosorter wheels exist."""
    wheels_dir = tmp_path / "offline_bundle" / "wheels"
    wheels_dir.mkdir(parents=True)
    manifest_path = tmp_path / "offline_bundle" / "app_wheel.txt"

    def fake_run(_cmd: list[str], *, check: bool = False) -> None:
        assert check is True
        for name in wheel_filenames:
            (wheels_dir / name).write_bytes(b"payload")

    with pytest.raises(SystemExit) as exc_info:
        build_and_pin_app_wheel(wheels_dir, manifest_path, run_cmd=fake_run)
    assert exc_info.value.code == 1
    assert not manifest_path.exists()


def test_cleanup_stale_file_handles_oserror_gracefully(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cleanup_stale_file unlinks existing files and logs a warning without raising on OSError."""
    stale = tmp_path / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    cleanup_stale_file(stale)
    assert not stale.exists()

    locked = tmp_path / "locked.txt"
    locked.write_text("locked", encoding="utf-8")
    with patch.object(Path, "unlink", side_effect=OSError("busy")):
        cleanup_stale_file(locked)
    assert "Warning: could not delete" in capsys.readouterr().out


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("scripts.prepare_offline.shutil.make_archive")
@patch("scripts.prepare_offline.Path.mkdir")
@patch("builtins.print")
def test_prepare_offline_fails_before_zipping_when_app_wheel_missing(
    mock_print: MagicMock,
    mock_mkdir: MagicMock,
    mock_archive: MagicMock,
    mock_rmtree: MagicMock,
    mock_run: MagicMock,
) -> None:
    """prepare_offline.py exits 1 before zipping if smart_autosorter-*.whl is absent."""
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
                    prepare_offline_main()

                mock_exit.assert_called_once_with(1)
                mock_archive.assert_not_called()


@patch("scripts.prepare_offline.subprocess.run")
@patch("scripts.prepare_offline.shutil.rmtree")
@patch("builtins.print")
def test_prepare_offline_normalizes_app_wheel_and_manifest_timestamps_preserving_hash(
    mock_print: MagicMock,
    mock_rmtree: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end archive creation normalizes app_wheel.txt and wheel timestamps while preserving SHA-256."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(
        (REPO_ROOT / "pyproject.toml").read_bytes()
    )
    (tmp_path / "uv.lock").write_bytes((REPO_ROOT / "uv.lock").read_bytes())

    bundle_dir = tmp_path / "offline_bundle"
    bundle_dir.mkdir()
    reqs_file = bundle_dir / "requirements.txt"
    initial_reqs = _build_mock_requirements()
    reqs_file.write_text(initial_reqs, encoding="utf-8")

    # Seed a stale app_wheel.txt to confirm cleanup + regeneration
    stale_manifest = bundle_dir / "app_wheel.txt"
    stale_manifest.write_text("stale-pin\n", encoding="utf-8")

    app_wheel_bytes = b"reproducible-app-wheel-payload"

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> None:
        if "download" in cmd and "-r" in cmd:
            wheels_dir = bundle_dir / "wheels"
            (wheels_dir / _default_cpu_torch_wheel().name).write_bytes(b"torch-bytes")
        elif cmd[:3] == ["uv", "build", "--wheel"]:
            out_dir = Path(cmd[cmd.index("--out-dir") + 1])
            (out_dir / "smart_autosorter-0.1.0-py3-none-any.whl").write_bytes(
                app_wheel_bytes
            )

    mock_run.side_effect = fake_run

    with patch("sys.argv", ["prepare_offline.py", "--cpu", "--frozen"]):
        prepare_offline_main()

    assert reqs_file.read_text(encoding="utf-8") == initial_reqs
    zip_path = tmp_path / "offline_bundle.zip"
    assert zip_path.is_file()

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        assert "app_wheel.txt" in names
        assert "wheels/smart_autosorter-0.1.0-py3-none-any.whl" in names
        for info in zf.infolist():
            assert info.date_time == NORMALIZED_ARCHIVE_DATE_TIME

    extracted = tmp_path / "airgap" / "offline_bundle"
    safe_extract_zip(zip_path, extracted)
    extracted_whl = extracted / "wheels" / "smart_autosorter-0.1.0-py3-none-any.whl"
    extracted_pin = (extracted / "app_wheel.txt").read_text(encoding="utf-8")
    expected_sha256 = hashlib.sha256(app_wheel_bytes).hexdigest()

    assert int(extracted_whl.stat().st_mtime) == NORMALIZED_ARCHIVE_EPOCH
    assert hashlib.sha256(extracted_whl.read_bytes()).hexdigest() == expected_sha256
    assert extracted_pin == f"smart-autosorter==0.1.0 --hash=sha256:{expected_sha256}\n"


def test_resolve_target_venv_precedence_and_checkout_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify --venv > UV_PROJECT_ENVIRONMENT > non-checkout VIRTUAL_ENV > .venv-offline in checkout vs .venv on clean host."""
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()
    (checkout_dir / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n", encoding="utf-8"
    )
    (checkout_dir / ".venv").mkdir()

    monkeypatch.chdir(checkout_dir)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    assert resolve_target_venv() == Path(".venv-offline")
    assert resolve_target_venv("   ") == Path(".venv-offline")

    # Implicit VIRTUAL_ENV=<checkout>/.venv set by `uv run --no-project` must NOT clobber .venv
    monkeypatch.setenv("VIRTUAL_ENV", str(checkout_dir / ".venv"))
    assert resolve_target_venv() == Path(".venv-offline")

    # Explicit custom VIRTUAL_ENV is honored
    custom_venv = tmp_path / "custom-venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(custom_venv))
    assert resolve_target_venv() == custom_venv

    # UV_PROJECT_ENVIRONMENT overrides VIRTUAL_ENV
    uv_proj_venv = tmp_path / "uv-proj-venv"
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(uv_proj_venv))
    assert resolve_target_venv() == uv_proj_venv

    # --venv overrides UV_PROJECT_ENVIRONMENT and VIRTUAL_ENV
    cli_venv = tmp_path / "cli-venv"
    assert resolve_target_venv(cli_venv) == cli_venv

    # Git-only checkout (.git present without pyproject.toml) also isolates into .venv-offline
    git_only_dir = tmp_path / "git-checkout"
    (git_only_dir / ".git").mkdir(parents=True)
    monkeypatch.chdir(git_only_dir)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert resolve_target_venv() == Path(".venv-offline")

    # Clean air-gapped host without .git or pyproject.toml defaults to .venv, even if a
    # contributor checkout .venv was still exported in VIRTUAL_ENV from an outer shell.
    airgap_dir = tmp_path / "airgap"
    airgap_dir.mkdir()
    monkeypatch.chdir(airgap_dir)
    monkeypatch.setenv("VIRTUAL_ENV", str(checkout_dir / ".venv"))
    assert resolve_target_venv() == Path(".venv")

    # Clean air-gapped host with its own activated .venv honors that .venv
    airgap_venv = airgap_dir / ".venv"
    airgap_venv.mkdir()
    monkeypatch.setenv("VIRTUAL_ENV", str(airgap_venv))
    assert resolve_target_venv() == airgap_venv

    assert venv_python_path(
        Path(".venv"), platform_name="linux", os_name="posix"
    ) == Path(".venv/bin/python")
    assert venv_python_path(Path(".venv"), platform_name="win32", os_name="nt") == Path(
        ".venv/Scripts/python.exe"
    )


@pytest.mark.parametrize(
    "cli_argv",
    [
        ["offline-install", "--venv", "/tmp/custom-offline-venv"],
        ["--venv", "/tmp/custom-offline-venv", "offline-install"],
    ],
)
@patch("scripts.install_offline._extract_and_install_offline")
@patch("scripts.install_offline.get_uv_cmd", return_value="uv")
def test_install_offline_main_forwards_venv_flag(
    mock_get_uv: MagicMock,
    mock_extract_install: MagicMock,
    cli_argv: list[str],
) -> None:
    """Verify --venv is accepted before or after offline-install and forwarded."""
    install_offline_main(cli_argv)
    mock_extract_install.assert_called_once_with(
        "uv", venv_dir="/tmp/custom-offline-venv"
    )


def test_extract_and_install_offline_clean_host_creates_venv_and_fails_without_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify clean air-gapped host fails closed when requirements.txt or app_wheel.txt is missing."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    bundle_dir = tmp_path / "offline_bundle"
    (bundle_dir / "wheels").mkdir(parents=True)

    # Missing requirements.txt must exit 1
    with pytest.raises(SystemExit) as exc_reqs:
        _extract_and_install_offline("uv")
    assert exc_reqs.value.code == 1

    (bundle_dir / "requirements.txt").write_text(
        "pkg==1.0.0 --hash=sha256:aa\n", encoding="utf-8"
    )

    # Missing app_wheel.txt must exit 1
    with pytest.raises(SystemExit) as exc_app:
        _extract_and_install_offline("uv")
    assert exc_app.value.code == 1

    # With both manifests present, creates .venv and passes platform --python
    (bundle_dir / "app_wheel.txt").write_text(
        "smart-autosorter==0.1.0 --hash=sha256:bb\n", encoding="utf-8"
    )
    with patch("scripts.install_offline.subprocess.run") as mock_run:
        resolved = _extract_and_install_offline("uv")
        assert resolved == Path(".venv")
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0] == ["uv", "venv", ".venv"]
        install_cmd = mock_run.call_args_list[1][0][0]
        assert install_cmd[install_cmd.index("--python") + 1] == str(
            venv_python_path(Path(".venv"))
        )


def test_hashes_registry_sync_formatting_idempotency_and_lite_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify canonical Ruff formatting, tracked registry parity, no-op skips, AnnAssign/corrupt recovery, and --lite stub."""
    from scripts.build import (
        PINNED_HASHES,
        download_and_prepare_weights,
    )
    from scripts.build import (
        main as build_main,
    )

    # 1. Tracked app/core/hashes_registry.py matches canonical PINNED_HASHES byte-for-byte
    tracked_registry = (REPO_ROOT / "app" / "core" / "hashes_registry.py").read_text(
        encoding="utf-8"
    )
    assert tracked_registry == format_hashes_registry(PINNED_HASHES)
    assert '"model.gguf":' in tracked_registry

    # 2. AnnAssign and corrupt/non-dict existing registries parse/recover cleanly
    ann_registry = tmp_path / "ann" / "hashes_registry.py"
    ann_registry.parent.mkdir(parents=True)
    ann_registry.write_text(
        'HASHES: dict[str, dict[str, str]] = {"easyocr": {"a.pth": "00"}}\n',
        encoding="utf-8",
    )
    assert read_existing_hashes(ann_registry) == {"easyocr": {"a.pth": "00"}}

    corrupt_registry = tmp_path / "corrupt" / "hashes_registry.py"
    corrupt_registry.parent.mkdir(parents=True)
    corrupt_registry.write_text("HASHES = ['not', 'a', 'dict']\n", encoding="utf-8")
    assert read_existing_hashes(corrupt_registry) is None
    assert sync_hashes_registry(corrupt_registry, PINNED_HASHES) is True
    assert sync_hashes_registry(corrupt_registry, PINNED_HASHES) is False

    # 3. download_and_prepare_weights writes sorted Ruff-formatted hashes and skips rewrite when unchanged
    monkeypatch.chdir(tmp_path)
    model_dir = tmp_path / "offline_bundle" / "model"
    easyocr_dir = tmp_path / "offline_bundle" / "easyocr"
    model_dir.mkdir(parents=True)
    easyocr_dir.mkdir(parents=True)

    for fname in reversed(list(PINNED_HASHES["model_download"])):
        (model_dir / fname).write_text("data", encoding="utf-8")
    for fname in reversed(list(PINNED_HASHES["easyocr"])):
        (easyocr_dir / fname).write_text("data", encoding="utf-8")

    def fake_hash(path: Path) -> str:
        if path.name in PINNED_HASHES["model_download"]:
            return PINNED_HASHES["model_download"][path.name]
        if path.name in PINNED_HASHES["easyocr"]:
            return PINNED_HASHES["easyocr"][path.name]
        return "unknown"

    with (
        patch("scripts.build.package_sidecar_models"),
        patch("scripts.build.resilient_file_hash", side_effect=fake_hash),
    ):
        download_and_prepare_weights()

        hashes_registry_path = tmp_path / "app" / "core" / "hashes_registry.py"
        assert hashes_registry_path.read_text(encoding="utf-8") == tracked_registry

        fmt_res = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "--check",
                str(hashes_registry_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert fmt_res.returncode == 0, fmt_res.stderr or fmt_res.stdout

        real_open = open
        write_targets: list[str] = []

        def spy_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if "w" in mode or "a" in mode:
                write_targets.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=spy_open):
            download_and_prepare_weights()
        assert not any("hashes_registry.py" in t for t in write_targets)

    # 4. --lite writes a Ruff-formatted HASHES = {} stub when app/core/hashes_registry.py is missing
    lite_root = tmp_path / "lite_workspace"
    lite_root.mkdir()
    monkeypatch.chdir(lite_root)

    fake_pyinstaller_mod = MagicMock()
    fake_pyinstaller_main = MagicMock()
    fake_pyinstaller_mod.__main__ = fake_pyinstaller_main

    mock_spec = MagicMock()
    mock_spec.submodule_search_locations = ["/some/path"]

    with (
        patch.dict(
            sys.modules,
            {
                "PyInstaller": fake_pyinstaller_mod,
                "PyInstaller.__main__": fake_pyinstaller_main,
            },
        ),
        patch("importlib.util.find_spec", return_value=mock_spec),
    ):
        build_main(["--lite"])

    stub_path = lite_root / "app" / "core" / "hashes_registry.py"
    assert stub_path.is_file()
    assert stub_path.read_text(encoding="utf-8") == format_hashes_registry({})
    stub_fmt = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(stub_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert stub_fmt.returncode == 0, stub_fmt.stderr or stub_fmt.stdout
