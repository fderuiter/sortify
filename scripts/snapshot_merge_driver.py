#!/usr/bin/env python3
"""Custom Git Merge Driver and Interactive Snapshot Conflict Resolver.

Provides 3-way JSON/SVG snapshot merging, API signature backward-compatibility verification,
SHA-256 payload checksum calculation, Git conflict marker fallback, and interactive CLI inspection.
"""

import argparse
import ast
import difflib
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET


def compute_payload_checksum(payload: dict) -> str:
    """Compute SHA-256 checksum of canonical JSON payload definitions."""
    canonical_json = json.dumps(payload, indent=2, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def extract_metadata_and_payload(snapshot_data: dict) -> tuple[dict, dict]:
    """Separate metadata header from definition payload dictionary."""
    metadata = {}
    if isinstance(snapshot_data, dict):
        if "_metadata" in snapshot_data and isinstance(snapshot_data["_metadata"], dict):
            metadata = snapshot_data["_metadata"]
        elif "metadata" in snapshot_data and isinstance(snapshot_data["metadata"], dict):
            metadata = snapshot_data["metadata"]

    payload = {k: v for k, v in snapshot_data.items() if k not in ("_metadata", "metadata")}
    return metadata, payload


def parse_annotation_ast(annotation_str: str | None):
    """Safely validate annotation string using AST parsing."""
    if annotation_str and isinstance(annotation_str, str):
        try:
            return ast.parse(annotation_str, mode="eval")
        except Exception:
            pass
    return None


def detect_contract_breaking_changes(base_payload: dict, ours_payload: dict, theirs_payload: dict) -> list[str]:
    """Detect dropped or removed public API signatures relative to ancestor base_payload."""
    errors = []

    def check_branch(branch_payload: dict, branch_name: str):
        # 1. Check dropped classes
        base_classes = {c["class_name"]: c for c in base_payload.get("classes", []) if isinstance(c, dict) and "class_name" in c}
        branch_classes = {c["class_name"]: c for c in branch_payload.get("classes", []) if isinstance(c, dict) and "class_name" in c}

        for c_name, base_cls in base_classes.items():
            if c_name not in branch_classes:
                errors.append(f"Contract Error ({branch_name}): Public class '{c_name}' was removed or dropped.")
                continue

            # Check dropped methods
            branch_cls = branch_classes[c_name]
            base_methods = {m["name"]: m for m in base_cls.get("methods", []) if isinstance(m, dict) and "name" in m}
            branch_methods = {m["name"]: m for m in branch_cls.get("methods", []) if isinstance(m, dict) and "name" in m}

            for m_name, base_method in base_methods.items():
                if m_name not in branch_methods:
                    errors.append(f"Contract Error ({branch_name}): Public method '{c_name}.{m_name}' was removed or dropped.")
                else:
                    # Check dropped required parameters
                    branch_method = branch_methods[m_name]
                    base_params = {p["name"]: p for p in base_method.get("parameters", []) if isinstance(p, dict) and "name" in p}
                    branch_params = {p["name"]: p for p in branch_method.get("parameters", []) if isinstance(p, dict) and "name" in p}
                    for p_name, base_param in base_params.items():
                        parse_annotation_ast(base_param.get("annotation"))
                        if base_param.get("default") is None and not p_name.startswith("*") and p_name != "self" and p_name != "cls":
                            if p_name not in branch_params:
                                errors.append(f"Contract Error ({branch_name}): Required parameter '{p_name}' in method '{c_name}.{m_name}' was removed.")

        # 2. Check dropped functions
        base_funcs = {f["name"]: f for f in base_payload.get("functions", []) if isinstance(f, dict) and "name" in f}
        branch_funcs = {f["name"]: f for f in branch_payload.get("functions", []) if isinstance(f, dict) and "name" in f}

        for f_name, base_func in base_funcs.items():
            if f_name not in branch_funcs:
                errors.append(f"Contract Error ({branch_name}): Public function '{f_name}' was removed or dropped.")
            else:
                branch_func = branch_funcs[f_name]
                base_params = {p["name"]: p for p in base_func.get("parameters", []) if isinstance(p, dict) and "name" in p}
                branch_params = {p["name"]: p for p in branch_func.get("parameters", []) if isinstance(p, dict) and "name" in p}
                for p_name, base_param in base_params.items():
                    parse_annotation_ast(base_param.get("annotation"))
                    if base_param.get("default") is None and not p_name.startswith("*"):
                        if p_name not in branch_params:
                            errors.append(f"Contract Error ({branch_name}): Required parameter '{p_name}' in function '{f_name}' was removed.")

    check_branch(ours_payload, "ours")
    check_branch(theirs_payload, "theirs")

    return errors


def write_conflict_file(target_path: str, ours_str: str, theirs_str: str, label_ours="OURS", label_theirs="THEIRS"):
    """Write standard Git conflict markers to target_path."""
    content = f"<<<<<<< {label_ours}\n{ours_str}\n=======\n{theirs_str}\n>>>>>>> {label_theirs}\n"
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)


def merge_json_lists_by_key(base_list: list, ours_list: list, theirs_list: list, key_name: str) -> tuple[bool, list]:
    """3-way merge of lists of dictionaries keyed by key_name (e.g. 'class_name' or 'name')."""
    base_map = {item[key_name]: item for item in base_list if isinstance(item, dict) and key_name in item}
    ours_map = {item[key_name]: item for item in ours_list if isinstance(item, dict) and key_name in item}
    theirs_map = {item[key_name]: item for item in theirs_list if isinstance(item, dict) and key_name in item}

    all_keys = sorted(set(base_map.keys()) | set(ours_map.keys()) | set(theirs_map.keys()))
    merged_items = []

    for k in all_keys:
        in_base = k in base_map
        in_ours = k in ours_map
        in_theirs = k in theirs_map

        if in_base and in_ours and in_theirs:
            o_item = base_map[k]
            a_item = ours_map[k]
            b_item = theirs_map[k]

            if a_item == o_item and b_item == o_item:
                merged_items.append(o_item)
            elif a_item == o_item and b_item != o_item:
                merged_items.append(b_item)
            elif b_item == o_item and a_item != o_item:
                merged_items.append(a_item)
            elif a_item == b_item:
                merged_items.append(a_item)
            else:
                # Need deeper merge (e.g. methods in class)
                if key_name == "class_name":
                    success, merged_cls = merge_class_item(o_item, a_item, b_item)
                    if not success:
                        return False, []
                    merged_items.append(merged_cls)
                else:
                    # Item conflict
                    return False, []
        elif not in_base:
            if in_ours and not in_theirs:
                merged_items.append(ours_map[k])
            elif in_theirs and not in_ours:
                merged_items.append(theirs_map[k])
            elif in_ours and in_theirs:
                if ours_map[k] == theirs_map[k]:
                    merged_items.append(ours_map[k])
                else:
                    if key_name == "class_name":
                        success, merged_cls = merge_class_item({}, ours_map[k], theirs_map[k])
                        if not success:
                            return False, []
                        merged_items.append(merged_cls)
                    else:
                        return False, []

    return True, merged_items


def merge_class_item(base_cls: dict, ours_cls: dict, theirs_cls: dict) -> tuple[bool, dict]:
    """Merge class definitions including decorators and methods."""
    class_name = ours_cls.get("class_name") or theirs_cls.get("class_name") or base_cls.get("class_name")

    # Merge decorators
    ours_decs = ours_cls.get("decorators", [])
    theirs_decs = theirs_cls.get("decorators", [])

    merged_decs = list(ours_decs)
    for dec in theirs_decs:
        if dec not in merged_decs:
            merged_decs.append(dec)

    # Merge methods
    base_methods = base_cls.get("methods", [])
    ours_methods = ours_cls.get("methods", [])
    theirs_methods = theirs_cls.get("methods", [])

    success, merged_methods = merge_json_lists_by_key(base_methods, ours_methods, theirs_methods, "name")
    if not success:
        return False, {}

    merged_methods.sort(key=lambda m: m.get("name", ""))

    return True, {
        "class_name": class_name,
        "decorators": merged_decs,
        "methods": merged_methods,
    }


def merge_cli_calls(base_cli: list, ours_cli: list, theirs_cli: list) -> list:
    """Merge list of CLI argument call dictionaries preserving additions."""
    merged = list(ours_cli)
    for item in theirs_cli:
        if item not in merged:
            merged.append(item)
    return merged


def three_way_json_merge(base_data: dict, ours_data: dict, theirs_data: dict) -> tuple[bool, dict]:
    """Perform 3-way merge on JSON snapshot payloads."""
    _, base_payload = extract_metadata_and_payload(base_data)
    _, ours_payload = extract_metadata_and_payload(ours_data)
    _, theirs_payload = extract_metadata_and_payload(theirs_data)

    merged_payload = {}

    # Merge classes
    base_classes = base_payload.get("classes", [])
    ours_classes = ours_payload.get("classes", [])
    theirs_classes = theirs_payload.get("classes", [])

    if base_classes or ours_classes or theirs_classes:
        success, merged_classes = merge_json_lists_by_key(base_classes, ours_classes, theirs_classes, "class_name")
        if not success:
            return False, {}
        merged_classes.sort(key=lambda c: c.get("class_name", ""))
        merged_payload["classes"] = merged_classes

    # Merge functions
    base_funcs = base_payload.get("functions", [])
    ours_funcs = ours_payload.get("functions", [])
    theirs_funcs = theirs_payload.get("functions", [])

    if base_funcs or ours_funcs or theirs_funcs:
        success, merged_funcs = merge_json_lists_by_key(base_funcs, ours_funcs, theirs_funcs, "name")
        if not success:
            return False, {}
        merged_funcs.sort(key=lambda f: f.get("name", ""))
        merged_payload["functions"] = merged_funcs

    # Merge cli calls
    base_cli = base_payload.get("cli", [])
    ours_cli = ours_payload.get("cli", [])
    theirs_cli = theirs_payload.get("cli", [])

    if base_cli or ours_cli or theirs_cli:
        merged_payload["cli"] = merge_cli_calls(base_cli, ours_cli, theirs_cli)

    # Merge any remaining arbitrary dict keys
    all_keys = set(base_payload.keys()) | set(ours_payload.keys()) | set(theirs_payload.keys())
    for k in all_keys:
        if k in ("classes", "functions", "cli"):
            continue
        val_o = base_payload.get(k)
        val_a = ours_payload.get(k)
        val_b = theirs_payload.get(k)

        if val_a == val_o and val_b == val_o:
            merged_payload[k] = val_o
        elif val_a == val_o and val_b != val_o:
            merged_payload[k] = val_b
        elif val_b == val_o and val_a != val_o:
            merged_payload[k] = val_a
        elif val_a == val_b:
            merged_payload[k] = val_a
        else:
            return False, {}

    return True, merged_payload


def run_json_merge_driver(ancestor_path: str, current_path: str, incoming_path: str, pathname: str = "") -> int:
    """Run 3-way merge driver for JSON snapshot files."""
    try:
        with open(ancestor_path, "r", encoding="utf-8") as f:
            base_data = json.load(f)
        with open(current_path, "r", encoding="utf-8") as f:
            ours_data = json.load(f)
        with open(incoming_path, "r", encoding="utf-8") as f:
            theirs_data = json.load(f)
    except Exception as e:
        print(f"Error reading JSON snapshot versions for merge: {e}", file=sys.stderr)
        return 1

    _, base_payload = extract_metadata_and_payload(base_data)
    _, ours_payload = extract_metadata_and_payload(ours_data)
    _, theirs_payload = extract_metadata_and_payload(theirs_data)

    # Check for contract breaking changes
    contract_errors = detect_contract_breaking_changes(base_payload, ours_payload, theirs_payload)
    if contract_errors:
        print(f"FAIL: Snapshot contract breaking changes detected in '{pathname or current_path}'!", file=sys.stderr)
        for err in contract_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    success, merged_payload = three_way_json_merge(base_data, ours_data, theirs_data)
    if not success:
        print(f"Conflict: Unresolvable JSON snapshot collision in '{pathname or current_path}'. Writing conflict markers.", file=sys.stderr)
        ours_str = json.dumps(ours_data, indent=2, sort_keys=True)
        theirs_str = json.dumps(theirs_data, indent=2, sort_keys=True)
        write_conflict_file(current_path, ours_str, theirs_str)
        return 1

    # Recompute payload SHA-256 checksum
    checksum = compute_payload_checksum(merged_payload)
    final_data = {
        "_metadata": {
            "checksum": checksum,
        },
        **merged_payload,
    }

    with open(current_path, "w", encoding="utf-8") as f:
        json.dump(final_data, f, indent=2, sort_keys=True)
        f.write("\n")

    return 0


def three_way_svg_merge(base_str: str, ours_str: str, theirs_str: str) -> tuple[bool, str]:
    """Perform 3-way merge on SVG snapshot XML strings."""
    if ours_str == theirs_str:
        return True, ours_str
    if ours_str == base_str:
        return True, theirs_str
    if theirs_str == base_str:
        return True, ours_str

    try:
        base_root = ET.fromstring(base_str)
        ours_root = ET.fromstring(ours_str)
        theirs_root = ET.fromstring(theirs_str)
    except Exception:
        return False, ""

    # Compare children elements
    base_children = [ET.tostring(c, encoding="utf-8").decode("utf-8") for c in base_root]

    # Check additions in theirs
    theirs_additions = [c for c in theirs_root if ET.tostring(c, encoding="utf-8").decode("utf-8") not in base_children]

    # If roots match and we have additions from both sides
    if ours_root.tag == theirs_root.tag == base_root.tag:
        merged_root = ET.fromstring(ours_str)
        existing_xml_strings = {ET.tostring(c, encoding="utf-8").decode("utf-8") for c in merged_root}
        for add_child in theirs_additions:
            xml_str = ET.tostring(add_child, encoding="utf-8").decode("utf-8")
            if xml_str not in existing_xml_strings:
                merged_root.append(add_child)
                existing_xml_strings.add(xml_str)

        merged_svg_str = ET.tostring(merged_root, encoding="utf-8").decode("utf-8")
        if not merged_svg_str.startswith("<?xml"):
            merged_svg_str = '<?xml version="1.0" encoding="utf-8"?>\n' + merged_svg_str
        return True, merged_svg_str

    return False, ""


def run_svg_merge_driver(ancestor_path: str, current_path: str, incoming_path: str, pathname: str = "") -> int:
    """Run 3-way merge driver for SVG visual snapshot files."""
    try:
        with open(ancestor_path, "r", encoding="utf-8") as f:
            base_str = f.read()
        with open(current_path, "r", encoding="utf-8") as f:
            ours_str = f.read()
        with open(incoming_path, "r", encoding="utf-8") as f:
            theirs_str = f.read()
    except Exception as e:
        print(f"Error reading SVG snapshot versions for merge: {e}", file=sys.stderr)
        return 1

    success, merged_svg = three_way_svg_merge(base_str, ours_str, theirs_str)
    if success:
        with open(current_path, "w", encoding="utf-8") as f:
            f.write(merged_svg)
            if not merged_svg.endswith("\n"):
                f.write("\n")
        return 0

    print(f"Conflict: Unresolvable SVG snapshot collision in '{pathname or current_path}'. Writing conflict markers.", file=sys.stderr)
    write_conflict_file(current_path, ours_str, theirs_str)
    return 1


def run_git_setup():
    """Configure local Git repository with custom merge drivers."""
    cmds = [
        ["git", "config", "merge.snapshot-json.name", "Snapshot JSON 3-way merge driver"],
        ["git", "config", "merge.snapshot-json.driver", "python scripts/snapshot_merge_driver.py --mode=json %O %A %B %P"],
        ["git", "config", "merge.snapshot-svg.name", "Snapshot SVG 3-way merge driver"],
        ["git", "config", "merge.snapshot-svg.driver", "python scripts/snapshot_merge_driver.py --mode=svg %O %A %B %P"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            print(f"Error executing git config command {' '.join(cmd)}: {e}", file=sys.stderr)
            sys.exit(1)

    print("Successfully configured snapshot merge drivers in git config.")
    sys.exit(0)


def run_cli_resolver(file_path: str = None, diff_only: bool = False):
    """Run interactive CLI resolution tool for inspecting snapshot diffs and fields."""
    print("==================================================================")
    print("         Interactive Snapshot Conflict & Diff Resolver            ")
    print("==================================================================")

    if file_path and os.path.exists(file_path):
        target_files = [file_path]
    else:
        # Scan snapshot directory for files or conflicts
        target_files = []
        snap_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "snapshots")
        if os.path.exists(snap_dir):
            for root, _, files in os.walk(snap_dir):
                for file in files:
                    if file.endswith((".json", ".svg")):
                        target_files.append(os.path.join(root, file))

    if not target_files:
        print("No snapshot files found for inspection.")
        sys.exit(0)

    print(f"Found {len(target_files)} snapshot file(s) for analysis:\n")
    for idx, tf in enumerate(target_files, 1):
        rel_p = os.path.relpath(tf)
        has_conflict = False
        try:
            with open(tf, "r", encoding="utf-8") as f:
                content = f.read()
                if "<<<<<<<" in content and "=======" in content and ">>>>>>>" in content:
                    has_conflict = True
        except Exception:
            pass

        status = "[CONFLICT]" if has_conflict else "[OK]"
        print(f"  {idx}. {rel_p} {status}")

    print("\nSelect a file number to inspect diffs/fields, or press enter to exit:")

    # If non-interactive mode or stdin not a tty
    if not sys.stdin.isatty():
        print("Non-interactive session detected. Diff analysis summary generated.")
        for tf in target_files:
            rel_p = os.path.relpath(tf)
            print(f"\n--- Analysis for {rel_p} ---")
            try:
                with open(tf, "r", encoding="utf-8") as f:
                    content = f.read()
                    if tf.endswith(".json"):
                        data = json.loads(content)
                        if isinstance(data, dict):
                            meta, payload = extract_metadata_and_payload(data)
                            classes = [c.get("class_name") for c in payload.get("classes", []) if isinstance(c, dict)]
                            funcs = [f.get("name") for f in payload.get("functions", []) if isinstance(f, dict)]
                            print(f"  Checksum: {meta.get('checksum')}")
                            print(f"  Classes ({len(classes)}): {', '.join(classes)}")
                            print(f"  Functions ({len(funcs)}): {', '.join(funcs)}")
                        elif isinstance(data, list):
                            print(f"  JSON list items: {len(data)}")
                        # Generate diff against empty payload baseline for verification
                        diff = list(difflib.unified_diff([], content.splitlines(), fromfile="Baseline", tofile=rel_p))
                        if diff:
                            print(f"  Diff lines generated: {len(diff)}")
                    elif tf.endswith(".svg"):
                        print(f"  SVG lines: {len(content.splitlines())}")
            except Exception as e:
                print(f"  Error reading file: {e}")
        sys.exit(0)

    try:
        user_input = input("> ").strip()
        if not user_input.isdigit():
            print("Exiting interactive resolver.")
            sys.exit(0)
        choice = int(user_input) - 1
        if 0 <= choice < len(target_files):
            selected = target_files[choice]
            print(f"\nInspecting: {selected}")
            with open(selected, "r", encoding="utf-8") as f:
                content = f.read()
                print("Content Preview:")
                print("----------------------------------------------------------------")
                lines = content.splitlines()
                for line in lines[:30]:
                    print(line)
                if len(lines) > 30:
                    print(f"... ({len(lines) - 30} more lines)")
                print("----------------------------------------------------------------")
    except (KeyboardInterrupt, EOFError):
        print("\nExiting interactive resolver.")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Git Snapshot 3-Way Merge Driver and Conflict Resolver.")
    parser.add_argument("--setup", action="store_true", help="Configure local Git config with custom snapshot merge drivers.")
    parser.add_argument("--cli", action="store_true", help="Run interactive CLI snapshot conflict & diff analysis tool.")
    parser.add_argument("--mode", choices=["json", "svg", "auto"], default="auto", help="Snapshot mode (json, svg, or auto-detect).")
    parser.add_argument("args", nargs="*", help="Positional arguments passed by Git: %O %A %B [%P]")

    parsed, unknown = parser.parse_known_args()

    if parsed.setup:
        run_git_setup()

    if parsed.cli:
        target_file = parsed.args[0] if parsed.args else None
        run_cli_resolver(target_file)
        sys.exit(0)

    pos_args = parsed.args + unknown
    if len(pos_args) < 3:
        # If run without enough merge positional args and without --cli / --setup, show usage or default to --cli
        if not parsed.setup and not parsed.cli:
            parser.print_help()
            sys.exit(0)

    ancestor_path = pos_args[0]
    current_path = pos_args[1]
    incoming_path = pos_args[2]
    pathname = pos_args[3] if len(pos_args) > 3 else ""

    mode = parsed.mode
    if mode == "auto":
        target_name = pathname or current_path
        if target_name.endswith(".svg"):
            mode = "svg"
        else:
            mode = "json"

    if mode == "svg":
        code = run_svg_merge_driver(ancestor_path, current_path, incoming_path, pathname)
    else:
        code = run_json_merge_driver(ancestor_path, current_path, incoming_path, pathname)

    sys.exit(code)


if __name__ == "__main__":
    main()
