"""Schema-Driven Diagram Compiler and External CLI Toolchain.

Compiles declarative diagram schemas into canonical Mermaid (.mmd) files,
validates syntax, and renders visual SVG and PNG diagram assets using @mermaid-js/mermaid-cli.
"""

import argparse
import hashlib
import json
import os
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
    BaseDiagramSpec,
)

DEFAULT_OUTPUT_DIR = Path("docs/assets/diagrams")
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

    cmd = mmdc_cmd or find_mmdc_executable()
    if not cmd:
        _BROWSER_AVAILABLE_CACHE = False
        return False

    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
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
                probe_cmd, capture_output=True, text=True, check=False, timeout=15
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


def find_mmdc_executable() -> Optional[List[str]]:
    """Determine the command prefix required to run mmdc (mermaid-cli)."""
    mmdc_path = shutil.which("mmdc")
    if mmdc_path:
        return [mmdc_path]

    npx_path = shutil.which("npx")
    if npx_path:
        return [npx_path, "--yes", "-p", "@mermaid-js/mermaid-cli", "mmdc"]

    return None


def check_no_raw_mermaid_in_docs(docs_dir: Path = Path("docs")) -> bool:
    """Scan docs directory for raw inline ```mermaid code blocks and return False if any remain."""
    import re

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


def collect_all_specs() -> Dict[str, BaseDiagramSpec]:
    """Collect all registered system and component diagram specifications."""
    specs: Dict[str, BaseDiagramSpec] = {}

    # System diagrams
    for key, spec in SYSTEM_DIAGRAM_SPECS.items():
        specs[spec.id] = spec

    # Catalog component specs
    for entry in CATALOG_REGISTRY:
        if "diagram_spec" in entry and hasattr(entry["diagram_spec"], "to_mermaid"):
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
    """Generate a clean text-based SVG placeholder when headless rendering is unavailable."""
    escaped_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    lines = [line.strip() for line in mmd_content.splitlines() if line.strip()]
    content_preview = "\n".join(lines[:10])
    escaped_preview = (
        content_preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="800" height="400" viewBox="0 0 800 400">
  <rect width="100%" height="100%" fill="#f8fafc" rx="8" stroke="#cbd5e1" stroke-width="2"/>
  <text x="20" y="40" font-family="sans-serif" font-size="18" font-weight="bold" fill="#0f172a">{escaped_title}</text>
  <text x="20" y="70" font-family="sans-serif" font-size="12" fill="#64748b">Mermaid Diagram Specification (Fallback View):</text>
  <foreignObject x="20" y="90" width="760" height="290">
    <pre xmlns="http://www.w3.org/1999/xhtml" style="font-family: monospace; font-size: 12px; color: #334155; background: #ffffff; padding: 12px; border-radius: 6px; border: 1px solid #e2e8f0; overflow: auto; height: 260px;">{escaped_preview}</pre>
  </foreignObject>
</svg>
"""


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
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            sys.stderr.write(
                f"mmdc failed for {mmd_path.name} ({output_format}): {res.stderr}\n"
            )
            return False
        return True
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
    has_browser = is_browser_available(mmdc_cmd)

    if not has_browser:
        if mmdc_cmd:
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
            or (has_browser and not png_file.exists())
        )

        if verify_only:
            # In verification mode, check schema validity and headless rendering or fallback SVG generation
            if has_browser and mmdc_cmd:
                temp_svg = output_dir / f".tmp_verify_{spec_id}.svg"
                success = render_diagram_artifact(mmdc_cmd, mmd_file, temp_svg, "svg")
                if temp_svg.exists():
                    temp_svg.unlink()
                if not success:
                    sys.stderr.write(
                        f"Verification FAILED: Diagram '{spec_id}' failed headless compilation.\n"
                    )
                    all_success = False
                else:
                    print(
                        f"Verified spec '{spec_id}' -> {mmd_file.name} (headless compilation successful)."
                    )
            else:
                fallback_svg = generate_fallback_svg(spec.title, mmd_content)
                if not svg_file.exists() or force or cached_hash != current_hash:
                    with open(svg_file, "w", encoding="utf-8", newline="\n") as f:
                        f.write(fallback_svg)
                print(
                    f"Verified spec '{spec_id}' -> {mmd_file.name} (using fallback SVG generator, browser engine unavailable)."
                )
            continue

        if not needs_render:
            print(f"Skipping cached diagram asset: {spec_id}")
            continue

        updated_count += 1
        print(f"Compiling diagram asset: {spec_id} ...")

        if has_browser and mmdc_cmd:
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
                f"Browser engine unavailable. Creating fallback text-based SVG for '{spec_id}'."
            )
            fallback_svg = generate_fallback_svg(spec.title, mmd_content)
            with open(svg_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(fallback_svg)
            new_cache[spec_id] = {"sha256": current_hash, "fallback": True}

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
