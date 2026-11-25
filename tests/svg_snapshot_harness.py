"""Reusable SVG snapshot sanitization and comparison harness for Textual TUI tests."""

from __future__ import annotations

import html
import os
import re

import pytest


def _fmt_svg_num(val: float) -> str:
    """Format a Rich SVG coordinate/dimension float without trailing .0."""
    formatted = f"{val:.1f}"
    return formatted[:-2] if formatted.endswith(".0") else formatted


def _normalize_dummy_drive_geometry(svg: str) -> str:
    """Adjust Rich SVG textLength and row background rects when stripping Windows drive prefixes on /dummy paths."""
    line_to_rect_y = {
        m.group(1): m.group(2)
        for m in re.finditer(
            r'<clipPath id="terminal-test-line-(\d+)">\s*<rect [^>]*?\by="([\d.]+)"',
            svg,
        )
    }
    rect_adjustments: list[tuple[str, str, float, float]] = []

    def _replace_text(match: re.Match[str]) -> str:
        attrs, body = match.group(1), match.group(2)
        if not re.search(r"[A-Za-z]:[/\\]+dummy", body):
            return match.group(0)
        new_body = re.sub(r"(?:[A-Za-z]:)?[/\\]+dummy", "/dummy", body)
        old_cells = len(html.unescape(body))
        new_cells = len(html.unescape(new_body))
        delta_cells = old_cells - new_cells
        if delta_cells <= 0 or old_cells <= 0:
            return f"<text{attrs}>{new_body}</text>"

        tl_match = re.search(r'\btextLength="([\d.]+)"', attrs)
        x_match = re.search(r'\bx="([\d.]+)"', attrs)
        line_match = re.search(r'clip-path="url\(#terminal-test-line-(\d+)\)"', attrs)
        if tl_match:
            old_tl = float(tl_match.group(1))
            cell_w = round(old_tl / old_cells, 2)
            new_tl = round(old_tl - delta_cells * cell_w, 1)
            attrs = (
                attrs[: tl_match.start(1)]
                + _fmt_svg_num(new_tl)
                + attrs[tl_match.end(1) :]
            )
            if x_match and line_match and line_match.group(1) in line_to_rect_y:
                rect_adjustments.append(
                    (
                        line_to_rect_y[line_match.group(1)],
                        x_match.group(1),
                        old_tl,
                        new_tl,
                    )
                )
        return f"<text{attrs}>{new_body}</text>"

    svg = re.sub(r"<text([^>]*)>(.*?)</text>", _replace_text, svg)

    for rect_y, x_str, old_tl, new_tl in rect_adjustments:
        x_val = float(x_str)
        old_tl_str = _fmt_svg_num(old_tl)
        old_next_x_str = _fmt_svg_num(x_val + old_tl)
        pair_pattern = re.compile(
            r'(<rect fill="([^"]+)" x="'
            + re.escape(x_str)
            + r'" y="'
            + re.escape(rect_y)
            + r'" width=")'
            + re.escape(old_tl_str)
            + r'("[^>]*/>)'
            + r'(<rect fill="\2" x=")'
            + re.escape(old_next_x_str)
            + r'(" y="'
            + re.escape(rect_y)
            + r'" width=")([\d.]+)("[^>]*/>)'
        )
        svg = pair_pattern.sub(
            lambda m, xv=x_val, ot=old_tl, nt=new_tl: (
                f"{m.group(1)}{_fmt_svg_num(nt)}{m.group(3)}"
                f"{m.group(4)}{_fmt_svg_num(xv + nt)}{m.group(5)}"
                f"{_fmt_svg_num(float(m.group(6)) + (ot - nt))}{m.group(7)}"
            ),
            svg,
            count=1,
        )

    return svg


def sanitize_svg(svg: str) -> str:
    """Sanitize and canonicalize dynamic elements in Textual rendered SVG output.

    Normalizes Rich's auto-generated unique element ID prefixes, clock timestamps,
    Windows path separators, drive letters, filters out unused CSS style declarations,
    and sorts active style declarations to ensure deterministic baseline snapshot
    comparisons across operating systems and test execution order.
    """
    svg = svg.replace("\r\n", "\n")
    svg = re.sub(r"terminal-[a-zA-Z0-9]+-", "terminal-test-", svg)
    svg = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "00:00:00", svg)
    # Normalize cursor blink dim fill color #495259 to standard text fill color #a0a3a6
    svg = svg.replace("fill: #495259", "fill: #a0a3a6")

    # Normalize line heights across platforms
    svg = re.sub(r"line-height:\s*[\d.]+px;?", "line-height: 24.4px;", svg)
    svg = re.sub(r'height="24\.\d+"', 'height="24.65"', svg)

    # Normalize Windows drive letters, backslashes, and HTML entities in test paths
    svg = (
        svg.replace("&#92;", "\\")
        .replace("&bsol;", "\\")
        .replace("&#x5C;", "\\")
        .replace("&#x5c;", "\\")
        .replace("&#47;", "/")
        .replace("&sol;", "/")
        .replace("&#x2F;", "/")
        .replace("&#x2f;", "/")
    )
    svg = re.sub(r"<tspan[^>]*>[A-Za-z]:</tspan>", "", svg)
    svg = _normalize_dummy_drive_geometry(svg)
    svg = re.sub(r"(?:[A-Za-z]:)?[/\\]+dummy", "/dummy", svg)
    svg = re.sub(
        r"/dummy([^<\"]*)",
        lambda m: "/dummy" + m.group(1).replace("\\", "/"),
        svg,
    )
    svg = re.sub(r"\b[A-Za-z]:[/\\]", "/", svg)
    svg = svg.replace("\\", "/")

    style_match = re.search(r"<style>(.*?)</style>", svg, re.DOTALL)
    if style_match:
        css_text = style_match.group(1)
        rules = re.findall(r"\.(terminal-test-r\w+)\s*\{(.*?)\}", css_text)
        if rules:
            body_without_style = svg.replace(style_match.group(0), "")
            used_rules = [
                (cls, body)
                for cls, body in rules
                if re.search(r"\b" + re.escape(cls) + r"\b", body_without_style)
            ]

            style_map: dict[str, str] = {}
            sorted_unique_styles = sorted({body.strip() for _, body in used_rules})

            for old_class, style_body in used_rules:
                style_index = sorted_unique_styles.index(style_body.strip())
                style_map[old_class] = f"terminal-test-c{style_index}"

            new_css_lines = [
                f"    .terminal-test-c{idx} {{ {body} }}"
                for idx, body in enumerate(sorted_unique_styles)
            ]
            new_css = "\n" + "\n".join(new_css_lines) + "\n    "
            svg = svg.replace(css_text, new_css)

            for old_class, new_class in sorted(
                style_map.items(), key=lambda x: len(x[0]), reverse=True
            ):
                svg = re.sub(r"\b" + old_class + r"\b", new_class, svg)

    return svg


def verify_svg_snapshot(
    snapshot_name: str,
    actual_svg: str,
    *,
    snapshot_dir: str | os.PathLike[str],
    update_snapshots: bool | None = None,
) -> None:
    """Compare *actual_svg* against ``<snapshot_dir>/<snapshot_name>.svg`` and manage ``.svg.actual`` artifacts."""
    snap_dir_str = os.fspath(snapshot_dir)
    os.makedirs(snap_dir_str, exist_ok=True)
    snapshot_path = os.path.join(snap_dir_str, f"{snapshot_name}.svg")
    actual_path = snapshot_path + ".actual"
    sanitized_actual = sanitize_svg(actual_svg)

    should_update = (
        os.environ.get("UPDATE_SNAPSHOTS") == "1"
        if update_snapshots is None
        else update_snapshots
    )

    if not os.path.exists(snapshot_path) or should_update:
        with open(snapshot_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(sanitized_actual)
        if os.path.exists(actual_path):
            os.unlink(actual_path)
        if not should_update:
            pytest.fail(
                f"SVG snapshot '{snapshot_name}' generated for the first time at {snapshot_path}. Run again to verify."
            )
        return

    with open(snapshot_path, encoding="utf-8", newline="\n") as f:
        expected_svg = f.read().replace("\r\n", "\n")

    if sanitized_actual != expected_svg:
        with open(actual_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(sanitized_actual)
    elif os.path.exists(actual_path):
        os.unlink(actual_path)

    assert sanitized_actual == expected_svg, (
        f"SVG visual snapshot mismatch for '{snapshot_name}'. Set UPDATE_SNAPSHOTS=1 to re-baseline."
    )
