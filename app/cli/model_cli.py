"""CLI subcommand handler for local AI model status, download, verification, and removal."""

import argparse
import json
import logging
import sys

from app.cli.error_emitter import emit_cli_error
from app.cli.settings_args import build_global_subparser_parent
from app.config import AppSettings
from app.core.downloader import (
    DEFAULT_MODEL_URL,
    get_ai_runtime_state,
    inspect_local_model_bundle,
    provision_model_cli,
    remove_local_model_bundle,
)
from app.core.exceptions import DownloaderError

logger = logging.getLogger(__name__)

_DAEMON_IPC_CONTROL_ACTIONS: frozenset[str] = frozenset(
    {"status", "stop", "pause", "resume", "health", "metrics"}
)


def _flag_enabled(args: argparse.Namespace, attr_name: str) -> bool:
    """Return True only when an explicit boolean CLI flag attribute is True."""
    return getattr(args, attr_name, False) is True


def add_model_provisioning_args(
    parser: argparse.ArgumentParser,
    *,
    suppress_default: bool = False,
) -> None:
    """Register ``--defer-model-download`` on top-level or subcommand CLI parsers."""
    default_val: object = argparse.SUPPRESS if suppress_default else False
    parser.add_argument(
        "--defer-model-download",
        action="store_true",
        default=default_val,
        help="Skip automatic AI model download and use local keyword/TF-IDF fallback",
    )


def maybe_provision_model_for_cli_consent(
    args: argparse.Namespace, settings: AppSettings
) -> None:
    """Verify or download model weights on CLI ``--accept-ai-consent`` operational runs."""
    if not _flag_enabled(args, "accept_ai_consent"):
        return
    subcommand = getattr(args, "subcommand", None)
    if subcommand not in (None, "scan", "sort", "drop", "daemon"):
        return
    if subcommand == "daemon":
        daemon_action = str(getattr(args, "action", "") or "").strip().lower()
        if daemon_action in _DAEMON_IPC_CONTROL_ACTIONS:
            return
    if (
        _flag_enabled(args, "defer_model_download")
        or _flag_enabled(args, "tui")
        or _flag_enabled(args, "interactive")
    ):
        return
    if getattr(args, "_cli_consent_model_checked", False):
        return
    object.__setattr__(args, "_cli_consent_model_checked", True)

    from app.core.downloader import provision_model_cli as run_provision_model_cli

    quiet = bool(getattr(args, "quiet", False))
    proxy = str(getattr(settings, "PROXY", "") or "").strip()
    try:
        run_provision_model_cli(proxy=proxy, quiet=quiet, stream=sys.stderr)
    except DownloaderError as exc:
        logger.warning(
            "Could not download AI model weights (%s); proceeding with local keyword/TF-IDF fallback.",
            exc,
        )
        if not quiet:
            print(
                f"Warning: Could not download AI model weights ({exc}). "
                "Proceeding with local keyword/TF-IDF fallback. "
                "Retry anytime with 'smart-autosorter model download'.",
                file=sys.stderr,
            )
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        logger.warning(
            "Model provisioning check encountered an error (%s); proceeding with local keyword/TF-IDF fallback.",
            exc,
        )
        if not quiet:
            print(
                f"Warning: Model provisioning check encountered an error ({exc}). "
                "Proceeding with local keyword/TF-IDF fallback.",
                file=sys.stderr,
            )


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``model`` CLI subcommand group."""
    parser_model = subparsers.add_parser(
        "model",
        parents=[build_global_subparser_parent()],
        help="Inspect, download, verify, or remove local AI model weights",
    )
    model_subparsers = parser_model.add_subparsers(
        dest="model_command", help="Model management subcommands"
    )

    def add_model_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--model-dir",
            type=str,
            default=None,
            help="Explicit local model directory path override",
        )

    p_status = model_subparsers.add_parser(
        "status",
        parents=[build_global_subparser_parent()],
        help="Display AI consent state, local model bundle readiness, and verification status",
    )
    add_model_common(p_status)

    p_download = model_subparsers.add_parser(
        "download",
        parents=[build_global_subparser_parent()],
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
        parents=[build_global_subparser_parent()],
        help="Verify SHA-256 hashes and integrity of local model bundle files",
    )
    add_model_common(p_verify)

    p_remove = model_subparsers.add_parser(
        "remove",
        aliases=["delete"],
        parents=[build_global_subparser_parent()],
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
        onnx_ready = bool(runtime["onnxruntime_available"])
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
            "onnxruntime_available": onnx_ready,
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
            onnx_text = (
                "Available"
                if onnx_ready
                else "Not installed — install smart-autosorter[ml] (offline TF-IDF fallback)"
            )
            print(f"ONNX Runtime:      {onnx_text}")
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
