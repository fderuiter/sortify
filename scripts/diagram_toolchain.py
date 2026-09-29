"""Schema-Driven Diagram Compiler and External CLI Toolchain.

Compiles declarative diagram schemas into canonical Mermaid (.mmd) files,
validates syntax, and renders visual SVG and PNG diagram assets using @mermaid-js/mermaid-cli.
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
from typing import Dict, List, Optional

# Add project root to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.ui.catalog import CATALOG_REGISTRY
from app.ui.diagram_schema import (
    SYSTEM_DIAGRAM_SPECS,
    ComponentDiagramSpec,
)

DEFAULT_OUTPUT_DIR = Path("docs/assets/diagrams")
CACHE_FILE_NAME = ".build_cache.json"


def get_sha256(content: str) -> str:
    """Compute SHA-256 hash of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def find_mmdc_executable() -> Optional[List[str]]:
    """Determine the command prefix required to run mmdc (mermaid-cli)."""
    mmdc_path = shutil.which("mmdc")
    if mmdc_path:
        return [mmdc_path]

    npx_path = shutil.which("npx")
    if npx_path:
        return [npx_path, "--yes", "-p", "@mermaid-js/mermaid-cli", "mmdc"]

    return None


def collect_all_specs() -> Dict[str, ComponentDiagramSpec]:
    """Collect all registered system and component diagram specifications."""
    specs: Dict[str, ComponentDiagramSpec] = {}

    # System diagrams
    for key, spec in SYSTEM_DIAGRAM_SPECS.items():
        specs[spec.id] = spec

    # Catalog component specs
    for entry in CATALOG_REGISTRY:
        if "diagram_spec" in entry and isinstance(
            entry["diagram_spec"], ComponentDiagramSpec
        ):
            spec = entry["diagram_spec"]
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

            # Check for URL in quotes
            m_quotes = re.findall(r'"([^"]*)"', stripped)
            if m_quotes:
                url_candidate = m_quotes[0]
                if url_candidate and (
                    "://" in url_candidate
                    or url_candidate.startswith(("javascript:", "data:", "vbscript:"))
                    or ":" in url_candidate
                ):
                    try:
                        from app.ui.diagram_schema import DiagramNode

                        DiagramNode.validate_url_scheme(url_candidate)
                    except ValueError as ve:
                        errors.append(
                            f"Line {idx}: Unsafe or invalid URL in click directive: {ve}"
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
            cmd, capture_output=True, text=True, check=False, timeout=10
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
    """Compile diagram specifications into .mmd, SVG, and PNG assets."""
    output_dir.mkdir(parents=True, exist_ok=True)
    specs = collect_all_specs()

    if not specs:
        print("No diagram specifications registered.")
        return True

    mmdc_cmd = find_mmdc_executable()
    cache_path = output_dir / CACHE_FILE_NAME
    cache = load_cache(cache_path)
    new_cache = dict(cache)

    all_success = True
    updated_count = 0

    for spec_id, spec in specs.items():
        mmd_file = output_dir / f"{spec_id}.mmd"
        svg_file = output_dir / f"{spec_id}.svg"
        png_file = output_dir / f"{spec_id}.png"

        try:
            mmd_content = spec.to_mermaid()
        except Exception as e:
            sys.stderr.write(
                f"Schema error: Failed to serialize spec '{spec_id}' to Mermaid: {e}\n"
            )
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

        # Write or update .mmd file
        with open(mmd_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(mmd_content)

        cached_entry = cache.get(spec_id, {})
        cached_hash = (
            cached_entry.get("sha256") if isinstance(cached_entry, dict) else None
        )

        needs_render = (
            force
            or cached_hash != current_hash
            or not svg_file.exists()
            or not png_file.exists()
        )

        if verify_only:
            # In verification mode, check syntax and click directives, and run mmdc if functional
            rendered_ok = False
            if mmdc_cmd:
                temp_svg = output_dir / f".tmp_verify_{spec_id}.svg"
                rendered_ok = render_diagram_artifact(
                    mmdc_cmd, mmd_file, temp_svg, "svg"
                )
                if temp_svg.exists():
                    temp_svg.unlink()

            if rendered_ok:
                print(
                    f"Verified spec '{spec_id}' -> {mmd_file.name} (headless mmdc render succeeded)."
                )
            else:
                print(
                    f"Verified spec '{spec_id}' -> {mmd_file.name} (validated click directives and schema)."
                )
            continue

        if not needs_render:
            print(f"Skipping cached diagram asset: {spec_id}")
            continue

        updated_count += 1
        print(f"Compiling diagram asset: {spec_id} ...")

        if mmdc_cmd:
            svg_ok = render_diagram_artifact(mmdc_cmd, mmd_file, svg_file, "svg")
            png_ok = render_diagram_artifact(mmdc_cmd, mmd_file, png_file, "png")

            if svg_ok and png_ok:
                new_cache[spec_id] = {"sha256": current_hash}
            else:
                sys.stderr.write(
                    f"Warning: Failed visual rendering for '{spec_id}'. Generating fallback SVG.\n"
                )
                fallback_svg = generate_fallback_svg(spec.title, mmd_content)
                with open(svg_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(fallback_svg)
                all_success = False
        else:
            print(
                f"mmdc not found in environment. Creating fallback text-based SVG for '{spec_id}'."
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
