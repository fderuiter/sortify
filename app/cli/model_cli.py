"""CLI subcommand handler for local AI model status, download, verification, and removal."""

import argparse
import json
import logging
import sys

from app.cli.error_emitter import emit_cli_error
from app.cli.settings_args import add_settings_source_args
from app.config import AppSettings
from app.core.downloader import (
    DEFAULT_MODEL_URL,
    get_ai_runtime_state,
    inspect_local_model_bundle,
    provision_model_cli,
    remove_local_model_bundle,
)
from app.core.exceptions import DownloaderError
from app.core.verifier import is_ml_available

logger = logging.getLogger(__name__)


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``model`` CLI subcommand group."""
    parser_model = subparsers.add_parser(
        "model",
        help="Inspect, download, verify, or remove local AI model weights",
    )
    parser_model.add_argument(
        "--json",
        action="store_true",
        help="Output response in structured JSON format",
    )
    add_settings_source_args(parser_model, suppress_default=True)
    parser_model.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        default=argparse.SUPPRESS,
        help="Suppress informational prints",
    )
    parser_model.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        default=argparse.SUPPRESS,
        help="Disable ANSI color formatting",
    )
    model_subparsers = parser_model.add_subparsers(
        dest="model_command", help="Model management subcommands"
    )

    def add_model_common(p: argparse.ArgumentParser) -> None:
        add_settings_source_args(p, suppress_default=True)
        p.add_argument(
            "--model-dir",
            type=str,
            default=None,
            help="Explicit local model directory path override",
        )
        p.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            dest="quiet",
            default=argparse.SUPPRESS,
            help="Suppress informational prints",
        )
        p.add_argument(
            "--no-color",
            action="store_true",
            dest="no_color",
            default=argparse.SUPPRESS,
            help="Disable ANSI color formatting",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="Output response in structured JSON format",
        )

    p_status = model_subparsers.add_parser(
        "status",
        help="Display AI consent state, local model bundle readiness, and verification status",
    )
    add_model_common(p_status)

    p_download = model_subparsers.add_parser(
        "download",
        help="Download and cryptographically verify local ONNX model weights",
    )
    add_model_common(p_download)
    p_download.add_argument(
        "--url",
        type=str,
        default=DEFAULT_MODEL_URL,
        help="HTTPS allowlisted model weights URL",
    )
    p_download.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="HTTP/HTTPS proxy URL for model download transfers",
    )
    p_download.add_argument(
        "--force",
        action="store_true",
        help="Re-download model weights even if verified files already exist",
    )

    p_verify = model_subparsers.add_parser(
        "verify",
        help="Verify SHA-256 hashes and integrity of local model bundle files",
    )
    add_model_common(p_verify)

    p_remove = model_subparsers.add_parser(
        "remove",
        aliases=["delete"],
        help="Unload in-memory AI models and delete local cached model files",
    )
    add_model_common(p_remove)


def _persist_setting_immediately(settings: AppSettings) -> None:
    """Persist settings immediately to disk if supported."""
    timer = getattr(settings, "_save_timer", None)
    if timer is not None:
        try:
            timer.cancel()
        except (AttributeError, RuntimeError) as exc:
            logger.debug("Could not cancel settings timer: %s", exc)
    if hasattr(settings, "_save"):
        settings._save()


def handle_model_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Execute ``model`` CLI subcommands and exit with appropriate status code."""
    action = getattr(args, "model_command", None) or "status"
    is_json = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))
    model_dir = getattr(args, "model_dir", None)

    if action == "status":
        runtime = get_ai_runtime_state(settings=settings, model_dir=model_dir)
        ml_ready = is_ml_available()
        payload = {
            "status": "ok",
            "ai_state": runtime["state"],
            "badge": runtime["badge"],
            "ai_consent_granted": getattr(settings, "AI_CONSENT_GRANTED", None),
            "verified": runtime["verified"],
            "bundle_status": runtime["bundle_status"],
            "detail": runtime["detail"],
            "model_dir": runtime["model_dir"],
            "size_bytes": runtime["size_bytes"],
            "onnxruntime_available": ml_ready,
            "proxy_configured": bool(getattr(settings, "PROXY", "")),
            "files": runtime["files"],
        }
        if is_json:
            print(json.dumps(payload, indent=2))
        elif not quiet:
            size_mb = runtime["size_bytes"] / (1024 * 1024)
            print(f"AI Runtime Status: {runtime['badge']} ({runtime['state']})")
            print(
                f"AI Consent Granted: {getattr(settings, 'AI_CONSENT_GRANTED', None)}"
            )
            print(f"Model Directory:   {runtime['model_dir']}")
            print(
                f"Bundle Integrity:  {runtime['bundle_status']} ({runtime['detail']})"
            )
            print(f"Size on Disk:      {size_mb:.2f} MB")
            print(
                f"ONNX Runtime:      {'Available' if ml_ready else 'Not installed (offline TF-IDF fallback)'}"
            )
        sys.exit(0)

    if action == "download":
        explicit_proxy = getattr(args, "proxy", None)
        if explicit_proxy is not None:
            settings.PROXY = explicit_proxy.strip()
            _persist_setting_immediately(settings)
        effective_proxy = (
            explicit_proxy.strip()
            if explicit_proxy is not None
            else str(getattr(settings, "PROXY", "") or "").strip()
        )
        url = str(getattr(args, "url", None) or DEFAULT_MODEL_URL)
        force = bool(getattr(args, "force", False))

        try:
            result = provision_model_cli(
                model_dir=model_dir,
                url=url,
                proxy=effective_proxy,
                force=force,
                quiet=quiet,
                stream=sys.stderr,
            )
        except DownloaderError as exc:
            emit_cli_error(
                f"Error: Model download failed: {exc}",
                json_mode=is_json,
                exit_code=1,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            emit_cli_error(
                f"Error: Unexpected model provisioning failure: {exc}",
                json_mode=is_json,
                exit_code=1,
            )

        if is_json:
            print(json.dumps(result, indent=2))
        sys.exit(0)

    if action == "verify":
        bundle = inspect_local_model_bundle(model_dir=model_dir)
        if not bundle["verified"]:
            emit_cli_error(
                f"Error: Model verification failed ({bundle['disk_bundle_status']}): {bundle['detail']}",
                json_mode=is_json,
                exit_code=1,
            )
        payload = {
            "status": "verified",
            "verified": True,
            "bundle_status": bundle["disk_bundle_status"],
            "detail": bundle["detail"],
            "model_dir": bundle["model_dir"],
            "size_bytes": bundle["size_bytes"],
        }
        if is_json:
            print(json.dumps(payload, indent=2))
        elif not quiet:
            print(
                f"Verified local AI model bundle at '{bundle['model_dir']}': {bundle['detail']}"
            )
        sys.exit(0)

    if action in ("remove", "delete"):
        removed = remove_local_model_bundle(model_dir=model_dir)
        payload = {
            "status": "removed",
            **removed,
        }
        if is_json:
            print(json.dumps(payload, indent=2))
        elif not quiet:
            freed_mb = removed["freed_bytes"] / (1024 * 1024)
            print(
                f"Removed local model files from '{removed['model_dir']}' ({freed_mb:.2f} MB freed)."
            )
        sys.exit(0)

    emit_cli_error(
        f"Error: Unknown model subcommand '{action}'.",
        json_mode=is_json,
        exit_code=2,
    )
    return True
