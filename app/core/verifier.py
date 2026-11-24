"""Verification engine for proactive move validation."""

import contextlib
import logging
import os
import re
from typing import Any

from pydantic import BaseModel

from app.core.domain_contracts import _get_val, _set_val
from app.core.link_manager import LinkManager

try:
    import pylnk3
except ImportError:
    pylnk3 = None

logger = logging.getLogger(__name__)


def is_ml_available() -> bool:
    """Check if heavy machine learning dependencies (torch, easyocr) are available.

    Returns
    -------
    bool
        True if torch and easyocr are both importable, False otherwise.
    """
    try:
        import easyocr  # noqa: F401
        import torch  # noqa: F401

        return True
    except Exception:
        # A broken ML install can fail on import with more than ImportError
        # (e.g. OSError from a missing native library).
        logger.debug("ML dependencies are unavailable", exc_info=True)
        return False


def check_ai_status(settings) -> tuple[bool, str | None]:
    """Check AI models status, returning (is_healthy, warning_message).

    Parameters
    ----------
    settings : app.config.AppSettings
        The settings object of the application containing features flags.

    Returns
    -------
    tuple of (bool, str or None)
        A tuple containing:
        - is_healthy (bool): True if the machine learning/AI subsystem is healthy and fully available.
        - warning_message (str or None): A warning description if fallback or corrupt state is detected, otherwise None.

    Raises
    ------
    ValueError
        If key dependencies are missing/corrupted and we are running in sandboxed execution.
    """
    import sys

    from app.core.path_utils import get_base_path, get_meipass_dir, is_packaged

    if settings is not None and getattr(settings, "AI_CONSENT_GRANTED", None) is False:
        return (
            False,
            "AI processing is disabled because AI consent has been revoked.",
        )

    is_sandboxed = is_packaged()

    if not is_ml_available():
        mei = get_meipass_dir() or ""
        try:
            base = get_base_path(__file__)
        except OSError:
            base = ""
        exe_dir = os.path.dirname(sys.executable) if is_packaged() else ""

        lite_candidates = [
            os.path.join(mei, "LITE_BUILD") if mei else "",
            os.path.join(mei, "_internal", "LITE_BUILD") if mei else "",
            os.path.join(base, "LITE_BUILD") if base else "",
            os.path.join(base, "_internal", "LITE_BUILD") if base else "",
            os.path.join(exe_dir, "LITE_BUILD") if exe_dir else "",
            os.path.join(exe_dir, "_internal", "LITE_BUILD") if exe_dir else "",
        ]
        marker_lite = any(c and os.path.exists(c) for c in lite_candidates)

        is_lite = os.environ.get("LITE_BUILD") == "1" or marker_lite
        if is_sandboxed and not is_lite:
            raise ValueError(
                "Machine learning dependencies (PyTorch/EasyOCR) are missing in sandboxed execution. To enable ML features, install the 'ml' extra group: uv sync --extra ml or pip install '.[ml]'"
            )
        return (
            False,
            "Machine learning dependencies (PyTorch/EasyOCR) are not installed. Running in fallback state. To enable ML features, install the 'ml' extra group: uv sync --extra ml or pip install '.[ml]'",
        )

    from app.config import get_app_dir
    from app.core.path_utils import get_base_path

    try:
        base_path = get_base_path(__file__)
    except OSError:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    local_bundle_path = os.path.join(base_path, "offline_bundle", "model")
    try:
        user_bundle_path = str(get_app_dir() / "model")
    except (OSError, RuntimeError):
        user_bundle_path = os.path.expanduser("~/.smart-autosorter/model")

    model_dir = None
    mei_dir = get_meipass_dir()
    if mei_dir:
        mei_bundle_path = os.path.join(mei_dir, "offline_bundle", "model")
        if os.path.exists(mei_bundle_path):
            model_dir = mei_bundle_path
    if not model_dir:
        if os.path.exists(local_bundle_path):
            model_dir = local_bundle_path
        elif os.path.exists(user_bundle_path):
            model_dir = user_bundle_path

    if mei_dir:
        easyocr_dir = os.path.join(mei_dir, "offline_bundle", "easyocr")
    else:
        # The working directory is deliberately not searched: it is often a
        # folder of untrusted files that this app is sorting (#722).
        local_easyocr_path = os.path.join(base_path, "offline_bundle", "easyocr")
        easyocr_path = os.environ.get("EASYOCR_MODULE_PATH")
        if os.path.exists(local_easyocr_path):
            easyocr_dir = local_easyocr_path
        elif easyocr_path:
            easyocr_dir = os.path.join(easyocr_path, "model")
        else:
            easyocr_dir = os.path.expanduser("~/.EasyOCR/model")

    craft_model = os.path.join(easyocr_dir, "craft_mlt_25k.pth")
    lang_model = os.path.join(easyocr_dir, "english_g2.pth")

    if getattr(settings, "AI_ASSISTED_NAMING", False) or is_sandboxed:
        if not model_dir or not os.path.exists(os.path.join(model_dir, "config.json")):
            if is_sandboxed:
                raise ValueError(
                    "AI generative model weights are missing or corrupt in sandboxed execution."
                )
            return (
                False,
                "AI generative model weights are missing or corrupt. Running in fallback rule-based sorting state.",
            )

        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()
        try:
            registry.verify_integrity("generative_naming", model_dir)
        except Exception as e:  # noqa: BLE001 - any failure is reported as unhealthy
            if is_sandboxed:
                raise ValueError(
                    f"AI generative model integrity check failed in sandboxed execution: {str(e)}"
                )
            return (
                False,
                f"AI generative model integrity check failed: {str(e)}. Running in fallback rule-based sorting state.",
            )

    if not os.path.exists(craft_model) or not os.path.exists(lang_model):
        if is_sandboxed:
            raise ValueError(
                "OCR vision model files are corrupt or missing in sandboxed execution."
            )
        return (
            False,
            "OCR vision model files are corrupt or missing. Visual text extraction is unavailable.",
        )

    # Let's also verify easyocr integrity if expected hashes exist!
    from app.core.shared_registry import SharedModelRegistry

    registry = SharedModelRegistry.get_instance()
    if registry.get_expected_hashes("easyocr"):
        try:
            registry.verify_integrity("easyocr", easyocr_dir)
        except Exception as e:  # noqa: BLE001 - any failure is reported as unhealthy
            if is_sandboxed:
                raise ValueError(
                    f"OCR vision model integrity check failed in sandboxed execution: {str(e)}"
                )
            return (
                False,
                f"OCR vision model integrity check failed: {str(e)}. Visual text extraction is unavailable.",
            )

    if registry.get_expected_hashes("florence-2"):
        from app.core.offline_loader import (
            ModelWeightsNotFoundError,
            OfflineModelLoader,
        )

        try:
            florence_dir = OfflineModelLoader.resolve_model_path("florence-2")
            registry.verify_integrity("florence-2", florence_dir)
        except ModelWeightsNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 - any failure is reported as unhealthy
            if is_sandboxed:
                raise ValueError(
                    f"Florence-2 vision model integrity check failed in sandboxed execution: {str(e)}"
                )
            return (
                False,
                f"Florence-2 vision model integrity check failed: {str(e)}.",
            )

    return True, None


_RECOVERABLE_COLLISION_TYPES = ("target_collision", "duplicate_target")


class VerificationEngine:
    """Engine to verify file operations before execution."""

    @staticmethod
    def is_critical_error(result: dict) -> bool:
        """Return True if verification result contains any blocking critical errors."""
        if not result or not isinstance(result, dict):
            return False
        if result.get("has_critical_errors") is not None:
            return bool(result.get("has_critical_errors"))
        critical_collisions = [
            c
            for c in result.get("collisions", [])
            if c.get("type") not in _RECOVERABLE_COLLISION_TYPES
        ]
        return bool(
            critical_collisions
            or result.get("circular_renames")
            or result.get("invalid_renames")
            or result.get("long_paths")
            or result.get("escaping_paths")
        )

    @staticmethod
    def sanitize_plan(plan: dict) -> tuple[dict, list[str]]:
        """Sanitize plan folder keys and file targets, returning (sanitized_plan, warnings)."""
        from app.core.path_utils import sanitize_plan as _sp

        return _sp(plan)

    @staticmethod
    def verify_plan_integrity(
        base_dir: str, plan: Any, conflict_policy: str = "skip"
    ) -> dict:
        """Run complete virtual filesystem simulation and integrity check."""
        try:
            from app.core.path_utils import _sanitize_plan_with_base

            sanitized_plan, sanitization_warnings = _sanitize_plan_with_base(
                plan, base_dir=base_dir
            )
            import logging

            for warn in sanitization_warnings:
                logging.info(f"Pre-flight plan sanitization adjustment: {warn}")

            if isinstance(plan, dict):
                plan.clear()
                plan.update(sanitized_plan)
                plan_dict = plan
            elif hasattr(plan, "plan") and isinstance(getattr(plan, "plan"), dict):
                plan.plan = sanitized_plan
                plan_dict = plan.plan
            else:
                plan_dict = sanitized_plan

            tracker = VirtualFilesystemTracker()
            result = tracker.verify_integrity(
                base_dir, plan_dict, conflict_policy=conflict_policy
            )

            all_warnings = sanitization_warnings + result.get("warnings", [])
            result["warnings"] = list(dict.fromkeys(all_warnings))
            result["plan"] = plan
            return result
        except ValueError as e:
            return {
                "success": False,
                "collisions": [],
                "circular_renames": [],
                "broken_links": [],
                "long_paths": [],
                "warnings": [str(e)],
            }

    @staticmethod
    def get_moves(
        base_dir: str,
        plan: Any,
        current_dest: str = "",
        active_parent_path: str = "",
        depth: int = 0,
    ) -> list:
        """Get a flat list of moves from the plan."""
        base_dir = os.path.normpath(base_dir)
        moves: list[Any] = []
        if hasattr(plan, "plan") and isinstance(plan.plan, dict):
            plan = plan.plan

        if not isinstance(plan, dict):
            return moves

        from app.core.scanner import parse_item_identifier

        for key, content in plan.items():
            node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
            is_node = (
                content is None
                or node_type in ("file", "directory")
                or isinstance(content, BaseModel)
            )
            if is_node:
                if node_type == "directory":
                    continue
                if (
                    _get_val(content, "is_excluded")
                    or _get_val(content, "excluded")
                    or _get_val(content, "is_checked") is False
                ):
                    continue

                parsed_root, rel_key = parse_item_identifier(str(key), base_dir)
                src_root = _get_val(content, "source_root")
                if not src_root and "::" in str(key):
                    src_root = parsed_root
                effective_base = src_root if src_root else base_dir

                src_p = _get_val(content, "source_path") or _get_val(
                    content, "filepath"
                )
                if src_p and "::" in str(src_p):
                    parsed_r, parsed_p = parse_item_identifier(
                        str(src_p), effective_base
                    )
                    effective_base = src_root or parsed_r
                    src_p = parsed_p

                if src_p and os.path.isabs(src_p):
                    source_path = os.path.normpath(src_p)
                elif depth > 0:
                    rel_src_val = _get_val(content, "relative_source")
                    if rel_src_val is None and not src_p:
                        raise ValueError(
                            f"Missing required relative source metadata field for nested item '{key}'"
                        )
                    if src_p:
                        cand_src_p = os.path.normpath(
                            os.path.join(effective_base, src_p)
                        )
                    else:
                        cand_src_p = None

                    if rel_src_val is None and cand_src_p is not None:
                        source_path = cand_src_p
                    else:
                        rel_str = str(rel_src_val or "")
                        relative_source = (
                            rel_str.split("::", 1)[1] if "::" in rel_str else rel_str
                        )
                        rel_src_norm = relative_source.replace("\\", "/")
                        parent_norm = active_parent_path.replace("\\", "/")
                        if parent_norm and (
                            rel_src_norm.startswith(parent_norm + "/")
                            or rel_src_norm == parent_norm
                        ):
                            rel_src_with_parent = relative_source
                        else:
                            rel_src_with_parent = os.path.join(
                                active_parent_path, relative_source
                            )
                        primary_cand = os.path.normpath(
                            os.path.join(effective_base, rel_src_with_parent)
                        )
                        alt_cand = os.path.normpath(
                            os.path.join(effective_base, relative_source)
                        )
                        raw_key = key.split("::", 1)[1] if "::" in str(key) else key
                        key_cand = os.path.normpath(
                            os.path.join(effective_base, raw_key)
                        )
                        if cand_src_p and os.path.lexists(cand_src_p):
                            source_path = cand_src_p
                        elif os.path.lexists(alt_cand) and not os.path.lexists(
                            primary_cand
                        ):
                            source_path = alt_cand
                        elif os.path.lexists(primary_cand):
                            source_path = primary_cand
                        elif (
                            "/" in str(raw_key) or "\\" in str(raw_key)
                        ) and os.path.lexists(key_cand):
                            source_path = key_cand
                        elif cand_src_p:
                            source_path = cand_src_p
                        else:
                            source_path = primary_cand
                else:
                    rel_src_val = _get_val(content, "relative_source")
                    if src_p:
                        source_path = os.path.normpath(
                            os.path.join(effective_base, src_p)
                        )
                    elif rel_src_val is not None:
                        _, relative_source = parse_item_identifier(
                            str(rel_src_val), effective_base
                        )
                        source_path = os.path.normpath(
                            os.path.join(effective_base, relative_source)
                        )
                    else:
                        source_path = os.path.normpath(
                            os.path.join(effective_base, rel_key)
                        )

                tgt_fn = _get_val(content, "target_filename")
                if tgt_fn is not None:
                    _, clean_tf = parse_item_identifier(str(tgt_fn), effective_base)
                    filename = re.split(r"[/\\]+", clean_tf)[-1]
                else:
                    filename = re.split(r"[/\\]+", rel_key)[-1]

                from app.core.path_utils import _is_absolute_path

                dest_override = _get_val(content, "destination_override")
                if dest_override and _is_absolute_path(dest_override):
                    dest_dir = dest_override
                elif dest_override:
                    dest_dir = os.path.join(base_dir, dest_override)
                elif _is_absolute_path(current_dest):
                    dest_dir = current_dest
                else:
                    dest_dir = os.path.join(base_dir, current_dest)

                if re.match(r"^[a-zA-Z]:[/\\]", dest_dir):
                    sep = "\\" if "\\" in dest_dir else "/"
                    planned_dest_path = f"{dest_dir.rstrip('/\\')}{sep}{filename}"
                else:
                    planned_dest_path = os.path.normpath(
                        os.path.join(dest_dir, filename)
                    )

                dup_action = _get_val(content, "duplicate_action")
                pol_action = _get_val(content, "policy_action")
                if dup_action in ("stage", "trash") or pol_action == "trash":
                    from app.core.resilient_file_ops import duplicate_staging_relpath

                    stage_rel = duplicate_staging_relpath(
                        source_path or key, base_dir, source_root=effective_base
                    )
                    dest_path = os.path.normpath(
                        os.path.join(
                            base_dir, ".sortify_duplicates", "_staged", stage_rel
                        )
                    )
                else:
                    dest_path = planned_dest_path
                moves.append((key, source_path, dest_path))
            else:
                from app.core.path_utils import _is_absolute_path

                if depth == 0 and _is_absolute_path(key):
                    next_dest = key
                    next_active_parent = active_parent_path
                else:
                    next_dest = os.path.join(current_dest, key)
                    next_active_parent = (
                        active_parent_path
                        if _is_absolute_path(key)
                        else os.path.join(active_parent_path, key)
                    )

                moves.extend(
                    VerificationEngine.get_moves(
                        base_dir,
                        content,
                        next_dest,
                        next_active_parent,
                        depth + 1,
                    )
                )
        return moves

    @staticmethod
    def get_move_entries(
        base_dir: str,
        plan: Any,
        current_dest: str = "",
        active_parent_path: str = "",
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        """Collect rich metadata dictionaries for every active file node in ``plan``."""
        base_dir = os.path.normpath(base_dir)
        entries: list[dict[str, Any]] = []
        if hasattr(plan, "plan") and isinstance(plan.plan, dict):
            plan = plan.plan
        if not isinstance(plan, dict):
            return entries

        from app.core.path_utils import _is_absolute_path
        from app.core.scanner import parse_item_identifier

        for key, content in plan.items():
            node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
            is_node = (
                content is None
                or node_type in ("file", "directory")
                or isinstance(content, BaseModel)
            )
            if is_node:
                if node_type == "directory":
                    continue
                if (
                    _get_val(content, "is_excluded")
                    or _get_val(content, "excluded")
                    or _get_val(content, "is_checked") is False
                ):
                    continue

                # Reuse get_moves resolution on a single-entry subplan for exact parity
                single_moves = VerificationEngine.get_moves(
                    base_dir,
                    {key: content},
                    current_dest=current_dest,
                    active_parent_path=active_parent_path,
                    depth=depth,
                )
                if not single_moves:
                    continue
                _, source_path, dest_path = single_moves[0]

                parsed_root, rel_key = parse_item_identifier(str(key), base_dir)
                src_root = _get_val(content, "source_root") or (
                    parsed_root if "::" in str(key) else base_dir
                )
                tgt_fn = _get_val(content, "target_filename")
                if tgt_fn is not None:
                    _, clean_tf = parse_item_identifier(str(tgt_fn), src_root)
                    filename = re.split(r"[/\\]+", clean_tf)[-1]
                else:
                    filename = re.split(r"[/\\]+", rel_key)[-1]
                dest_override = _get_val(content, "destination_override")
                if dest_override and _is_absolute_path(dest_override):
                    dest_dir = dest_override
                elif dest_override:
                    dest_dir = os.path.join(base_dir, dest_override)
                elif _is_absolute_path(current_dest):
                    dest_dir = current_dest
                else:
                    dest_dir = os.path.join(base_dir, current_dest)

                if re.match(r"^[a-zA-Z]:[/\\]", dest_dir):
                    sep = "\\" if "\\" in dest_dir else "/"
                    planned_dest = f"{dest_dir.rstrip('/\\')}{sep}{filename}"
                else:
                    planned_dest = os.path.normpath(os.path.join(dest_dir, filename))

                entries.append(
                    {
                        "node_key": key,
                        "source_path": os.path.normpath(os.path.abspath(source_path)),
                        "dest_path": os.path.normpath(os.path.abspath(dest_path)),
                        "planned_dest_path": os.path.normpath(
                            os.path.abspath(planned_dest)
                        ),
                        "content": content,
                        "source_root": src_root,
                        "parent_dict": plan,
                        "current_dest": current_dest,
                    }
                )
            else:
                if depth == 0 and _is_absolute_path(key):
                    next_dest = key
                    next_active_parent = active_parent_path
                else:
                    next_dest = os.path.join(current_dest, key)
                    next_active_parent = (
                        active_parent_path
                        if _is_absolute_path(key)
                        else os.path.join(active_parent_path, key)
                    )
                entries.extend(
                    VerificationEngine.get_move_entries(
                        base_dir,
                        content,
                        next_dest,
                        next_active_parent,
                        depth + 1,
                    )
                )
        return entries

    @staticmethod
    def get_duplicate_groups(
        base_dir: str,
        plan: Any,
        detect_original_by: str = "first_seen",
        include_resolved: bool = False,
    ) -> list[dict]:
        """Detect byte-identical duplicate file clusters across active plan items."""
        from app.core.resilient_file_ops import (
            group_byte_identical_files,
            same_file_content,
        )

        entries = VerificationEngine.get_move_entries(base_dir, plan)
        if not entries:
            return []

        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""
        src_to_entry: dict[str, dict[str, Any]] = {}
        ordered_sources: list[str] = []
        moving_sources: set[str] = set()

        for entry in entries:
            abs_src = entry["source_path"]
            moving_sources.add(abs_src)
            if abs_src not in src_to_entry:
                src_to_entry[abs_src] = entry
                ordered_sources.append(abs_src)

        # Include any stationary existing file at a planned destination that has identical bytes
        stationary_candidates: list[str] = []
        seen_stationary: set[str] = set()
        for entry in entries:
            planned_dst = entry["planned_dest_path"]
            if (
                planned_dst not in moving_sources
                and planned_dst not in seen_stationary
                and os.path.isfile(planned_dst)
                and not os.path.islink(planned_dst)
            ):
                try:
                    if not (
                        os.path.exists(entry["source_path"])
                        and os.path.samefile(planned_dst, entry["source_path"])
                    ) and same_file_content(planned_dst, entry["source_path"]):
                        stationary_candidates.append(planned_dst)
                        seen_stationary.add(planned_dst)
                except OSError:
                    continue

        candidate_paths = [*stationary_candidates, *ordered_sources]
        clusters = group_byte_identical_files(
            candidate_paths, detect_original_by=detect_original_by
        )
        if not clusters:
            return []

        groups: list[dict[str, Any]] = []
        group_counter = 1

        for cluster in clusters:
            cluster_paths = cluster["paths"]
            stationary_in_cluster = [
                p
                for p in cluster_paths
                if p in seen_stationary and p not in src_to_entry
            ]
            plan_paths_in_cluster = [p for p in cluster_paths if p in src_to_entry]
            if not plan_paths_in_cluster:
                continue

            # If there is a stationary file already at destination, treat it as surviving original
            if stationary_in_cluster:
                primary_src = stationary_in_cluster[0]
                primary_target = primary_src
                existing_on_disk = True
                redundant_plan_paths = plan_paths_in_cluster
            else:
                if len(plan_paths_in_cluster) < 2:
                    continue
                primary_src = cluster["original"]
                if primary_src not in src_to_entry:
                    primary_src = plan_paths_in_cluster[0]
                primary_entry = src_to_entry[primary_src]
                primary_target = primary_entry["planned_dest_path"]
                existing_on_disk = False
                redundant_plan_paths = [
                    p for p in plan_paths_in_cluster if p != primary_src
                ]

            if not include_resolved and all(
                bool(_get_val(src_to_entry[p]["content"], "duplicate_resolved"))
                for p in redundant_plan_paths
            ):
                continue

            items: list[dict[str, Any]] = []
            ordered_for_modal = (
                redundant_plan_paths
                if existing_on_disk
                else [primary_src, *redundant_plan_paths]
            )
            default_action = "stage"
            for idx, p in enumerate(ordered_for_modal):
                entry = src_to_entry[p]
                c_node = entry["content"]
                pre_action = _get_val(c_node, "duplicate_action")
                if pre_action in ("stage", "trash", "keep", "rename", "skip"):
                    default_action = str(pre_action)
                rel_src = (
                    os.path.relpath(p, base_dir_abs)
                    if base_dir_abs and p.startswith(base_dir_abs)
                    else p
                )
                items.append(
                    {
                        "node_key": entry["node_key"],
                        "source_path": p,
                        "relative_source": rel_src,
                        "target_filename": os.path.basename(entry["planned_dest_path"]),
                        "target_path": entry["planned_dest_path"],
                        "is_primary": (not existing_on_disk and idx == 0),
                        "byte_size": cluster["byte_size"],
                    }
                )

            gid = f"dup_{group_counter}"
            group_counter += 1
            groups.append(
                {
                    "group_id": gid,
                    "group_kind": "duplicate",
                    "target_path": primary_target,
                    "target_filename": os.path.basename(primary_target),
                    "target_dir": os.path.dirname(primary_target),
                    "existing_file_on_disk": existing_on_disk,
                    "primary_source_path": primary_src,
                    "byte_size": cluster["byte_size"],
                    "sha256": cluster["sha256"],
                    "detect_original_by": cluster["detect_original_by"],
                    "items": items,
                    "default_action": default_action,
                }
            )

        return groups

    @staticmethod
    def get_collision_groups(
        base_dir: str, plan: Any, *, include_resolved: bool = False
    ) -> list[dict]:
        """Detect destination target collisions among distinct files."""
        from app.core.resilient_file_ops import same_file_content

        entries = VerificationEngine.get_move_entries(base_dir, plan)
        if not entries:
            return []

        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""
        dest_to_entries: dict[str, list[dict[str, Any]]] = {}
        moving_sources = {entry["source_path"] for entry in entries}

        for entry in entries:
            content = entry["content"]
            if (
                _get_val(content, "duplicate_action")
                in (
                    "stage",
                    "trash",
                    "skip",
                )
                or _get_val(content, "policy_action") == "trash"
            ):
                continue
            if not include_resolved and (
                _get_val(content, "overwrite") or _get_val(content, "allow_overwrite")
            ):
                continue
            dest_to_entries.setdefault(entry["dest_path"], []).append(entry)

        groups: list[dict[str, Any]] = []
        group_counter = 1

        for dst_path, dst_entries in dest_to_entries.items():
            existing_on_disk = (
                os.path.lexists(dst_path)
                and not os.path.isdir(dst_path)
                and dst_path not in moving_sources
            )
            is_existing_collision = False
            if existing_on_disk:
                for entry in dst_entries:
                    src_p = entry["source_path"]
                    if src_p != dst_path:
                        try:
                            if not (
                                os.path.exists(src_p)
                                and os.path.samefile(dst_path, src_p)
                            ):
                                is_existing_collision = True
                                break
                        except OSError:
                            is_existing_collision = True
                            break

            if len(dst_entries) <= 1 and not is_existing_collision:
                continue

            # Exclude pure byte-identical clusters unless user already resolved duplicate review
            comparison_paths = [entry["source_path"] for entry in dst_entries]
            if is_existing_collision:
                comparison_paths.insert(0, dst_path)

            all_exist_regular = all(
                os.path.isfile(p) and not os.path.islink(p) for p in comparison_paths
            )
            any_unresolved_dup = any(
                not bool(_get_val(entry["content"], "duplicate_resolved"))
                for entry in dst_entries
            )
            if (
                all_exist_regular
                and len(comparison_paths) >= 2
                and any_unresolved_dup
                and all(
                    same_file_content(comparison_paths[0], p)
                    for p in comparison_paths[1:]
                )
            ):
                continue

            items = []
            for entry in dst_entries:
                src_p = entry["source_path"]
                rel_src = (
                    os.path.relpath(src_p, base_dir_abs)
                    if base_dir_abs and src_p.startswith(base_dir_abs)
                    else src_p
                )
                items.append(
                    {
                        "node_key": entry["node_key"],
                        "source_path": src_p,
                        "relative_source": rel_src,
                        "target_filename": os.path.basename(dst_path),
                        "target_path": dst_path,
                    }
                )

            group_id = f"group_{group_counter}"
            group_counter += 1
            groups.append(
                {
                    "group_id": group_id,
                    "group_kind": "collision",
                    "target_path": dst_path,
                    "target_filename": os.path.basename(dst_path),
                    "target_dir": os.path.dirname(dst_path),
                    "existing_file_on_disk": is_existing_collision,
                    "items": items,
                    "default_action": "rename",
                }
            )

        return groups

    @staticmethod
    def apply_collision_selections(
        base_dir: str,
        plan: Any,
        selections: dict[str, str],
        groups: list[dict] | None = None,
    ) -> Any:
        """Apply collision resolution selections (rename, overwrite, keep_both, keep, skip) to plan."""
        target_groups = (
            groups
            if groups is not None
            else VerificationEngine.get_collision_groups(
                base_dir, plan, include_resolved=True
            )
        )
        return VerificationEngine.apply_deduplication_selections(
            base_dir, plan, selections, groups=target_groups
        )

    @staticmethod
    def apply_deduplication_selections(
        base_dir: str,
        plan: Any,
        selections: dict[str, str],
        groups: list[dict] | None = None,
        detect_original_by: str = "first_seen",
    ) -> Any:
        """Apply duplicate or collision resolution selections to move plan items."""
        if not selections:
            return plan

        if groups is None:
            dup_groups = VerificationEngine.get_duplicate_groups(
                base_dir,
                plan,
                detect_original_by=detect_original_by,
                include_resolved=True,
            )
            col_groups = VerificationEngine.get_collision_groups(
                base_dir, plan, include_resolved=True
            )
            groups = [*dup_groups, *col_groups]

        if not groups:
            return plan

        from app.core.scanner import parse_item_identifier

        entries = VerificationEngine.get_move_entries(base_dir, plan)
        entry_by_src = {entry["source_path"]: entry for entry in entries}
        entry_by_key = {entry["node_key"]: entry for entry in entries}

        for group in groups:
            gid = group["group_id"]
            t_path = group.get("target_path", "")
            group_kind = group.get("group_kind") or (
                "duplicate" if str(gid).startswith("dup_") else "collision"
            )
            action = (
                selections.get(gid)
                or (selections.get(t_path) if t_path else None)
                or group.get(
                    "default_action",
                    "stage" if group_kind == "duplicate" else "rename",
                )
            )
            items = group.get("items", [])
            existing_on_disk = bool(group.get("existing_file_on_disk"))
            primary_src = group.get("primary_source_path") or (
                items[0].get("source_path") if items else None
            )

            for idx, item_obj in enumerate(items):
                src_p = item_obj.get("source_path") or item_obj.get("filepath")
                n_key = item_obj.get("node_key")
                resolved_src_key = None
                if src_p:
                    resolved_src_key = os.path.normpath(
                        os.path.abspath(
                            src_p
                            if os.path.isabs(str(src_p))
                            else os.path.join(base_dir, str(src_p))
                        )
                    )
                entry = (
                    entry_by_src.get(resolved_src_key) if resolved_src_key else None
                ) or (entry_by_key.get(n_key) if n_key else None)
                if not entry:
                    continue

                content = entry["content"]
                if content is None and isinstance(entry.get("parent_dict"), dict):
                    content = {
                        "__type__": "file",
                        "source_path": entry["source_path"],
                        "relative_source": item_obj.get("relative_source") or n_key,
                    }
                    entry["parent_dict"][n_key] = content
                    entry["content"] = content

                raw_tf = (
                    _get_val(content, "target_filename")
                    or item_obj.get("target_filename")
                    or n_key
                )
                _, clean_raw_tf = parse_item_identifier(
                    str(raw_tf), entry.get("source_root") or base_dir
                )
                filename = re.split(r"[/\\]+", str(clean_raw_tf))[-1]
                item_action = (
                    (selections.get(src_p) if src_p else None)
                    or (selections.get(n_key) if n_key else None)
                    or selections.get(f"{gid}_{idx}")
                    or action
                )
                is_primary_item = (
                    group_kind == "duplicate"
                    and not existing_on_disk
                    and (bool(item_obj.get("is_primary")) or idx == 0)
                )

                if group_kind == "duplicate" and item_action in ("stage", "trash"):
                    _set_val(content, "duplicate_resolved", True)
                    if is_primary_item:
                        _set_val(content, "is_duplicate", False)
                        _set_val(content, "is_duplicate_primary", True)
                        _set_val(content, "duplicate_action", None)
                        _set_val(content, "confirmed", True)
                        _set_val(content, "is_confirmed", True)
                        _set_val(content, "is_excluded", False)
                        _set_val(content, "status", "Confirmed")
                    else:
                        _set_val(content, "is_duplicate", True)
                        _set_val(content, "is_duplicate_primary", False)
                        if primary_src:
                            _set_val(content, "duplicate_of", primary_src)
                        _set_val(content, "duplicate_action", item_action)
                        _set_val(content, "confirmed", True)
                        _set_val(content, "is_confirmed", True)
                        _set_val(content, "is_excluded", False)
                        _set_val(
                            content,
                            "status",
                            "Staged Duplicate"
                            if item_action == "stage"
                            else "Trash Duplicate",
                        )
                elif item_action == "skip":
                    if group_kind == "duplicate":
                        _set_val(content, "duplicate_resolved", True)
                        _set_val(content, "duplicate_action", "skip")
                    _set_val(content, "is_excluded", True)
                    _set_val(content, "is_checked", False)
                    _set_val(content, "status", "Skipped")
                elif item_action == "overwrite":
                    if group_kind == "duplicate":
                        _set_val(content, "duplicate_resolved", True)
                        _set_val(content, "duplicate_action", None)
                    _set_val(content, "overwrite", True)
                    _set_val(content, "allow_overwrite", True)
                    _set_val(content, "confirmed", True)
                    _set_val(content, "is_confirmed", True)
                    _set_val(content, "status", "Confirmed")
                    _set_val(content, "is_excluded", False)
                elif item_action == "keep_both":
                    from datetime import datetime

                    if group_kind == "duplicate":
                        _set_val(content, "duplicate_resolved", True)
                        _set_val(content, "duplicate_action", None)
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    prefix = f"{ts}_" if idx == 0 else f"{ts}_{idx}_"
                    new_name = f"{prefix}{filename}"
                    _set_val(content, "target_filename", new_name)
                    _set_val(content, "confirmed", True)
                    _set_val(content, "is_confirmed", True)
                    _set_val(content, "status", "Confirmed")
                    _set_val(content, "is_excluded", False)
                elif item_action == "rename":
                    if group_kind == "duplicate":
                        _set_val(content, "duplicate_resolved", True)
                        _set_val(content, "duplicate_action", None)
                    if idx > 0 or existing_on_disk:
                        stem, ext = os.path.splitext(filename)
                        counter = idx if idx > 0 else 1
                        new_name = f"{stem}_{counter}{ext}"
                        _set_val(content, "target_filename", new_name)
                    _set_val(content, "confirmed", True)
                    _set_val(content, "is_confirmed", True)
                    _set_val(content, "status", "Confirmed")
                    _set_val(content, "is_excluded", False)
                elif item_action == "keep":
                    if group_kind == "duplicate":
                        _set_val(content, "duplicate_resolved", True)
                    if idx == 0 and not existing_on_disk:
                        _set_val(content, "duplicate_action", None)
                        _set_val(content, "confirmed", True)
                        _set_val(content, "is_confirmed", True)
                        _set_val(content, "status", "Confirmed")
                    else:
                        _set_val(content, "duplicate_action", "skip")
                        _set_val(content, "is_excluded", True)
                        _set_val(content, "is_checked", False)
                        _set_val(content, "status", "Skipped")

        return plan


class VirtualNode:
    """Represents a simulated file or directory in the virtual filesystem tracker."""

    def __init__(
        self, path, is_dir, inode, size, symlink_target=None, shortcut_target=None
    ):
        self.path = os.path.abspath(path)
        self.is_dir = is_dir
        self.inode = inode
        self.size = size
        self.symlink_target = symlink_target
        self.shortcut_target = shortcut_target


class VirtualFilesystemTracker:
    """In-memory virtual filesystem tracker to simulate file operations and verify path integrity."""

    def __init__(self):
        self.nodes = {}
        self._inode_counter = 1000000

    def _get_next_inode(self) -> int:
        self._inode_counter += 1
        return self._inode_counter

    def populate_from_disk(self, base_dir: str):
        """Traverse base_dir recursively and populate the virtual filesystem."""
        if not base_dir or not os.path.exists(base_dir):
            return

        base_dir_abs = os.path.abspath(base_dir)

        # Add base_dir itself
        try:
            st = os.stat(base_dir_abs)
            inode = st.st_ino
            size = st.st_size
        except OSError:
            inode = self._get_next_inode()
            size = 0
        self.nodes[base_dir_abs] = VirtualNode(
            base_dir_abs, is_dir=True, inode=inode, size=size
        )

        for root, dirs, files in os.walk(base_dir):
            for d in dirs:
                dir_path = os.path.join(root, d)
                abs_dir_path = os.path.abspath(dir_path)
                try:
                    st = os.stat(abs_dir_path)
                    inode = st.st_ino
                    size = st.st_size
                except OSError:
                    inode = self._get_next_inode()
                    size = 0
                self.nodes[abs_dir_path] = VirtualNode(
                    abs_dir_path, is_dir=True, inode=inode, size=size
                )

            for f in files:
                file_path = os.path.join(root, f)
                abs_file_path = os.path.abspath(file_path)

                # Check if symbolic link
                symlink_target = None
                if os.path.islink(abs_file_path):
                    with contextlib.suppress(OSError):
                        symlink_target = os.readlink(abs_file_path)

                # Check if Windows shortcut (.lnk)
                shortcut_target = None
                if abs_file_path.lower().endswith(".lnk"):
                    info = LinkManager.get_link_info(abs_file_path)
                    if info and info.get("type") == "lnk":
                        shortcut_target = info.get("target")
                    elif pylnk3:
                        try:
                            lnk = pylnk3.parse(abs_file_path)
                            shortcut_target = lnk.path
                        except Exception:
                            logger.debug(
                                "Could not parse shortcut %s",
                                abs_file_path,
                                exc_info=True,
                            )

                try:
                    st = (
                        os.lstat(abs_file_path)
                        if symlink_target
                        else os.stat(abs_file_path)
                    )
                    inode = st.st_ino
                    size = st.st_size
                except OSError:
                    inode = self._get_next_inode()
                    size = 0

                self.nodes[abs_file_path] = VirtualNode(
                    abs_file_path,
                    is_dir=False,
                    inode=inode,
                    size=size,
                    symlink_target=symlink_target,
                    shortcut_target=shortcut_target,
                )

    def populate_from_moves(self, moves_list: list):
        """Ensure all source files in the moves are registered in the virtual filesystem."""
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            if abs_src not in self.nodes:
                inode = self._get_next_inode()
                symlink_target = None
                shortcut_target = None

                # Retrieve registered link info if available
                link_info = LinkManager.get_link_info(abs_src)
                if link_info:
                    if link_info.get("type") in ("symlink", "junction"):
                        symlink_target = link_info.get("target")
                    elif link_info.get("type") == "lnk":
                        shortcut_target = link_info.get("target")
                else:
                    if abs_src.lower().endswith(".lnk"):
                        shortcut_target = "mock_target.exe"

                self.nodes[abs_src] = VirtualNode(
                    abs_src,
                    is_dir=False,
                    inode=inode,
                    size=1024,  # default mock size
                    symlink_target=symlink_target,
                    shortcut_target=shortcut_target,
                )

    def simulate_final_state(self, moves_list: list) -> dict:
        """Return a new dict of nodes representing the filesystem state after executing all moves."""
        final_nodes = {}

        # 1. Start with copy of unmoved nodes
        moved_srcs = {os.path.abspath(src) for _, src, _ in moves_list}
        for path, node in self.nodes.items():
            if path not in moved_srcs:
                final_nodes[path] = node

        # 2. Add moved nodes at their destinations
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)
            node = self.nodes.get(abs_src)
            if node:
                new_node = VirtualNode(
                    abs_dst,
                    is_dir=node.is_dir,
                    inode=node.inode,
                    size=node.size,
                    symlink_target=node.symlink_target,
                    shortcut_target=node.shortcut_target,
                )
                final_nodes[abs_dst] = new_node

                # Also ensure all intermediate parent directories are in the final_nodes
                # unless they are already occupied by a file.
                current = os.path.dirname(abs_dst)
                while current and len(current) > 0:
                    if current not in final_nodes:
                        final_nodes[current] = VirtualNode(
                            current, is_dir=True, inode=self._get_next_inode(), size=0
                        )
                    parent = os.path.dirname(current)
                    if parent == current:
                        break
                    current = parent

        return final_nodes

    def check_collisions(
        self, moves_list: list, base_dir: str, conflict_policy: str = "rename"
    ) -> list:
        """Detect duplicate destination target collisions and blocked parent directory/ancestor paths."""
        collisions = []
        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""

        dest_to_srcs: dict[Any, Any] = {}
        moving_sources: set[str] = set()
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)
            dest_to_srcs.setdefault(abs_dst, []).append(abs_src)
            moving_sources.add(abs_src)

        # 1. Duplicate target collision (exempt if dynamic rename policy is active)
        if conflict_policy in ("skip", "overwrite"):
            for dst, srcs in dest_to_srcs.items():
                if len(srcs) > 1:
                    collisions.append(
                        {
                            "path": dst,
                            "type": "duplicate_target",
                            "sources": srcs,
                            "message": f"Multiple files are planned to be moved to the same destination: {dst}",
                        }
                    )

        # 2. Parent directory / target structure collisions
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)

            # Check if any ancestor is a file (blocked parent directory)
            current = os.path.dirname(abs_dst)
            ancestor_collision = False
            while current and len(current) >= len(base_dir_abs):
                node = self.nodes.get(current)
                if node and not node.is_dir:
                    collisions.append(
                        {
                            "path": abs_dst,
                            "type": "parent_directory_collision",
                            "blocked_by": current,
                            "source": abs_src,
                            "message": f"Destination path '{abs_dst}' is blocked because ancestor '{current}' is a file.",
                        }
                    )
                    ancestor_collision = True
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent

            if ancestor_collision:
                continue

            # Check destination itself
            node_at_dst = self.nodes.get(abs_dst)
            if node_at_dst:
                if node_at_dst.is_dir:
                    collisions.append(
                        {
                            "path": abs_dst,
                            "type": "target_is_directory",
                            "source": abs_src,
                            "message": f"Destination path '{abs_dst}' is already an existing directory.",
                        }
                    )
                else:
                    is_dst_moving = abs_dst in moving_sources
                    if not is_dst_moving and abs_src != abs_dst:
                        collisions.append(
                            {
                                "path": abs_dst,
                                "type": "target_collision",
                                "source": abs_src,
                                "message": f"Destination path '{abs_dst}' is blocked by an existing file that is not being moved.",
                            }
                        )

        return collisions

    def check_circular_renames(self, moves_list: list) -> list:
        """Trace renaming dependencies and detect circular renames."""
        move_map = {}
        for rel_path, src, dst in moves_list:
            abs_src = os.path.abspath(src)
            abs_dst = os.path.abspath(dst)
            if abs_src != abs_dst:
                move_map[abs_src] = abs_dst

        cycles = []
        visited = set()

        for start_path in move_map:
            if start_path in visited:
                continue

            path = start_path
            current_chain: list[Any] = []
            chain_set = set()

            while path in move_map:
                if path in chain_set:
                    cycle_start_idx = current_chain.index(path)
                    cycle = current_chain[cycle_start_idx:]
                    cycles.append(cycle)
                    break
                if path in visited:
                    break

                current_chain.append(path)
                chain_set.add(path)
                path = move_map[path]

            for p in current_chain:
                visited.add(p)

        circular_renames = []
        for cycle in cycles:
            cycle_str = " -> ".join(cycle) + f" -> {cycle[0]}"
            message = f"Circular renaming dependency detected: {cycle_str}"
            for path in cycle:
                circular_renames.append(
                    {"path": path, "cycle": cycle, "message": message}
                )

        return circular_renames

    def check_broken_links(
        self, moves_list: list, base_dir: str, final_nodes: dict
    ) -> list:
        """Validate symbolic links and Windows shortcut targets after simulating moves."""
        broken_links = []
        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""

        path_map = {
            os.path.normcase(os.path.abspath(src)): os.path.abspath(dst)
            for _, src, dst in moves_list
        }

        # Case-normalization map for final nodes
        final_nodes_norm = {os.path.normcase(p): p for p in final_nodes}

        for abs_path, node in final_nodes.items():
            target_path = None
            link_type = None

            if node.symlink_target:
                target_path = node.symlink_target
                link_type = "symlink"
            elif node.shortcut_target:
                target_path = node.shortcut_target
                link_type = "shortcut"

            if not target_path:
                continue

            # Determine original absolute target path before moves
            orig_src = None
            for rel_path, src, dst in moves_list:
                if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(abs_path):
                    orig_src = os.path.abspath(src)
                    break

            if orig_src is None:
                orig_src = abs_path

            if os.path.isabs(target_path):
                abs_target_orig = os.path.abspath(target_path)
            else:
                abs_target_orig = os.path.abspath(
                    os.path.join(os.path.dirname(orig_src), target_path)
                )

            # Final absolute target path after moves
            norm_target_orig = os.path.normcase(abs_target_orig)
            abs_target_final = path_map.get(norm_target_orig)
            if not abs_target_final:
                for src_norm, dst_path in path_map.items():
                    if norm_target_orig.startswith(src_norm + os.sep):
                        from app.core.path_utils import safe_relpath

                        rel = safe_relpath(abs_target_orig, src_norm)
                        abs_target_final = os.path.normpath(os.path.join(dst_path, rel))
                        break
            if not abs_target_final:
                abs_target_final = abs_target_orig

            # Verify target existence using case-normalized lookup
            is_inside_base = (
                os.path.normcase(abs_target_final).startswith(
                    os.path.normcase(base_dir_abs)
                )
                if base_dir_abs
                else False
            )

            if is_inside_base:
                target_exists = os.path.normcase(abs_target_final) in final_nodes_norm
            else:
                target_exists = os.path.exists(abs_target_final)

            if not target_exists:
                message = f"Broken {link_type} target: '{abs_path}' points to '{abs_target_final}', which does not exist."
                broken_links.append(
                    {
                        "path": abs_path,
                        "type": f"broken_{link_type}",
                        "target": abs_target_final,
                        "message": message,
                    }
                )

        return broken_links

    def verify_integrity(
        self, base_dir: str, plan: dict, conflict_policy: str = "skip"
    ) -> dict:
        """Run complete virtual filesystem simulation and integrity check."""
        moves_list = VerificationEngine.get_moves(base_dir, plan)

        # Populate initial VFS state
        self.populate_from_disk(base_dir)
        self.populate_from_moves(moves_list)

        # Trace and analyze issues
        collisions = self.check_collisions(
            moves_list, base_dir, conflict_policy=conflict_policy
        )
        recoverable_collisions = [
            c for c in collisions if c.get("type") in _RECOVERABLE_COLLISION_TYPES
        ]
        seen_dup_paths = {
            c.get("path")
            for c in recoverable_collisions
            if c.get("type") == "duplicate_target"
        }
        dest_to_srcs: dict[str, list[str]] = {}
        for _, src, dst in moves_list:
            abs_dst = os.path.abspath(dst)
            dest_to_srcs.setdefault(abs_dst, []).append(os.path.abspath(src))
        for dst, srcs in dest_to_srcs.items():
            if len(srcs) > 1 and dst not in seen_dup_paths:
                recoverable_collisions.append(
                    {
                        "path": dst,
                        "type": "duplicate_target",
                        "sources": srcs,
                        "message": f"Multiple files are planned to be moved to the same destination: {dst}",
                    }
                )
        critical_collisions = [
            c for c in collisions if c.get("type") not in _RECOVERABLE_COLLISION_TYPES
        ]
        circular_renames = self.check_circular_renames(moves_list)

        # Simulate final VFS state
        final_nodes = self.simulate_final_state(moves_list)

        # Validate links in final VFS state
        broken_links = self.check_broken_links(moves_list, base_dir, final_nodes)

        # Check path lengths
        from app.core.path_utils import is_path_too_long

        long_paths = []
        for rel_path, src, dst in moves_list:
            if is_path_too_long(dst):
                long_paths.append(
                    {
                        "path": os.path.abspath(dst),
                        "source": os.path.abspath(src),
                        "message": f"Destination path '{os.path.abspath(dst)}' exceeds the standard Windows character limit of 260 characters.",
                    }
                )

        # Check escaping target paths
        escaping_paths = self.check_escaping_paths(moves_list, base_dir)

        # Check rename proposals
        invalid_renames, unconfirmed_renames = self.check_rename_proposals(
            base_dir, plan
        )

        # Consolidate all warnings
        warnings = []
        for c in collisions:
            warnings.append(c["message"])
        for rc in recoverable_collisions:
            warnings.append(rc["message"])
        for r in circular_renames:
            warnings.append(r["message"])
        for link in broken_links:
            warnings.append(link["message"])
        for lp in long_paths:
            warnings.append(lp["message"])
        for ep in escaping_paths:
            warnings.append(ep["message"])
        for inv in invalid_renames:
            warnings.append(inv["message"])
        for unc in unconfirmed_renames:
            warnings.append(unc["message"])

        # Eliminate duplicate messages in warnings
        unique_warnings = list(dict.fromkeys(warnings))

        success = (
            len(collisions) == 0
            and len(recoverable_collisions) == 0
            and len(circular_renames) == 0
            and len(broken_links) == 0
            and len(long_paths) == 0
            and len(escaping_paths) == 0
            and len(invalid_renames) == 0
            and len(unconfirmed_renames) == 0
        )

        has_critical_errors = (
            len(critical_collisions) > 0
            or len(circular_renames) > 0
            or len(invalid_renames) > 0
            or len(long_paths) > 0
            or len(escaping_paths) > 0
        )

        return {
            "success": success,
            "has_critical_errors": has_critical_errors,
            "collisions": collisions,
            "recoverable_collisions": recoverable_collisions,
            "circular_renames": circular_renames,
            "broken_links": broken_links,
            "long_paths": long_paths,
            "escaping_paths": escaping_paths,
            "invalid_renames": invalid_renames,
            "unconfirmed_renames": unconfirmed_renames,
            "warnings": unique_warnings,
        }

    def check_escaping_paths(self, moves_list: list, base_dir: str) -> list:
        """Validate that all destination target paths resolve strictly within base_dir."""
        from app.core.mover import is_subpath_or_equal

        escaping_paths = []
        base_dir_abs = os.path.abspath(base_dir) if base_dir else ""

        for rel_path, src, dst in moves_list:
            abs_dst = os.path.abspath(dst)
            if not is_subpath_or_equal(abs_dst, base_dir_abs):
                message = f"Destination path '{abs_dst}' attempts to escape target base directory '{base_dir_abs}'."
                escaping_paths.append(
                    {
                        "path": abs_dst,
                        "source": os.path.abspath(src),
                        "type": "escaping_path",
                        "message": message,
                    }
                )

        return escaping_paths

    def check_rename_proposals(self, base_dir: str, plan: dict) -> tuple[list, list]:
        """Check all rename proposals in plan for OS character safety, extension preservation, and user confirmation."""
        from app.core.path_utils import is_valid_name
        from app.core.scanner import parse_item_identifier

        invalid_renames = []
        unconfirmed_renames = []

        def _inspect_node(node, current_dest=""):
            curr_dict = (
                node.plan
                if hasattr(node, "plan") and isinstance(node.plan, dict)
                else node
            )
            if not isinstance(curr_dict, dict):
                return

            for key, content in curr_dict.items():
                node_type = _get_val(content, "node_type") or _get_val(
                    content, "__type__"
                )
                is_file_or_dir = (
                    content is None
                    or node_type in ("file", "directory")
                    or isinstance(content, BaseModel)
                )
                if is_file_or_dir:
                    if node_type == "directory":
                        continue
                    if (
                        _get_val(content, "is_excluded")
                        or _get_val(content, "excluded")
                        or _get_val(content, "is_checked") is False
                        or _get_val(content, "duplicate_action")
                        in ("stage", "trash", "skip")
                        or _get_val(content, "policy_action") == "trash"
                    ):
                        continue

                    parsed_root, rel_key = parse_item_identifier(str(key), base_dir)
                    src_root = _get_val(content, "source_root")
                    if not src_root and "::" in str(key):
                        src_root = parsed_root
                    effective_base = src_root if src_root else base_dir

                    raw_src_p = _get_val(content, "source_path") or _get_val(
                        content, "filepath"
                    )
                    raw_rel_src = _get_val(content, "relative_source")
                    if raw_src_p is not None:
                        _, rel_src = parse_item_identifier(
                            str(raw_src_p), effective_base
                        )
                    elif raw_rel_src is not None:
                        _, rel_src = parse_item_identifier(
                            str(raw_rel_src), effective_base
                        )
                    else:
                        rel_src = rel_key
                    src_filename = re.split(r"[/\\]+", rel_src)[-1]

                    raw_tf = _get_val(content, "target_filename")
                    if raw_tf is not None:
                        _, clean_tf = parse_item_identifier(str(raw_tf), effective_base)
                        target_filename = re.split(r"[/\\]+", clean_tf)[-1]
                    else:
                        target_filename = re.split(r"[/\\]+", rel_key)[-1]

                    # Check if target_filename differs from src_filename (rename proposal)
                    if target_filename != src_filename:
                        _src_stem, src_ext = os.path.splitext(src_filename)
                        _tgt_stem, tgt_ext = os.path.splitext(target_filename)

                        src_p = _get_val(content, "source_path")
                        if src_p:
                            _, clean_sp = parse_item_identifier(
                                str(src_p), effective_base
                            )
                            source_path = os.path.normpath(
                                clean_sp
                                if os.path.isabs(clean_sp)
                                else os.path.join(effective_base, clean_sp)
                            )
                        else:
                            source_path = os.path.normpath(
                                os.path.join(effective_base, rel_src)
                            )
                        dest_dir = os.path.normpath(
                            os.path.join(base_dir, current_dest)
                        )
                        dest_path = os.path.normpath(
                            os.path.join(dest_dir, target_filename)
                        )

                        # 1. Extension preservation check
                        if src_ext.lower() != tgt_ext.lower():
                            invalid_renames.append(
                                {
                                    "source": source_path,
                                    "path": dest_path,
                                    "type": "modified_extension",
                                    "message": f"Proposed file rename '{src_filename}' -> '{target_filename}' modifies or deletes original file extension ('{src_ext}').",
                                }
                            )
                        # 2. OS character / structure safety check
                        elif not is_valid_name(target_filename):
                            invalid_renames.append(
                                {
                                    "source": source_path,
                                    "path": dest_path,
                                    "type": "illegal_os_characters",
                                    "message": f"Proposed filename '{target_filename}' contains illegal OS characters or invalid path structures.",
                                }
                            )
                        # 3. Explicit user confirmation check
                        else:
                            status_val = _get_val(content, "status", "") or ""
                            is_confirmed = bool(
                                _get_val(content, "confirmed")
                                or _get_val(content, "is_confirmed")
                                or _get_val(content, "user_confirmed")
                                or _get_val(content, "is_locked")
                                or status_val in ("Confirmed", "Locked")
                            )

                            if not is_confirmed:
                                unconfirmed_renames.append(
                                    {
                                        "source": source_path,
                                        "path": dest_path,
                                        "type": "unconfirmed_rename",
                                        "message": f"Proposed file rename '{src_filename}' -> '{target_filename}' lacks explicit user confirmation.",
                                    }
                                )
                else:
                    _inspect_node(content, os.path.join(current_dest, key))

        _inspect_node(plan)
        return invalid_renames, unconfirmed_renames
