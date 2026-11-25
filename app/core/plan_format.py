"""Human-readable rendering and duplicate/trash workflow helpers for sorting plans."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def normalize_duplicate_mode(value: object) -> str:
    """Normalize a configured or CLI ``DUPLICATE_ACTION`` value to lowercase."""
    raw = str(value or "off").strip().lower()
    return raw if raw in ("off", "skip", "stage", "trash", "review") else "off"


def validate_duplicate_mode_for_headless(
    dup_mode: object,
    *,
    is_tui: bool = False,
    non_interactive: bool = False,
) -> str | None:
    """Return a CLI validation error string when ``review`` mode lacks interactive TUI."""
    if normalize_duplicate_mode(dup_mode) != "review":
        return None
    if is_tui:
        if non_interactive:
            return "Validation error: Duplicate mode 'review' cannot be combined with --non-interactive."
        return None
    return (
        "Validation error: Duplicate mode 'review' requires an interactive TUI session "
        "(--tui or --interactive). Choose one of: off, skip, stage, trash for headless execution."
    )


def format_trash_handoff_warning(
    trash_handoff: Mapping[str, Any] | None,
) -> str | None:
    """Build a concise user-facing warning when OS Trash handoff kept staged fallback copies."""
    if not isinstance(trash_handoff, Mapping):
        return None
    trash_errors = dict(trash_handoff.get("errors") or {})
    if not trash_errors:
        return None
    first_err = next(iter(trash_errors.values()))
    return (
        f"Warning: OS Trash handoff failed for {len(trash_errors)} duplicate "
        f"item(s) ({first_err}); kept in reversible staging."
    )


def format_trashed_unrestorable_warning(
    trashed_count: int, *, session_id: str | None = None
) -> str | None:
    """Build a user-facing warning when a session includes copies already handed to OS Trash."""
    if trashed_count <= 0:
        return None
    noun = "copy" if trashed_count == 1 else "copies"
    verb_be = "was" if trashed_count == 1 else "were"
    verb_rem = "remains" if trashed_count == 1 else "remain"
    if session_id:
        return (
            f"Warning: {trashed_count} duplicate {noun} in session '{session_id}' "
            f"{verb_be} exported to OS Trash and {verb_rem} in OS Trash "
            "(cannot be restored automatically)."
        )
    return (
        f"Warning: {trashed_count} duplicate {noun} {verb_rem} in OS Trash "
        "(cannot be restored automatically)."
    )


def format_session_rollback_outcome(
    session_id: str,
    restored_count: int,
    trashed_remaining: int = 0,
) -> tuple[str, str]:
    """Return ``(announcement_message, notification_type)`` for TUI session rollback."""
    short_sid = f"{session_id[:8]}..."
    if restored_count == 0 and trashed_remaining > 0:
        noun = "copy" if trashed_remaining == 1 else "copies"
        verb_rem = "remains" if trashed_remaining == 1 else "remain"
        return (
            f"No reversible moves in session {short_sid}; "
            f"{trashed_remaining} duplicate {noun} {verb_rem} in OS Trash "
            "(cannot be restored automatically).",
            "warning",
        )
    if trashed_remaining > 0:
        warn = format_trashed_unrestorable_warning(trashed_remaining)
        return (
            f"Session {short_sid} rolled back ({restored_count} file(s) restored). {warn}",
            "warning",
        )
    return (
        f"Session {short_sid} successfully rolled back ({restored_count} file(s) restored).",
        "info",
    )


def count_plan_actions(plan: dict[str, Any] | None) -> dict[str, int]:
    """Count planned regular moves, staged duplicates, trashed items, and skipped duplicates."""
    totals = {"moved": 0, "staged": 0, "trashed": 0, "skipped": 0}

    def _walk(tree: Any) -> None:
        if not isinstance(tree, dict):
            return
        for name, node in tree.items():
            if str(name).startswith("__"):
                continue
            is_file = node is None or (
                isinstance(node, dict)
                and (
                    node.get("__type__") == "file"
                    or node.get("node_type") == "file"
                    or (
                        node.get("node_type") != "directory"
                        and (
                            "relative_source" in node
                            or "target_filename" in node
                            or "source_path" in node
                        )
                    )
                )
            )
            if is_file:
                info = node if isinstance(node, dict) else {}
                dup_act = str(info.get("duplicate_action") or "").strip().lower()
                pol_act = str(info.get("policy_action") or "").strip().lower()
                is_excl = bool(
                    info.get("is_excluded")
                    or info.get("excluded")
                    or info.get("is_checked") is False
                )
                if is_excl:
                    if dup_act == "skip" or info.get("is_duplicate"):
                        totals["skipped"] += 1
                    continue
                if pol_act == "trash" or dup_act == "trash":
                    totals["trashed"] += 1
                elif dup_act == "stage":
                    totals["staged"] += 1
                else:
                    totals["moved"] += 1
            elif isinstance(node, dict):
                _walk(node)

    _walk(plan)
    return totals


def format_plan_tree(plan: dict) -> tuple[list[str], int, int]:
    """Render a sorting plan as indented folder/file lines for humans.

    Returns the lines plus the number of non-disposal moved files and folders in
    the plan (staged/trashed duplicates are rendered with explicit tags and not
    inflated into moved folder/file counts).
    """
    lines: list[str] = []
    counts = {"files": 0, "folders": 0}

    def _is_file(node) -> bool:
        return node is None or (
            isinstance(node, dict)
            and (node.get("__type__") == "file" or node.get("node_type") == "file")
        )

    def _walk(tree: dict, depth: int) -> None:
        indent = "  " * depth
        for name in sorted(k for k in tree if not str(k).startswith("__")):
            node = tree[name]
            if _is_file(node):
                info = node or {}
                is_excl = bool(
                    info.get("is_excluded")
                    or info.get("excluded")
                    or info.get("is_checked") is False
                )
                dup_action = str(info.get("duplicate_action") or "").strip().lower()
                policy_action = str(info.get("policy_action") or "").strip()
                target = info.get("target_filename") or name
                source = info.get("relative_source") or info.get("source_path")
                if is_excl:
                    if dup_action == "skip" or info.get("is_duplicate"):
                        lines.append(f"{indent}{source or target}  [DUPLICATE: skip]")
                    continue

                if policy_action.lower() == "trash" or dup_action == "trash":
                    lines.append(f"{indent}{source or target}  [DUPLICATE: trash]")
                    continue
                if dup_action == "stage":
                    lines.append(f"{indent}{source or target}  [DUPLICATE: stage]")
                    continue

                counts["files"] += 1
                renamed = source and Path(str(source)).name != target
                action_suffix = f"  [ACTION: {policy_action}]" if policy_action else ""
                base_line = (
                    f"{indent}{target}  (was {source})"
                    if renamed
                    else f"{indent}{target}"
                )
                lines.append(f"{base_line}{action_suffix}")
            elif isinstance(node, dict):
                sub_before = len(lines)
                files_before = counts["files"]
                folders_before = counts["folders"]
                header_idx = len(lines)
                lines.append(f"{indent}{name}/")
                _walk(node, depth + 1)
                if len(lines) == sub_before + 1:
                    lines.pop(header_idx)
                elif (
                    counts["files"] > files_before or counts["folders"] > folders_before
                ):
                    counts["folders"] += 1

    if isinstance(plan, dict):
        _walk(plan, 0)
    return lines, counts["files"], counts["folders"]
