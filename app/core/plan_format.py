"""Human-readable rendering of sorting plans for the CLI and TUI."""

from pathlib import Path


def format_plan_tree(plan: dict) -> tuple[list[str], int, int]:
    """Render a sorting plan as indented folder/file lines for humans.

    Returns the lines plus the number of files and folders in the plan.
    """
    lines: list[str] = []
    counts = {"files": 0, "folders": 0}

    def _is_file(node) -> bool:
        return node is None or (
            isinstance(node, dict) and node.get("__type__") == "file"
        )

    def _walk(tree: dict, depth: int) -> None:
        indent = "  " * depth
        for name in sorted(k for k in tree if not str(k).startswith("__")):
            node = tree[name]
            if _is_file(node):
                counts["files"] += 1
                node = node or {}
                target = node.get("target_filename") or name
                source = node.get("relative_source") or node.get("source_path")
                renamed = source and Path(str(source)).name != target
                lines.append(
                    f"{indent}{target}  (was {source})"
                    if renamed
                    else f"{indent}{target}"
                )
            elif isinstance(node, dict):
                counts["folders"] += 1
                lines.append(f"{indent}{name}/")
                _walk(node, depth + 1)

    if isinstance(plan, dict):
        _walk(plan, 0)
    return lines, counts["files"], counts["folders"]
