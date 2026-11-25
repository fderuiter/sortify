"""TUI AI runtime polling and safe mock-session auto-rescan helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

import app.core.downloader as _downloader
from app.core.path_utils import _is_plan_leaf_node, collect_empty_plan_folders
from app.core.shared_registry import SharedModelRegistry

logger = logging.getLogger(__name__)


def compute_ai_poll_token(host: Any) -> tuple[Any, ...]:
    """Build an in-memory token of consent, ONNX readiness, and DownloadManager state without disk stat calls."""
    consent = getattr(host.settings, "AI_CONSENT_GRANTED", None) is True
    onnx_ready = _downloader.is_onnxruntime_available()
    reg = SharedModelRegistry._instance
    onnx_health = (
        str(reg.get_model_health("onnx_embeddings").get("status", ""))
        if reg is not None
        else ""
    )
    dm = _downloader.DownloadManager._instance
    if dm is None:
        return (0, consent, onnx_ready, onnx_health, "IDLE", False, 0, "")
    is_downloading = bool(dm.state.get("is_downloading", False))
    raw_progress = float(dm.state.get("progress", 0.0) or 0.0)
    pct = int(round(min(100.0, max(0.0, raw_progress * 100.0))))
    err = str(dm.state.get("error") or "")
    return (
        id(dm),
        consent,
        onnx_ready,
        onnx_health,
        str(dm.status),
        is_downloading,
        pct,
        err,
    )


def _is_leaf_file_entry(node: dict[str, Any]) -> bool:
    """Return True when ``node`` represents a leaf file or directory-cleanup entry in a sorting plan."""
    if _is_plan_leaf_node(node):
        return node.get("__type__") == "file"
    if node.get("node_type") == "file":
        return True
    if node.get("node_type") == "directory":
        return False
    return any(
        key in node
        for key in (
            "relative_source",
            "target_filename",
            "source_path",
            "filepath",
            "original_path",
        )
    )


def plan_has_manual_modifications(host: Any) -> bool:
    """Return True if the current plan has manual edits, empty folders, locks, or manual exclusions."""
    if (
        bool(getattr(host, "_manual_plan_edits", None))
        or bool(getattr(host, "_manual_empty_folders", None))
        or bool(getattr(host, "locked_files", None))
    ):
        return True

    raw_plan = getattr(host, "plan", None)
    plan_tree = getattr(raw_plan, "plan_tree", None)
    if plan_tree is None:
        plan_tree = getattr(raw_plan, "nodes", None)
    if plan_tree is None:
        plan_tree = getattr(raw_plan, "plan", raw_plan)
    if not isinstance(plan_tree, dict):
        return False

    if collect_empty_plan_folders(plan_tree):
        return True

    def _has_node_override(tree: dict[str, Any]) -> bool:
        for name, node in tree.items():
            if str(name).startswith("__") or not isinstance(node, dict):
                continue
            if _is_leaf_file_entry(node):
                is_auto_skipped_duplicate = (
                    bool(node.get("is_duplicate"))
                    and node.get("duplicate_action") == "skip"
                    and bool(node.get("duplicate_resolved"))
                )
                has_manual_exclusion = (
                    bool(node.get("is_excluded")) or node.get("is_checked") is False
                ) and not is_auto_skipped_duplicate
                if (
                    bool(node.get("is_locked"))
                    or bool(node.get("locked"))
                    or bool(node.get("confirmed"))
                    or bool(node.get("destination_override"))
                    or bool(node.get("is_overridden"))
                    or bool(node.get("manual_edit"))
                    or bool(node.get("excluded"))
                    or has_manual_exclusion
                ):
                    return True
            elif not _is_plan_leaf_node(node) and _has_node_override(node):
                return True
        return False

    return _has_node_override(plan_tree)


def is_mock_or_degraded_session(host: Any) -> bool:
    """Return True when an existing scan session or plan used mock/TF-IDF fallback embeddings."""
    session = getattr(host, "app_session", None)
    plan = getattr(host, "plan", {})
    has_files = (
        bool(host._has_file_nodes(plan))
        if hasattr(host, "_has_file_nodes")
        else bool(plan)
    )
    if session is None and not has_files:
        return False
    if session is not None:
        if getattr(session, "is_fallback", False):
            return True
        analyzer = getattr(session, "analyzer", None)
        emb = getattr(analyzer, "embedding_manager", None)
        if emb is not None and (
            getattr(emb, "is_fallback", False) or getattr(emb, "is_degraded", False)
        ):
            return True
    model_status = getattr(plan, "model_status", None)
    if model_status is None and isinstance(plan, dict):
        model_status = plan.get("model_status")
    return model_status == "DEGRADED_FALLBACK"


def should_auto_rescan_mock_session(host: Any) -> bool:
    """Return True if an unedited mock/fallback plan can be safely upgraded with active AI embeddings."""
    base_dir = getattr(host, "base_dir", None)
    s_dirs = getattr(host, "source_dirs", None) or ([base_dir] if base_dir else [])
    if not s_dirs or not any(os.path.exists(d) for d in s_dirs):
        return False
    if host._running_pipeline_worker() is not None or getattr(
        host, "_daemon_is_moving", False
    ):
        return False
    if not is_mock_or_degraded_session(host):
        return False
    return not plan_has_manual_modifications(host)


def trigger_ai_active_rescan_if_eligible(host: Any) -> None:
    """Re-scan an unedited mock session when verified AI weights and ONNX Runtime are active."""
    host._pending_ai_active_rescan = False
    if _downloader.get_ai_runtime_state(host.settings).get("state") != "active":
        return
    base_dir = getattr(host, "base_dir", None)
    if not getattr(host, "source_dirs", None) and base_dir:
        host.source_dirs = [base_dir]
    if should_auto_rescan_mock_session(host):
        host.announce(
            "Upgrading unedited sorting plan with verified AI embeddings...",
            priority="polite",
        )
        host._start_scan()


def poll_ai_provisioning_status(host: Any) -> None:
    """Poll background model download and verification state to refresh status bar badges."""
    try:
        poll_token = host._compute_ai_poll_token()
        if (
            getattr(host, "_last_ai_badge", None) is not None
            and getattr(host, "_last_ai_poll_token", None) == poll_token
        ):
            return

        prev_badge = getattr(host, "_last_ai_badge", None)
        prev_state = getattr(host, "_last_ai_state", None)
        runtime = _downloader.get_ai_runtime_state(host.settings)
        new_badge = str(runtime["badge"])
        new_state = str(runtime["state"])
        host._last_ai_badge = new_badge
        host._last_ai_state = new_state
        host._last_ai_poll_token = host._compute_ai_poll_token()

        if prev_badge is not None and prev_badge != new_badge:
            host.update_status()
        if prev_state is not None and prev_state != "active" and new_state == "active":
            if prev_state == "downloading":
                host.announce(
                    "AI model weights verified and active.",
                    priority="polite",
                )
            if host._running_pipeline_worker() == "run_scan_worker":
                host._pending_ai_active_rescan = True
            else:
                host._trigger_ai_active_rescan_if_eligible()
        elif (
            prev_state == "downloading"
            and new_state == "fallback"
            and runtime.get("error")
        ):
            host.announce(
                f"AI model download failed ({runtime['error']}). Using local keyword fallback.",
                priority="assertive",
            )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
    ) as exc:
        logger.debug("AI provisioning status poll error: %s", exc)
