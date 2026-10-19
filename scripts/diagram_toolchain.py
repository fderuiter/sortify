"""Schema-Driven Diagram Compiler and External CLI Toolchain.

Compiles declarative diagram schemas into canonical Mermaid (.mmd) files,
validates syntax, and renders SVG diagram assets using @mermaid-js/mermaid-cli.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from app.core.diagram_schema import BaseDiagramSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs/assets/diagrams"
CACHE_FILE_NAME = ".build_cache.json"


_BROWSER_AVAILABLE_CACHE: Optional[bool] = None


def reset_browser_cache() -> None:
    """Reset cached browser availability result (mainly for testing)."""
    global _BROWSER_AVAILABLE_CACHE
    _BROWSER_AVAILABLE_CACHE = None


def is_browser_available(
    mmdc_cmd: Optional[List[str]] = None, force_check: bool = False
) -> bool:
    """Probe whether mmdc is present and capable of launching a browser engine."""
    global _BROWSER_AVAILABLE_CACHE
    if not force_check and _BROWSER_AVAILABLE_CACHE is not None:
        return _BROWSER_AVAILABLE_CACHE

    cmd = mmdc_cmd or find_mmdc_executable(verify_browser=False)
    if not cmd:
        _BROWSER_AVAILABLE_CACHE = False
        return False

    import tempfile

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            test_mmd = Path(tmpdir) / "probe.mmd"
            test_svg = Path(tmpdir) / "probe.svg"
            test_mmd.write_text("graph TD\n  A --> B\n", encoding="utf-8")
            probe_cmd = cmd + [
                "-i",
                str(test_mmd),
                "-o",
                str(test_svg),
                "-e",
                "svg",
                "-b",
                "white",
            ]
            res = subprocess.run(
                probe_cmd, capture_output=True, text=True, check=False, timeout=60
            )
            if (
                res.returncode == 0
                and test_svg.exists()
                and test_svg.stat().st_size > 0
            ):
                _BROWSER_AVAILABLE_CACHE = True
                return True
            else:
                _BROWSER_AVAILABLE_CACHE = False
                return False
    except Exception:
        _BROWSER_AVAILABLE_CACHE = False
        return False


def get_sha256(content: str) -> str:
    """Compute SHA-256 hash of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def find_mmdc_executable(verify_browser: bool = True) -> Optional[List[str]]:
    """Determine the command prefix required to run mmdc (mermaid-cli).

    If verify_browser is True, probes whether the browser engine can launch successfully.
    """
    mmdc_path = shutil.which("mmdc")
    candidate: Optional[List[str]] = None
    if mmdc_path:
        candidate = [mmdc_path]
    else:
        npx_path = shutil.which("npx")
        if npx_path:
            candidate = [
                npx_path,
                "--no-install",
                "-p",
                "@mermaid-js/mermaid-cli",
                "mmdc",
            ]

    if not candidate:
        return None

    if verify_browser:
        if is_browser_available(candidate):
            return candidate
        return None

    return candidate


def validate_mermaid_syntax(mmd_content: str) -> List[str]:
    """Validate Mermaid diagram syntax using pure Python rules.

    Returns a list of error detail strings with line numbers. An empty list indicates valid syntax.
    """
    if not mmd_content or not mmd_content.strip():
        return ["Mermaid diagram content is empty."]

    lines = mmd_content.splitlines()
    code_lines = []

    for idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        # Strip inline comments
        code_part = stripped.split("%%")[0].strip()
        if code_part:
            code_lines.append((idx, raw_line, code_part))

    if not code_lines:
        return ["Mermaid diagram contains only comments."]

    valid_types = {
        "flowchart",
        "graph",
        "sequenceDiagram",
        "stateDiagram",
        "stateDiagram-v2",
        "classDiagram",
        "classDiagram-v2",
        "erDiagram",
        "gantt",
        "pie",
        "gitGraph",
        "mindmap",
        "timeline",
        "architecture",
        "architecture-beta",
        "C4Context",
        "zenuml",
        "kanban",
        "sankey-beta",
        "block-beta",
    }

    # Find first non-directive header line
    header_entry = None
    for entry in code_lines:
        if not entry[2].startswith("%%{"):
            header_entry = entry
            break

    if not header_entry:
        return ["Missing Mermaid diagram header definition."]

    h_idx, h_raw, h_code = header_entry
    first_word = h_code.split()[0]

    if first_word not in valid_types:
        return [
            f"Line {h_idx}: unknown diagram type '{first_word}'. Expected one of: {', '.join(sorted(valid_types))}."
        ]

    errors = []
    block_depth = 0
    is_sequence = first_word == "sequenceDiagram"
    is_flowchart = first_word in {"flowchart", "graph"}

    for idx, raw_line, code_line in code_lines:
        # Quote and bracket balance checking
        in_quotes = False
        escaped = False
        bracket_counts = {"[": 0, "]": 0, "(": 0, ")": 0, "{": 0, "}": 0}

        for char in code_line:
            if char == '"' and not escaped:
                in_quotes = not in_quotes
            elif not in_quotes:
                if char in bracket_counts:
                    bracket_counts[char] += 1
            escaped = (char == "\\") and not escaped

        if in_quotes:
            errors.append(f"Line {idx}: Unclosed double quote in '{raw_line.strip()}'.")

        if (
            bracket_counts["["] != bracket_counts["]"]
            or bracket_counts["("] != bracket_counts[")"]
            or bracket_counts["{"] != bracket_counts["}"]
        ):
            errors.append(
                f"Line {idx}: unbalanced brackets in '{raw_line.strip()}' "
                f"(Square: {bracket_counts['[']}/{bracket_counts[']']}, "
                f"Paren: {bracket_counts['(']}/{bracket_counts[')']}, "
                f"Curly: {bracket_counts['{']}/{bracket_counts['}']})."
            )

        # Structural block depth tracking
        tokens = code_line.split()
        if tokens:
            kw = tokens[0]
            if is_flowchart:
                if kw == "subgraph":
                    block_depth += 1
                elif kw == "end":
                    block_depth -= 1
                    if block_depth < 0:
                        errors.append(
                            f"Line {idx}: 'end' statement without matching 'subgraph'."
                        )
                        block_depth = 0
            elif is_sequence:
                if kw in {
                    "subgraph",
                    "opt",
                    "alt",
                    "loop",
                    "par",
                    "rect",
                    "critical",
                    "break",
                }:
                    block_depth += 1
                elif kw == "end":
                    block_depth -= 1
                    if block_depth < 0:
                        errors.append(
                            f"Line {idx}: 'end' statement without matching block start."
                        )
                        block_depth = 0

        # Relationship connection checks
        if any(
            code_line.endswith(arrow)
            for arrow in ["-->", "---", "==>", "-.->", "->>", "--->", "--o", "--x"]
        ):
            errors.append(
                f"Line {idx}: Hanging relationship arrow without target node in '{raw_line.strip()}'."
            )

    if block_depth > 0:
        errors.append(
            f"Unclosed structural block ({block_depth} unclosed block(s) remaining)."
        )

    return errors


def check_no_raw_mermaid_in_docs(docs_dir: Optional[Path] = None) -> bool:
    """Scan docs directory for raw inline ```mermaid code blocks and return False if any remain."""
    import re

    if docs_dir is None:
        docs_dir = REPO_ROOT / "docs"
    elif not docs_dir.is_absolute():
        docs_dir = REPO_ROOT / docs_dir

    if not docs_dir.exists():
        return True

    found_raw_mermaid = False
    for md_path in sorted(docs_dir.rglob("*.md")):
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as e:
            sys.stderr.write(f"Warning: Could not read {md_path}: {e}\n")
            continue

        if re.search(r"```mermaid", content):
            sys.stderr.write(
                f"Verification FAILED: Found raw ```mermaid code block in '{md_path.as_posix()}'. "
                f"All documentation diagrams must be registered schemas and linked as visual assets.\n"
            )
            found_raw_mermaid = True

    return not found_raw_mermaid


def collect_all_specs() -> Dict[str, "BaseDiagramSpec"]:
    """Collect all registered system diagram specifications."""
    from app.core.diagram_schema import (
        BaseDiagramSpec,
        get_all_diagram_specs,
    )

    specs: Dict[str, BaseDiagramSpec] = {}

    for key, spec in get_all_diagram_specs().items():
        specs[spec.id] = spec

    return specs


def load_cache(cache_path: Path) -> dict:
    """Load diagram compilation cache file."""
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache_path: Path, cache_data: dict) -> None:
    """Save diagram compilation cache file."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2)
    except Exception as e:
        sys.stderr.write(f"Warning: Could not save diagram cache: {e}\n")


def generate_fallback_svg(title: str, mmd_content: str) -> str:
    """Generate a clean text-based SVG placeholder with interactive click links when headless rendering is unavailable."""
    escaped_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    lines = [line.strip() for line in mmd_content.splitlines() if line.strip()]
    content_preview = "\n".join(lines[:15])
    escaped_preview = (
        content_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )

    # Extract click directives for interactive SVG hyperlinks
    click_links_svg = []
    y_pos = 320
    for line in lines:
        if line.startswith("click "):
            m = re.match(
                r'^\s*click\s+([A-Za-z0-9_\-]+)(?:\s+tooltip)?\s+"([^"]+)"(?:\s+"([^"]+)")?(?:\s+([^\s"]+))?',
                line,
            )
            if m:
                nid, url, tip, target = m.groups()
                target_attr = f' target="{target}"' if target else ""
                escaped_url = (
                    url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                )
                label_text = f"Node {nid} -> {tip or url}"
                escaped_label = (
                    label_text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                click_links_svg.append(
                    f'    <a href="{escaped_url}"{target_attr} style="color: #2563eb; text-decoration: underline;">'
                    f'<text x="20" y="{y_pos}" font-family="sans-serif" font-size="11" fill="#2563eb">🔗 {escaped_label}</text></a>'
                )
                y_pos += 18

    links_section = "\n".join(click_links_svg)
    total_height = max(400, y_pos + 20)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="800" height="{total_height}" viewBox="0 0 800 {total_height}">
  <rect width="100%" height="100%" fill="#f8fafc" rx="8" stroke="#cbd5e1" stroke-width="2"/>
  <text x="20" y="40" font-family="sans-serif" font-size="18" font-weight="bold" fill="#0f172a">{escaped_title}</text>
  <text x="20" y="70" font-family="sans-serif" font-size="12" fill="#64748b">Mermaid Diagram Specification (Fallback View):</text>
  <foreignObject x="20" y="90" width="760" height="210">
    <pre xmlns="http://www.w3.org/1999/xhtml" style="font-family: monospace; font-size: 12px; color: #334155; background: #ffffff; padding: 12px; border-radius: 6px; border: 1px solid #e2e8f0; overflow: auto; height: 180px;">{escaped_preview}</pre>
  </foreignObject>
{links_section}
</svg>
"""


def parse_and_validate_click_directives(mmd_content: str) -> List[str]:
    """Parse and validate Mermaid click directives within diagram markup."""
    errors = []
    lines = mmd_content.splitlines()
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("click "):
            parts = stripped.split()
            if len(parts) < 2:
                errors.append(
                    f"Line {idx}: Invalid click directive format: '{stripped}'"
                )
                continue

            node_id = parts[1]
            if not node_id:
                errors.append(f"Line {idx}: Missing node ID in click directive")
                continue

            # If this is a tooltip-only click directive (e.g., click node_id tooltip "text"), no URL check needed
            if len(parts) > 2 and parts[2].lower() == "tooltip":
                continue

            # Check for URL in quotes
            m_quotes = re.findall(r'"([^"]*)"', stripped)
            if m_quotes:
                url_candidate = m_quotes[0]
                if url_candidate:
                    try:
                        from app.core.diagram_schema import DiagramNode

                        DiagramNode.validate_url_scheme(url_candidate)
                    except ValueError as ve:
                        errors.append(
                            f"Line {idx}: Unsafe or invalid URL in click directive: {ve}"
                        )

                    # Check relative file path target existence if not an absolute URI scheme or hash-only
                    lower_url = url_candidate.lower()
                    if not (
                        lower_url.startswith(
                            ("http://", "https://", "mailto:", "file:", "#")
                        )
                        or "://" in lower_url
                    ):
                        file_part = url_candidate.split("#")[0].strip()
                        if file_part:
                            target_file = REPO_ROOT / file_part
                            if not target_file.exists():
                                errors.append(
                                    f"Line {idx}: Click directive target file '{file_part}' does not exist relative to REPO_ROOT ({REPO_ROOT})"
                                )

    return errors


def render_diagram_artifact(
    mmdc_cmd: List[str],
    mmd_path: Path,
    out_path: Path,
    output_format: str = "svg",
) -> bool:
    """Invoke external mmdc tool to compile .mmd into visual asset."""
    cmd = mmdc_cmd + [
        "-i",
        str(mmd_path),
        "-o",
        str(out_path),
        "-e",
        output_format,
        "-b",
        "white",
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=60
        )
        if res.returncode != 0:
            sys.stderr.write(
                f"mmdc failed for {mmd_path.name} ({output_format}): {res.stderr}\n"
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"mmdc command timed out for {mmd_path.name} ({output_format}).\n"
        )
        return False
    except Exception as e:
        sys.stderr.write(f"Error executing mmdc command for {mmd_path.name}: {e}\n")
        return False


def build_diagrams(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    force: bool = False,
    verify_only: bool = False,
) -> bool:
    """Compile diagram specifications into .mmd and SVG assets."""
    os.chdir(REPO_ROOT)
    if not output_dir.is_absolute():
        output_dir = REPO_ROOT / output_dir

    output_dir.mkdir(parents=True, exist_ok=True)
    specs = collect_all_specs()

    if not specs:
        print("No diagram specifications registered.")
        return True

    if verify_only:
        mmdc_cmd = None
        has_browser = False
    else:
        mmdc_cmd = find_mmdc_executable(verify_browser=True)
        has_browser = mmdc_cmd is not None and is_browser_available(mmdc_cmd)

        if not has_browser:
            candidate_cmd = find_mmdc_executable(verify_browser=False)
            if candidate_cmd:
                sys.stderr.write(
                    "Warning: mmdc executable found but browser execution environment is unavailable.\n"
                )
            else:
                sys.stderr.write("Warning: mmdc executable not found in PATH.\n")

    cache_path = output_dir / CACHE_FILE_NAME
    cache = load_cache(cache_path)
    new_cache = dict(cache)

    all_success = True
    if verify_only and not check_no_raw_mermaid_in_docs():
        all_success = False

    updated_count = 0

    for spec_id, spec in specs.items():
        mmd_file = output_dir / f"{spec_id}.mmd"
        svg_file = output_dir / f"{spec_id}.svg"

        try:
            mmd_content = spec.to_mermaid()
        except Exception as e:
            sys.stderr.write(
                f"Schema error: Failed to serialize spec '{spec_id}' to Mermaid: {e}\n"
            )
            all_success = False
            continue

        syntax_errors = validate_mermaid_syntax(mmd_content)
        if syntax_errors:
            sys.stderr.write(f"Syntax error in diagram spec '{spec_id}':\n")
            for err in syntax_errors:
                sys.stderr.write(f"  - {err}\n")
            all_success = False
            continue

        current_hash = get_sha256(mmd_content)

        # Validate click directives
        click_errs = parse_and_validate_click_directives(mmd_content)
        if click_errs:
            for ce in click_errs:
                sys.stderr.write(
                    f"Click directive validation error in spec '{spec_id}': {ce}\n"
                )
            all_success = False
            continue

        previous_mmd = (
            mmd_file.read_text(encoding="utf-8") if mmd_file.exists() else None
        )

        # Write or update .mmd file
        with open(mmd_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(mmd_content)

        cached_entry = cache.get(spec_id, {})
        cached_hash = (
            cached_entry.get("sha256") if isinstance(cached_entry, dict) else None
        )

        needs_render = force or cached_hash != current_hash or not svg_file.exists()

        if verify_only:
            # In verification mode, check schema validity and syntax, then headless compilation or fallback notice
            if has_browser and mmdc_cmd:
                temp_svg = output_dir / f".tmp_verify_{spec_id}.svg"
                rendered_ok = render_diagram_artifact(
                    mmdc_cmd, mmd_file, temp_svg, "svg"
                )
                if temp_svg.exists():
                    try:
                        temp_svg.unlink()
                    except Exception:
                        pass

                if not rendered_ok:
                    sys.stderr.write(
                        f"Verification FAILED: Diagram '{spec_id}' failed headless compilation.\n"
                    )
                    all_success = False
                else:
                    print(
                        f"Verified spec '{spec_id}' -> {mmd_file.name} (headless compilation successful)."
                    )
            else:
                # Verification never writes assets: without a browser it used to
                # overwrite rendered SVGs with text fallbacks on every run.
                print(
                    f"Verified spec '{spec_id}' -> {mmd_file.name} (validated syntax, click directives, and schema)."
                )
            continue

        if not needs_render:
            print(f"Skipping cached diagram asset: {spec_id}")
            continue

        updated_count += 1
        print(f"Compiling diagram asset: {spec_id} ...")

        if has_browser and mmdc_cmd:
            if render_diagram_artifact(mmdc_cmd, mmd_file, svg_file, "svg"):
                new_cache[spec_id] = {"sha256": current_hash}
            else:
                sys.stderr.write(
                    f"Warning: Failed visual rendering for '{spec_id}'. Generating fallback SVG.\n"
                )
                fallback_svg = generate_fallback_svg(spec.title, mmd_content)
                with open(svg_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(fallback_svg)
                all_success = False
        elif svg_file.exists():
            # Never replace a rendered diagram with a text fallback.
            if previous_mmd != mmd_content:
                sys.stderr.write(
                    f"Warning: '{spec_id}' changed but mermaid-cli is unavailable, so "
                    f"{svg_file.name} is stale. Install @mermaid-js/mermaid-cli and "
                    "re-run the diagram build.\n"
                )
        else:
            print(
                f"Browser engine unavailable. Creating fallback text-based SVG for '{spec_id}'."
            )
            fallback_svg = generate_fallback_svg(spec.title, mmd_content)
            with open(svg_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(fallback_svg)

    if not verify_only:
        save_cache(cache_path, new_cache)
        print(
            f"Diagram compilation complete. Processed {len(specs)} specs ({updated_count} updated)."
        )

    return all_success


def main():
    """CLI launcher for diagram toolchain compiler and validator."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description="Schema-driven Diagram Toolchain & Visual Asset Compiler"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="build",
        choices=["build", "verify"],
        help="Command to execute: 'build' (default) or 'verify'",
    )
    parser.add_argument(
        "--verify",
        "--check",
        dest="verify_flag",
        action="store_true",
        help="Validate diagram specs and headless CLI compilation",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-rendering of all visual assets ignoring build cache",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Target directory for compiled diagram assets (default: {DEFAULT_OUTPUT_DIR})",
    )

    args = parser.parse_args()

    is_verify = args.command == "verify" or args.verify_flag

    if is_verify:
        print("Executing diagram toolchain verification gate...")
        success = build_diagrams(
            output_dir=args.output_dir, force=args.force, verify_only=True
        )
        if success:
            print(
                "SUCCESS: All diagram specifications validated and passed compilation checks."
            )
            sys.exit(0)
        else:
            sys.stderr.write(
                "ERROR: Diagram specification or compilation validation failed.\n"
            )
            sys.exit(1)
    else:
        print("Executing diagram asset compilation...")
        success = build_diagrams(
            output_dir=args.output_dir, force=args.force, verify_only=False
        )
        if not success:
            sys.stderr.write(
                "Warning: One or more diagram assets had rendering issues.\n"
            )
        sys.exit(0)


if __name__ == "__main__":
    main()
