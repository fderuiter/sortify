"""Human-readable rendering of sorting plans for the CLI and TUI."""

from pathlib import Path
from typing import Any


def _is_file_node(node: Any) -> bool:
    return node is None or (
        isinstance(node, dict)
        and (node.get("__type__") == "file" or node.get("node_type") == "file")
    )


def count_plan_actions(plan: dict) -> dict[str, int]:
    """Count planned folder moves alongside staged, trashed, and skipped duplicates."""
    totals = {
        "moved": 0,
        "staged": 0,
        "trashed": 0,
        "skipped": 0,
        "folders": 0,
    }

    def _walk_counts(tree: dict, is_root: bool = False) -> bool:
        has_regular_files = False
        for name in sorted(k for k in tree if not str(k).startswith("__")):
            node = tree[name]
            if _is_file_node(node):
                info = node or {}
                dup_act = str(info.get("duplicate_action") or "").strip().lower()
                pol_act = str(info.get("policy_action") or "").strip().lower()
                if info.get("is_excluded") or dup_act == "skip":
                    totals["skipped"] += 1
                elif dup_act == "trash" or pol_act == "trash":
                    totals["trashed"] += 1
                elif dup_act == "stage":
                    totals["staged"] += 1
                else:
                    totals["moved"] += 1
                    has_regular_files = True
            elif isinstance(node, dict):
                child_has_regular = _walk_counts(node, is_root=False)
                if child_has_regular:
                    totals["folders"] += 1
                    has_regular_files = True
        return has_regular_files

    if isinstance(plan, dict):
        _walk_counts(plan, is_root=True)
    return totals


def format_plan_tree(plan: dict) -> tuple[list[str], int, int]:
    """Render a sorting plan as indented folder/file lines for humans.

    Returns the lines plus the number of regular moved files and target folders.
    Staged and trash-staged duplicate copies are listed in dedicated staging
    sections so they are not conflated with regular destination folder moves.
    """
    lines: list[str] = []
    counts = {"files": 0, "folders": 0}
    staged_lines: list[str] = []
    trashed_lines: list[str] = []

    def _format_leaf_line(name: str, info: dict, indent: str) -> str:
        target = info.get("target_filename") or name
        source = info.get("relative_source") or info.get("source_path")
        renamed = source and Path(str(source)).name != target
        policy_action = str(info.get("policy_action") or "").strip()
        action_suffix = f"  [ACTION: {policy_action}]" if policy_action else ""
        base_line = (
            f"{indent}{target}  (was {source})" if renamed else f"{indent}{target}"
        )
        return f"{base_line}{action_suffix}"

    def _walk(tree: dict, depth: int) -> list[str]:
        branch_lines: list[str] = []
        indent = "  " * depth
        for name in sorted(k for k in tree if not str(k).startswith("__")):
            node = tree[name]
            if _is_file_node(node):
                info = node or {}
                dup_act = str(info.get("duplicate_action") or "").strip().lower()
                pol_act = str(info.get("policy_action") or "").strip().lower()
                src_disp = (
                    info.get("source_path")
                    or info.get("relative_source")
                    or info.get("target_filename")
                    or name
                )
                if info.get("is_excluded") or dup_act == "skip":
                    continue
                if dup_act == "trash" or pol_act == "trash":
                    pol_suffix = "  [ACTION: trash]" if pol_act == "trash" else ""
                    trashed_lines.append(
                        f"  {src_disp}  [DUPLICATE: trash]{pol_suffix}"
                    )
                    continue
                if dup_act == "stage":
                    staged_lines.append(f"  {src_disp}  [DUPLICATE: stage]")
                    continue
                counts["files"] += 1
                branch_lines.append(_format_leaf_line(name, info, indent))
            elif isinstance(node, dict):
                child_lines = _walk(node, depth + 1)
                if child_lines:
                    counts["folders"] += 1
                    branch_lines.append(f"{indent}{name}/")
                    branch_lines.extend(child_lines)
        return branch_lines

    if isinstance(plan, dict):
        lines.extend(_walk(plan, 0))
    if staged_lines:
        lines.append(".sortify_duplicates/")
        lines.extend(staged_lines)
    if trashed_lines:
        lines.append(".sortify_duplicates/_trash/")
        lines.extend(trashed_lines)
    return lines, counts["files"], counts["folders"]
