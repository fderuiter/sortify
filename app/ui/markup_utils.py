"""Literal square-bracket badge helpers and inspector markup formatting for Textual widgets."""

import os
from collections.abc import Mapping
from typing import Any

from textual.content import Content


def escape_textual_markup(value: object) -> str:
    """Escape square brackets so Textual markup renders badges and dynamic text literally."""
    return str(value).replace("[", "\\[")


def literal_content(text: str) -> Content:
    """Wrap plain text in ``textual.content.Content`` so brackets render without markup parsing."""
    return Content(text)


def format_labeled_markup_line(
    label: str, value: object, *, label_style: str = "bold"
) -> str:
    """Format a styled ``Label: value`` markup line while escaping brackets in ``value``."""
    return f"[{label_style}]{label}:[/{label_style}] {escape_textual_markup(value)}"


def format_file_inspector_lines(
    *,
    key: object,
    path_display: object,
    folder: object,
    locked: object,
    rating: object,
    info: Mapping[str, Any],
    lock_error: str | None = None,
    is_collision: bool = False,
    conflict_policy: object = "rename",
) -> list[str]:
    """Build Rich/Textual markup lines for a file node in ``#meta-details`` with escaped values."""
    lines: list[str] = [
        format_labeled_markup_line("File", key, label_style="bold accent"),
        format_labeled_markup_line("Path", path_display),
        format_labeled_markup_line("Target Folder", folder),
    ]

    target_fn = info.get("target_filename")
    if target_fn:
        lines.append(format_labeled_markup_line("Target Filename", target_fn))

    explicit_rename_src = info.get("rename_source")
    raw_src_ref = str(
        info.get("source_path") or info.get("filepath") or key or ""
    ).replace("\\", "/")
    if "::" in raw_src_ref:
        raw_src_ref = raw_src_ref.split("::", 1)[1]
    src_basename = os.path.basename(raw_src_ref)

    rename_src_display: str | None = None
    if explicit_rename_src and str(explicit_rename_src).strip():
        rename_src_display = str(explicit_rename_src).strip()
    elif target_fn and src_basename and str(target_fn) != src_basename:
        routed_by_val = str(info.get("routed_by") or "").strip().lower()
        if info.get("is_locked") and info.get("confirmed"):
            rename_src_display = "Manual"
        elif info.get("is_confirmed") and routed_by_val not in (
            "",
            "clustering",
            "historical",
            "keyword",
            "learned",
            "extension",
        ):
            rename_src_display = "Policy Rule"
        else:
            rename_src_display = "Template"

    if rename_src_display:
        lines.append(format_labeled_markup_line("Rename Source", rename_src_display))

    lines.append(format_labeled_markup_line("Locked", locked))
    lines.append(format_labeled_markup_line("ML Rating", rating))

    if lock_error:
        lines.append(
            format_labeled_markup_line(
                "Lock Warning", lock_error, label_style="bold error"
            )
        )

    routing_src = info.get("routed_by")
    if routing_src:
        lines.append(format_labeled_markup_line("Routing Source", routing_src))

    pol_prio = info.get("policy_priority")
    if pol_prio is not None:
        lines.append(format_labeled_markup_line("Policy Priority", pol_prio))

    pol_action = info.get("policy_action")
    if pol_action:
        lines.append(format_labeled_markup_line("Policy Action", pol_action))

    match_val = info.get("match") or info.get("keyword")
    if match_val and str(match_val).strip().lower() != "none":
        match_label = (
            "Cluster Keywords"
            if str(routing_src or "").strip().lower() == "clustering"
            else "Matched Rule"
        )
        lines.append(format_labeled_markup_line(match_label, match_val))

    cat = info.get("category") or info.get("jev_category")
    if cat:
        lines.append(format_labeled_markup_line("Category", cat))

    sens_rating = info.get("sensitivity_rating")
    if sens_rating is not None:
        lines.append(format_labeled_markup_line("Sensitivity Rating", sens_rating))

    sens_score = info.get("sensitivity_score")
    if sens_score is not None:
        try:
            formatted_sens: object = f"{float(sens_score):.2f}"
        except (ValueError, TypeError):
            formatted_sens = sens_score
        lines.append(format_labeled_markup_line("Sensitivity Score", formatted_sens))

    arch_prio = info.get("archival_priority")
    if arch_prio is not None:
        arch_str = (
            f"P{arch_prio}"
            if isinstance(arch_prio, int)
            or (
                isinstance(arch_prio, str)
                and not str(arch_prio).upper().startswith("P")
            )
            else str(arch_prio).upper()
        )
        lines.append(format_labeled_markup_line("Archival Priority", arch_str))

    arch_score = info.get("archival_priority_score")
    if arch_score is not None:
        try:
            formatted_arch: object = f"{float(arch_score):.2f}"
        except (ValueError, TypeError):
            formatted_arch = arch_score
        lines.append(
            format_labeled_markup_line("Archival Priority Score", formatted_arch)
        )

    conf = info.get("confidence")
    if conf is not None:
        try:
            formatted_conf: object = f"{float(conf):.2%}"
        except (ValueError, TypeError):
            formatted_conf = conf
        lines.append(format_labeled_markup_line("Confidence", formatted_conf))

    if is_collision:
        pol_str = str(conflict_policy or "rename").upper()
        lines.append(
            format_labeled_markup_line(
                "Collision Indicator",
                f"[COLLISION: {pol_str}]",
                label_style="bold warning",
            )
        )

    return lines


def format_folder_inspector_lines(*, key: object, folder: object) -> list[str]:
    """Build Rich/Textual markup lines for a folder node in ``#meta-details`` with escaped values."""
    return [
        format_labeled_markup_line("Folder Category", key, label_style="bold accent"),
        format_labeled_markup_line("Relative Path", folder),
    ]
