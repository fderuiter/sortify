"""Output formatting utilities for CLI subcommands."""

import json
import os
from typing import Any, Dict, List, Optional


def extract_plan_rows(plan: Any) -> List[Dict[str, Any]]:
    """Extract normalized row objects from a sorting plan dictionary."""
    rows: List[Dict[str, Any]] = []
    if not isinstance(plan, dict):
        return rows

    plan_dict = plan
    if "plan" in plan and isinstance(plan["plan"], dict):
        plan_dict = plan["plan"]

    def _process_file_node(src_key: str, node: Any, default_category: str = ""):
        if isinstance(node, dict):
            orig_path = (
                node.get("relative_source")
                or node.get("source_path")
                or node.get("original_path")
                or src_key
            )
            if isinstance(orig_path, str):
                if orig_path.startswith("../"):
                    orig_path = orig_path[3:]
                elif orig_path.startswith("..\\"):
                    orig_path = orig_path[3:]

            target_path = node.get("target_filename") or node.get("target_path")
            category = (
                node.get("category")
                or node.get("jev_category")
                or default_category
            )

            if not target_path:
                if category and category != "Uncategorized":
                    target_path = f"{category}/{os.path.basename(str(orig_path))}"
                else:
                    target_path = str(orig_path)

            if not category and target_path:
                tp_str = str(target_path).replace("\\", "/")
                parts = [p for p in tp_str.split("/") if p]
                if parts and parts[0].endswith(":"):
                    parts = parts[1:]
                if len(parts) > 1:
                    category = parts[0]
            if not category:
                category = "Uncategorized"

            conf_val = node.get("confidence")
        else:
            orig_path = src_key
            target_path = str(node)
            category = default_category
            if not category and target_path:
                tp_str = str(target_path).replace("\\", "/")
                parts = [p for p in tp_str.split("/") if p]
                if parts and parts[0].endswith(":"):
                    parts = parts[1:]
                if len(parts) > 1:
                    category = parts[0]
            if not category:
                category = "Uncategorized"
            conf_val = None

        rows.append(
            {
                "original_path": str(orig_path),
                "target_path": str(target_path),
                "category": str(category),
                "confidence": conf_val,
            }
        )

    for key, value in plan_dict.items():
        if isinstance(value, dict):
            is_category_dict = False
            first_val = next(iter(value.values()), None)
            if isinstance(first_val, dict) and (
                "__type__" in first_val
                or "relative_source" in first_val
                or "source_path" in first_val
                or "target_filename" in first_val
                or "extraction_status" in first_val
                or "routed_by" in first_val
            ):
                is_category_dict = True

            if is_category_dict:
                for sub_key, sub_node in value.items():
                    _process_file_node(sub_key, sub_node, default_category=key)
            else:
                _process_file_node(key, value)
        else:
            _process_file_node(key, value)

    return rows


def format_table(rows: List[Dict[str, Any]]) -> str:
    """Format sorting plan rows into an ASCII table string."""
    if not rows:
        return "No files to display."

    headers = ["Original Path", "Predicted Category", "Target Path", "Confidence"]

    formatted_rows: List[List[str]] = []
    for r in rows:
        conf_val = r["confidence"]
        if conf_val is None:
            conf_str = "N/A"
        elif isinstance(conf_val, (int, float)):
            conf_str = f"{conf_val * 100:.0f}%" if conf_val <= 1.0 else f"{conf_val:.0f}%"
        else:
            conf_str = str(conf_val)

        formatted_rows.append(
            [
                r["original_path"],
                r["category"],
                r["target_path"],
                conf_str,
            ]
        )

    col_widths = [len(h) for h in headers]
    for row in formatted_rows:
        for idx, val in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(val))

    border = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_line = (
        "|"
        + "|".join(
            f" {headers[idx].ljust(col_widths[idx])} " for idx in range(len(headers))
        )
        + "|"
    )

    lines = [border, header_line, border]
    for row in formatted_rows:
        row_line = (
            "|"
            + "|".join(
                f" {row[idx].ljust(col_widths[idx])} " for idx in range(len(row))
            )
            + "|"
        )
        lines.append(row_line)
    lines.append(border)

    return "\n".join(lines)


def format_text(rows: List[Dict[str, Any]]) -> str:
    """Format sorting plan rows as line-delimited tab-separated mappings."""
    lines = [f"{r['original_path']}\t{r['target_path']}" for r in rows]
    return "\n".join(lines)


def format_output(
    data: Any,
    fmt: str = "table",
    plan: Optional[Dict[str, Any]] = None,
) -> str:
    """Format sorting plan data into specified output format (table, text, or json)."""
    fmt_clean = (fmt or "table").lower().strip()
    if fmt_clean == "json":
        return json.dumps(data, indent=2)

    plan_source = plan if plan is not None else data
    if (
        isinstance(plan_source, dict)
        and "plan" in plan_source
        and isinstance(plan_source["plan"], dict)
    ):
        plan_source = plan_source["plan"]

    rows = extract_plan_rows(plan_source)

    if fmt_clean == "text":
        return format_text(rows)
    return format_table(rows)
