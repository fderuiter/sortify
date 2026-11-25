"""Tests for `smart-autosorter model` CLI lifecycle subcommands (status, download, verify, remove)."""

import hashlib
import json
from unittest.mock import patch

import pytest

from app.cli.model_cli import handle_model_command
from app.config import AppSettings, get_app_dir
from app.core.downloader import (
    _VERIFICATION_CACHE,
    DownloadError,
    ModelVerificationError,
    inspect_local_model_bundle,
)
from app.core.shared_registry import SharedModelRegistry
from app.main import build_parser


def _populate_verified_bundle(model_dir):
    model_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "model.onnx": b"dummy-onnx-weights",
        "config.json": b'{"model_type": "bert"}',
        "tokenizer.json": b'{"version": "1.0"}',
        "tokenizer_config.json": b'{"do_lower_case": true}',
        "special_tokens_map.json": b'{"cls_token": "[CLS]"}',
        "vocab.txt": b"[PAD]\n[UNK]\n[CLS]\n[SEP]\n",
        "version.txt": b"1.0.0",
    }
    expected_hashes = {}
    for fname, data in payloads.items():
        (model_dir / fname).write_bytes(data)
        expected_hashes[fname] = hashlib.sha256(data).hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", expected_hashes
    )
    _VERIFICATION_CACHE.clear()
    return payloads, expected_hashes


def _invoke_model_cmd(args, settings) -> int:
    with pytest.raises(SystemExit) as exc_info:
        handle_model_command(args, settings)
    return int(exc_info.value.code or 0)


def test_model_cli_parser_subcommands():
    """Verify `smart-autosorter model` parses status, download, verify, and remove subcommands."""
    parser = build_parser()

    args_status = parser.parse_args(["model", "status", "--json"])
    assert args_status.subcommand == "model"
    assert args_status.model_command == "status"
    assert args_status.json is True

    args_dl = parser.parse_args(
        ["model", "download", "--proxy", "http://proxy.local:8080", "--force"]
    )
    assert args_dl.subcommand == "model"
    assert args_dl.model_command == "download"
    assert args_dl.proxy == "http://proxy.local:8080"
    assert args_dl.force is True

    args_verify = parser.parse_args(["model", "verify"])
    assert args_verify.model_command == "verify"

    args_rm = parser.parse_args(["model", "remove"])
    assert args_rm.model_command == "remove"


def test_model_status_and_verify_missing_vs_verified_vs_corrupt(tmp_path, capsys):
    """AC 8 / Req 12: `model status` and `model verify` report missing, verified, and hash-mismatched bundles."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = True
    parser = build_parser()

    # 1. Missing bundle: status exits 0 with bundle_status="missing", verify exits 1
    args_status = parser.parse_args(["model", "status", "--json"])
    assert _invoke_model_cmd(args_status, settings) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bundle_status"] == "missing"
    assert payload["verified"] is False
    assert payload["badge"] == "[AI: Fallback]"

    args_verify = parser.parse_args(["model", "verify", "--json"])
    assert _invoke_model_cmd(args_verify, settings) == 1
    verify_missing_err = json.loads(capsys.readouterr().out)
    assert verify_missing_err["status"] == "error"
    assert "missing" in verify_missing_err["message"].lower()

    # 2. Populate valid bundle: without onnxruntime -> [AI: Fallback], with onnxruntime -> [AI: Active]
    model_dir = get_app_dir() / "model"
    _populate_verified_bundle(model_dir)

    with patch("app.core.downloader.is_onnxruntime_available", return_value=False):
        assert _invoke_model_cmd(args_status, settings) == 0
        fallback_status = json.loads(capsys.readouterr().out)
        assert fallback_status["bundle_status"] == "valid"
        assert fallback_status["verified"] is True
        assert fallback_status["onnxruntime_available"] is False
        assert fallback_status["badge"] == "[AI: Fallback]"

    with patch("app.core.downloader.is_onnxruntime_available", return_value=True):
        assert _invoke_model_cmd(args_status, settings) == 0
        ok_status = json.loads(capsys.readouterr().out)
        assert ok_status["bundle_status"] == "valid"
        assert ok_status["verified"] is True
        assert ok_status["onnxruntime_available"] is True
        assert ok_status["badge"] == "[AI: Active]"

    assert _invoke_model_cmd(args_verify, settings) == 0
    ok_verify = json.loads(capsys.readouterr().out)
    assert ok_verify["verified"] is True

    # 3. Tamper with model.onnx -> SHA-256 verification fails with exit code 1
    (model_dir / "model.onnx").write_bytes(b"corrupted-tampered-weights")
    _VERIFICATION_CACHE.clear()
    assert _invoke_model_cmd(args_verify, settings) == 1
    corrupt_err = json.loads(capsys.readouterr().out)
    assert corrupt_err["status"] == "error"
    assert (
        "corrupted" in corrupt_err["message"].lower()
        or "sha-256" in corrupt_err["message"].lower()
    )


def test_model_status_human_output_onnx_without_torch_and_fallback_without_onnx(
    tmp_path, capsys, monkeypatch
):
    """AC 3 & AC 4: `model status` reports [AI: Fallback] without onnxruntime, and ONNX Runtime: Available + [AI: Active] with onnxruntime and no torch."""
    import importlib.machinery
    import importlib.util
    import sys

    from app.core.downloader import DownloadManager, get_ai_runtime_state

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = True
    parser = build_parser()

    model_dir = get_app_dir() / "model"
    _populate_verified_bundle(model_dir)
    args_human = parser.parse_args(["model", "status"])

    real_find_spec = importlib.util.find_spec

    # Case A: Neither onnxruntime nor torch installed -> [AI: Fallback], onnxruntime_available: False
    def _find_spec_no_onnx(name, package=None):
        if name in ("onnxruntime", "torch"):
            return None
        return real_find_spec(name, package)

    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", _find_spec_no_onnx)
    DownloadManager.reset_instance()

    rt_no_onnx = get_ai_runtime_state(settings, model_dir=str(model_dir))
    assert rt_no_onnx["state"] == "fallback"
    assert rt_no_onnx["badge"] == "[AI: Fallback]"
    assert rt_no_onnx["onnxruntime_available"] is False

    assert _invoke_model_cmd(args_human, settings) == 0
    out_no_onnx = capsys.readouterr().out
    assert "[AI: Fallback]" in out_no_onnx
    assert "Not installed — install smart-autosorter[ml]" in out_no_onnx

    # Case B: onnxruntime installed and NO torch -> ONNX Runtime: Available and [AI: Active]
    def _find_spec_onnx_only(name, package=None):
        if name == "onnxruntime":
            return importlib.machinery.ModuleSpec("onnxruntime", loader=None)
        if name == "torch":
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec_onnx_only)
    DownloadManager.reset_instance()

    rt_onnx_only = get_ai_runtime_state(settings, model_dir=str(model_dir))
    assert rt_onnx_only["state"] == "active"
    assert rt_onnx_only["badge"] == "[AI: Active]"
    assert rt_onnx_only["onnxruntime_available"] is True

    assert _invoke_model_cmd(args_human, settings) == 0
    out_onnx = capsys.readouterr().out
    assert "ONNX Runtime:      Available" in out_onnx
    assert "[AI: Active]" in out_onnx


def test_model_download_saves_proxy_and_handles_failures(tmp_path, capsys):
    """AC 8 / Req 12: `model download` persists --proxy, exits 0 on verified download, and exits 1 on network/hash error."""
    settings_path = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_path)
    parser = build_parser()

    args_dl = parser.parse_args(
        [
            "model",
            "download",
            "--proxy",
            "http://corp-proxy.internal:3128",
            "--json",
        ]
    )

    with patch(
        "app.cli.model_cli.provision_model_cli",
        return_value={
            "status": "verified",
            "verified": True,
            "downloaded": True,
            "model_dir": str(get_app_dir() / "model"),
            "detail": "Verified local AI model weights ready on disk.",
        },
    ) as mock_prov:
        assert _invoke_model_cmd(args_dl, settings) == 0
        mock_prov.assert_called_once()
        assert mock_prov.call_args.kwargs["proxy"] == "http://corp-proxy.internal:3128"

    out = json.loads(capsys.readouterr().out)
    assert out["verified"] is True
    assert (
        AppSettings(filepath=settings_path).PROXY == "http://corp-proxy.internal:3128"
    )

    # Network failure -> exit code 1
    with patch(
        "app.cli.model_cli.provision_model_cli",
        side_effect=DownloadError("Connection refused"),
    ):
        assert _invoke_model_cmd(args_dl, settings) == 1
    err_payload = json.loads(capsys.readouterr().out)
    assert err_payload["status"] == "error"
    assert "Connection refused" in err_payload["message"]

    # Hash mismatch -> exit code 1
    with patch(
        "app.cli.model_cli.provision_model_cli",
        side_effect=ModelVerificationError("SHA-256 digest mismatch"),
    ):
        assert _invoke_model_cmd(args_dl, settings) == 1
    hash_payload = json.loads(capsys.readouterr().out)
    assert hash_payload["status"] == "error"
    assert "SHA-256" in hash_payload["message"]


@pytest.mark.parametrize("subcmd", ["remove", "delete"])
def test_model_remove_deletes_local_weights_and_unloads_registry(
    tmp_path, capsys, subcmd
):
    """AC 8 / Req 12: `model remove` / `delete` removes local bundle files and unloads cached models."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    parser = build_parser()

    model_dir = get_app_dir() / "model"
    _populate_verified_bundle(model_dir)
    assert inspect_local_model_bundle(str(model_dir))["verified"] is True

    args_rm = parser.parse_args(["model", subcmd, "--json"])
    assert _invoke_model_cmd(args_rm, settings) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] is True
    assert not model_dir.exists()
    assert inspect_local_model_bundle(str(model_dir))["disk_bundle_status"] == "missing"
