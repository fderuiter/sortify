"""Side-by-side path comparisons with the changed segments highlighted."""

import difflib
import os
from typing import Any, Dict, List, Optional, Tuple

from rich.table import Table
from rich.text import Text


def _diff_highlight_paths(src_path: str, tgt_path: str) -> Tuple[Text, Text]:
    """Compute contrasting color diff highlighting for two path strings."""
    if not src_path and not tgt_path:
        return Text("(None)", style="dim"), Text("(None)", style="dim")

    src_clean = src_path or "(None)"
    tgt_clean = tgt_path or "(None)"

    if src_clean == tgt_clean:
        return Text(src_clean, style="dim white"), Text(tgt_clean, style="dim white")

    matcher = difflib.SequenceMatcher(None, src_clean, tgt_clean)
    src_formatted = Text()
    tgt_formatted = Text()

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            src_formatted.append(src_clean[i1:i2])
            tgt_formatted.append(tgt_clean[j1:j2])
        elif tag == "replace":
            src_formatted.append(src_clean[i1:i2], style="bold red")
            tgt_formatted.append(tgt_clean[j1:j2], style="bold green")
        elif tag == "delete":
            src_formatted.append(src_clean[i1:i2], style="bold red")
        elif tag == "insert":
            tgt_formatted.append(tgt_clean[j1:j2], style="bold green")

    return src_formatted, tgt_formatted


def format_side_by_side_path_comparison(
    source_path: str,
    target_path: str,
    base_dir: Optional[str] = None,
) -> Table:
    """Render split side-by-side columns comparing source and target paths with contrasting color diffs."""
    table = Table(
        expand=True,
        show_header=True,
        header_style="bold cyan",
        box=None,
    )
    table.add_column("Source Path (Current)", style="bold", ratio=1)
    table.add_column("Target Path (Proposed)", style="bold", ratio=1)

    src_norm = source_path.replace("\\", "/") if source_path else ""
    tgt_norm = target_path.replace("\\", "/") if target_path else ""

    base_norm = base_dir.replace("\\", "/") if base_dir else ""
    if base_norm and not base_norm.endswith("/"):
        base_norm += "/"

    src_rel = (
        src_norm[len(base_norm) :]
        if base_norm and src_norm.lower().startswith(base_norm.lower())
        else src_norm
    )
    tgt_rel = (
        tgt_norm[len(base_norm) :]
        if base_norm and tgt_norm.lower().startswith(base_norm.lower())
        else tgt_norm
    )

    src_rel_fmt, tgt_rel_fmt = _diff_highlight_paths(src_rel, tgt_rel)
    table.add_row(src_rel_fmt, tgt_rel_fmt)

    if base_norm and (src_norm != src_rel or tgt_norm != tgt_rel):
        src_full_fmt, tgt_full_fmt = _diff_highlight_paths(src_norm, tgt_norm)
        table.add_row(src_full_fmt, tgt_full_fmt)

    return table


def format_folder_path_comparison(
    files_info: List[Dict[str, Any]],
    base_dir: Optional[str] = None,
    max_items: int = 8,
) -> Table:
    """Render side-by-side path comparison table for items in a folder subtree."""
    table = Table(
        expand=True,
        show_header=True,
        header_style="bold cyan",
        box=None,
    )
    table.add_column("Source Path (Current)", style="bold", ratio=1)
    table.add_column("Target Path (Proposed)", style="bold", ratio=1)

    if not files_info:
        table.add_row(
            Text("No files in folder", style="dim"),
            Text("No files in folder", style="dim"),
        )
        return table

    for fi in files_info[:max_items]:
        src_path = fi.get("filepath", "")
        key = fi.get("key") or fi.get("target_filename") or ""
        folder = fi.get("folder", "")
        target_fn = fi.get("target_filename") or key
        tgt_rel = (
            os.path.join(folder, target_fn).replace("\\", "/")
            if folder and folder != "(Root)"
            else target_fn
        )

        src_rel = src_path
        if base_dir and src_path:
            base_norm = base_dir.replace("\\", "/").rstrip("/") + "/"
            src_norm = src_path.replace("\\", "/")
            if src_norm.lower().startswith(base_norm.lower()):
                src_rel = src_norm[len(base_norm) :]

        src_fmt, tgt_fmt = _diff_highlight_paths(src_rel, tgt_rel)
        table.add_row(src_fmt, tgt_fmt)

    if len(files_info) > max_items:
        remaining = len(files_info) - max_items
        table.add_row(
            Text(f"... and {remaining} more items", style="dim italic"),
            Text(f"... and {remaining} more items", style="dim italic"),
        )

    return table
