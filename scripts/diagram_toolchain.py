"""Schema-Driven Diagram Compiler and Pure-Python Mermaid Validator.

Compiles declarative diagram schemas into canonical Mermaid (.mmd) files and
validates Mermaid syntax and interactive click directives in pure Python.
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.diagram_schema import BaseDiagramSpec

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs/assets/diagrams"


def get_sha256(content: str) -> str:
    """Compute SHA-256 hash of a string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_mermaid_syntax(mmd_content: str) -> list[str]:
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

    h_idx, _h_raw, h_code = header_entry
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


def collect_all_specs() -> dict[str, "BaseDiagramSpec"]:
    """Collect all registered system diagram specifications."""
    from scripts.diagram_schema import (
        BaseDiagramSpec,
        get_all_diagram_specs,
    )

    specs: dict[str, BaseDiagramSpec] = {}
    for _key, spec in get_all_diagram_specs().items():
        specs[spec.id] = spec
    return specs


_MARKDOWN_ANCHORS_CACHE: dict[Path, set[str]] = {}


def reset_markdown_anchors_cache() -> None:
    """Reset cached markdown anchors (useful for testing)."""
    global _MARKDOWN_ANCHORS_CACHE
    _MARKDOWN_ANCHORS_CACHE = {}


def extract_markdown_anchors(file_path: Path) -> set[str]:
    """Parse a target markdown file to discover valid heading slugs and HTML anchor identifiers."""
    resolved_path = file_path.resolve()
    if resolved_path in _MARKDOWN_ANCHORS_CACHE:
        return _MARKDOWN_ANCHORS_CACHE[resolved_path]

    anchors: set[str] = set()
    try:
        content = resolved_path.read_text(encoding="utf-8")
    except Exception:
        _MARKDOWN_ANCHORS_CACHE[resolved_path] = anchors
        return anchors

    # 1. Parse standard markdown headings while ignoring code blocks (``` or ~~~)
    in_code_block = False
    for line in content.splitlines():
        trimmed = line.strip()
        if trimmed.startswith(("```", "~~~")):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        m_heading = re.match(r"^#{1,6}\s+(.+)$", trimmed)
        if m_heading:
            heading_text = m_heading.group(1).strip()
            # Explicit attribute list identifier: {#custom-id}
            attr_match = re.search(r"\{#([a-zA-Z0-9_\-]+)\}\s*$", heading_text)
            if attr_match:
                anchors.add(attr_match.group(1))
                anchors.add(attr_match.group(1).lower())
                heading_text = heading_text[: attr_match.start()].strip()

            # GitHub-style markdown heading slugification
            slug_base = re.sub(r"[^\w\- ]", "", heading_text.lower())
            slug1 = slug_base.replace(" ", "-")
            if slug1:
                anchors.add(slug1)
            slug2 = re.sub(r"-+", "-", slug1).strip("-")
            if slug2:
                anchors.add(slug2)

    # 2. Parse explicit HTML anchor identifiers (id="..." or name="...")
    tag_matches = re.finditer(
        r'<[a-zA-Z0-9_\-]+\s+[^>]*?(?:id|name)=["\']([^"\']+)["\']',
        content,
        re.IGNORECASE,
    )
    for tm in tag_matches:
        val = tm.group(1).strip()
        if val:
            anchors.add(val)
            anchors.add(val.lower())

    _MARKDOWN_ANCHORS_CACHE[resolved_path] = anchors
    return anchors


def parse_and_validate_click_directives(mmd_content: str) -> list[str]:
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
                        from scripts.diagram_schema import DiagramNode

                        DiagramNode.validate_url_scheme(url_candidate)
                    except ValueError as ve:
                        errors.append(
                            f"Line {idx}: Unsafe or invalid URL in click directive: {ve}"
                        )

                    # Check relative file path target existence and anchor fragments if not an absolute URI scheme or hash-only
                    lower_url = url_candidate.lower()
                    if not (
                        lower_url.startswith(
                            ("http://", "https://", "mailto:", "file:", "#")
                        )
                        or "://" in lower_url
                    ):
                        if "#" in url_candidate:
                            file_part, anchor_part = url_candidate.split("#", 1)
                            file_part = file_part.strip()
                            anchor_part = anchor_part.strip()
                        else:
                            file_part = url_candidate.strip()
                            anchor_part = None

                        if file_part:
                            target_file = REPO_ROOT / file_part
                            if not target_file.exists():
                                errors.append(
                                    f"Line {idx}: Click directive target file '{file_part}' does not exist relative to REPO_ROOT ({REPO_ROOT})"
                                )
                            elif anchor_part is not None and file_part.lower().endswith(
                                ".md"
                            ):
                                if not anchor_part:
                                    errors.append(
                                        f"Line {idx}: Empty anchor fragment in click directive URL '{url_candidate}'"
                                    )
                                else:
                                    valid_anchors = extract_markdown_anchors(
                                        target_file
                                    )
                                    if (
                                        anchor_part not in valid_anchors
                                        and anchor_part.lower() not in valid_anchors
                                    ):
                                        errors.append(
                                            f"Line {idx}: Target anchor '{anchor_part}' does not exist in '{file_part}'"
                                        )

    return errors


def validate_docs_mermaid_blocks(docs_dir: Path | None = None) -> list[str]:
    """Validate syntax and click directives across fenced ```mermaid blocks in docs."""
    if docs_dir is None:
        docs_dir = REPO_ROOT / "docs"
    elif not docs_dir.is_absolute():
        docs_dir = REPO_ROOT / docs_dir

    md_targets: list[Path] = []
    if docs_dir.exists():
        md_targets.extend(sorted(docs_dir.rglob("*.md")))
    if docs_dir == REPO_ROOT / "docs":
        readme = REPO_ROOT / "README.md"
        if readme.exists():
            md_targets.append(readme)

    errors: list[str] = []
    block_re = re.compile(r"```mermaid\s*\n([\s\S]*?)```")
    for md_path in md_targets:
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(f"Could not read {md_path.as_posix()}: {exc}")
            continue

        for block_idx, match in enumerate(block_re.finditer(content), start=1):
            block = match.group(1)
            for syn_err in validate_mermaid_syntax(block):
                errors.append(
                    f"{md_path.as_posix()} (block #{block_idx}): Syntax error - {syn_err}"
                )
            for click_err in parse_and_validate_click_directives(block):
                errors.append(
                    f"{md_path.as_posix()} (block #{block_idx}): Click directive error - {click_err}"
                )
    return errors


def build_diagrams(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    force: bool = False,
    verify_only: bool = False,
) -> bool:
    """Compile diagram specifications into canonical .mmd files and validate syntax/links."""
    os.chdir(REPO_ROOT)
    if not output_dir.is_absolute():
        output_dir = REPO_ROOT / output_dir

    if not verify_only:
        output_dir.mkdir(parents=True, exist_ok=True)
    specs = collect_all_specs()

    if not specs:
        print("No diagram specifications registered.")
        return True

    all_success = True
    updated_count = 0

    if verify_only and output_dir == DEFAULT_OUTPUT_DIR:
        for doc_err in validate_docs_mermaid_blocks():
            sys.stderr.write(f"Documentation Mermaid error: {doc_err}\n")
            all_success = False

    for spec_id, spec in specs.items():
        mmd_file = output_dir / f"{spec_id}.mmd"

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

        # Validate click directives
        click_errs = parse_and_validate_click_directives(mmd_content)
        if click_errs:
            for ce in click_errs:
                sys.stderr.write(
                    f"Click directive validation error in spec '{spec_id}': {ce}\n"
                )
            all_success = False
            continue

        normalized_expected = mmd_content.replace("\r\n", "\n")
        existing_mmd = None
        if mmd_file.exists():
            try:
                existing_mmd = mmd_file.read_text(encoding="utf-8").replace(
                    "\r\n", "\n"
                )
            except Exception:
                existing_mmd = None

        if verify_only:
            if existing_mmd != normalized_expected:
                sys.stderr.write(
                    f"Verification FAILED: {mmd_file.name} is missing or out of sync "
                    f"with the current '{spec_id}' spec. Run "
                    "`uv run python scripts/diagram_toolchain.py build` and commit "
                    "the updated diagram files.\n"
                )
                all_success = False
            else:
                print(
                    f"Verified spec '{spec_id}' -> {mmd_file.name} (validated syntax, click directives, and schema)."
                )
            continue

        if force or existing_mmd != normalized_expected:
            with open(mmd_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(normalized_expected)
            updated_count += 1
            print(f"Compiled Mermaid diagram source: {spec_id}.mmd")

    if not verify_only:
        print(
            f"Diagram compilation complete. Processed {len(specs)} specs ({updated_count} updated)."
        )

    return all_success


def main() -> None:
    """CLI launcher for diagram toolchain compiler and validator."""
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(
        description="Schema-driven Diagram Toolchain & Pure-Python Mermaid Validator"
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
        help="Validate diagram specs, click directives, and compiled .mmd sources",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rewriting all .mmd diagram sources",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Target directory for compiled diagram sources (default: {DEFAULT_OUTPUT_DIR})",
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
        print("Executing diagram source compilation...")
        success = build_diagrams(
            output_dir=args.output_dir, force=args.force, verify_only=False
        )
        if not success:
            sys.stderr.write(
                "ERROR: One or more diagram specifications failed validation.\n"
            )
            sys.exit(1)
        sys.exit(0)


if __name__ == "__main__":
    main()
