#!/usr/bin/env python3
"""AST-based Signature & CLI Snapshot Validation.

This script parses developer protocols and CLI tools statically to verify
backwards compatibility against a checked-in API/CLI snapshot file.
"""

import argparse
import ast
import difflib
import hashlib
import json
import os
import sys

# Compute project base directory (/app) based on script location
BASE_DIR = os.path.realpath(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
SNAPSHOT_PATH = os.path.join(BASE_DIR, "tests", "snapshots", "api_snapshot.json")

# Source files to parse
ANALYZER_STRATEGIES_PATH = os.path.join(
    BASE_DIR, "app", "core", "analyzer_strategies.py"
)
EXTRACTOR_STRATEGIES_PATH = os.path.join(
    BASE_DIR, "app", "core", "extractor_strategies.py"
)
MAIN_CLI_PATH = os.path.join(BASE_DIR, "app", "main.py")
SANDBOX_CLI_PATH = os.path.join(BASE_DIR, "sandbox_cli.py")


def safe_relpath(path, start):
    """Compute relative path if possible, fallback to absolute path if on different Windows drives."""
    try:
        return os.path.relpath(os.path.realpath(path), os.path.realpath(start))
    except ValueError:
        return os.path.abspath(path)


def get_ast_value(node):
    """Safely extract literal values from AST nodes or fallback to unparsed code representation."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return ast.unparse(node)


def is_protocol_node(node_to_check):
    """Check if the AST node represents a Protocol or typing.Protocol."""
    if isinstance(node_to_check, ast.Name) and node_to_check.id == "Protocol":
        return True
    if (
        isinstance(node_to_check, ast.Attribute)
        and isinstance(node_to_check.value, ast.Name)
        and node_to_check.value.id == "typing"
        and node_to_check.attr == "Protocol"
    ):
        return True
    return False


def extract_decorators(decorator_list):
    """Format AST decorator nodes as readable decorator strings."""
    decorators = []
    for d in decorator_list:
        dec_str = ast.unparse(d)
        if not dec_str.startswith("@"):
            dec_str = f"@{dec_str}"
        decorators.append(dec_str)
    return decorators


def extract_parameters(args_node):
    """Extract parameters, annotations, and defaults from an AST arguments node."""
    all_args = args_node.posonlyargs + args_node.args
    defaults = args_node.defaults
    default_map = {}
    for i, default_node in enumerate(defaults):
        arg_idx = len(all_args) - len(defaults) + i
        default_map[id(all_args[arg_idx])] = ast.unparse(default_node)

    params = []
    for arg_node in all_args:
        annotation_str = (
            ast.unparse(arg_node.annotation) if arg_node.annotation else None
        )
        default_str = default_map.get(id(arg_node), None)
        params.append(
            {
                "name": arg_node.arg,
                "annotation": annotation_str,
                "default": default_str,
            }
        )

    if args_node.vararg:
        arg_node = args_node.vararg
        annotation_str = (
            ast.unparse(arg_node.annotation) if arg_node.annotation else None
        )
        params.append(
            {
                "name": f"*{arg_node.arg}",
                "annotation": annotation_str,
                "default": None,
            }
        )

    for kwarg, default_node in zip(args_node.kwonlyargs, args_node.kw_defaults):
        annotation_str = (
            ast.unparse(kwarg.annotation) if kwarg.annotation else None
        )
        default_str = (
            ast.unparse(default_node) if default_node is not None else None
        )
        params.append(
            {
                "name": kwarg.arg,
                "annotation": annotation_str,
                "default": default_str,
            }
        )

    if args_node.kwarg:
        arg_node = args_node.kwarg
        annotation_str = (
            ast.unparse(arg_node.annotation) if arg_node.annotation else None
        )
        params.append(
            {
                "name": f"**{arg_node.arg}",
                "annotation": annotation_str,
                "default": None,
            }
        )

    return params


def extract_function_signature(func_node):
    """Extract signature dictionary for a FunctionDef or AsyncFunctionDef node."""
    is_async = isinstance(func_node, ast.AsyncFunctionDef)
    params = extract_parameters(func_node.args)
    return_annotation = (
        ast.unparse(func_node.returns) if func_node.returns else None
    )
    decorators = extract_decorators(func_node.decorator_list)

    return {
        "name": func_node.name,
        "async": is_async,
        "decorators": decorators,
        "parameters": params,
        "returns": return_annotation,
    }


def extract_module_signatures(file_path):
    """Statically parse a Python file and extract public class and function signatures."""
    if not os.path.exists(file_path):
        print(f"Error: Module source file not found: {file_path}", file=sys.stderr)
        return {"classes": [], "functions": []}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=file_path)
    except Exception as e:
        print(f"Warning: Failed to parse AST for {file_path}: {e}", file=sys.stderr)
        return {"classes": [], "functions": []}

    classes = []
    functions = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            # Only extract public classes
            if node.name.startswith("_"):
                continue

            class_decorators = extract_decorators(node.decorator_list)
            methods = []

            for body_node in node.body:
                if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Include public methods and __init__
                    if body_node.name.startswith("_") and body_node.name != "__init__":
                        continue
                    methods.append(extract_function_signature(body_node))

            methods.sort(key=lambda m: m["name"])
            classes.append(
                {
                    "class_name": node.name,
                    "decorators": class_decorators,
                    "methods": methods,
                }
            )

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Only extract public top-level functions
            if node.name.startswith("_"):
                continue

            functions.append(extract_function_signature(node))

    classes.sort(key=lambda c: c["class_name"])
    functions.sort(key=lambda f: f["name"])

    return {
        "classes": classes,
        "functions": functions,
    }


def extract_protocols(file_path):
    """Statically parse a Python file and extract classes that inherit from Protocol."""
    if not os.path.exists(file_path):
        print(f"Error: Protocol source file not found: {file_path}", file=sys.stderr)
        return {}

    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=file_path)
    protocols = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            is_protocol = False
            for base in node.bases:
                if is_protocol_node(base):
                    is_protocol = True
                elif isinstance(base, ast.Subscript) and is_protocol_node(base.value):
                    is_protocol = True

            if is_protocol:
                class_name = node.name
                methods = []
                for body_node in node.body:
                    if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(extract_function_signature(body_node))

                protocols[class_name] = {
                    "class_name": class_name,
                    "methods": sorted(methods, key=lambda m: m["name"]),
                }

    return protocols


def extract_cli(file_path):
    """Statically parse a Python file and extract argparse-related call nodes."""
    if not os.path.exists(file_path):
        print(f"Error: CLI source file not found: {file_path}", file=sys.stderr)
        return []

    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=file_path)
    cli_calls = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr in ("add_argument", "add_parser"):
                    caller = ast.unparse(node.func.value)
                    method = node.func.attr

                    args = []
                    for arg in node.args:
                        args.append(get_ast_value(arg))

                    keywords = {}
                    for kw in node.keywords:
                        if kw.arg:
                            keywords[kw.arg] = get_ast_value(kw.value)

                    cli_calls.append(
                        {
                            "caller": caller,
                            "method": method,
                            "args": args,
                            "keywords": keywords,
                        }
                    )

    return cli_calls


def collect_core_definitions(core_dir=None):
    """Dynamically scan core package modules and extract public definitions."""
    if core_dir is None:
        core_dir = os.path.join(BASE_DIR, "app", "core")

    core_data = {}
    if os.path.exists(core_dir):
        for root, dirs, files in os.walk(core_dir):
            dirs.sort()
            files.sort()
            for file in files:
                if file.endswith(".py") and not file.startswith("."):
                    full_path = os.path.join(root, file)
                    rel_path = safe_relpath(full_path, BASE_DIR).replace("\\", "/")
                    core_data[rel_path] = extract_module_signatures(full_path)

    return dict(sorted(core_data.items()))


def collect_current_definitions():
    """Parse codebase files and return the complete current definitions structure."""
    cli_data = {
        "app/main.py": extract_cli(MAIN_CLI_PATH),
        "sandbox_cli.py": extract_cli(SANDBOX_CLI_PATH),
    }

    return {
        "cli": cli_data,
        "core": collect_core_definitions(),
    }


def compute_payload_checksum(payload: dict) -> str:
    """Compute SHA-256 checksum of canonical JSON payload definitions."""
    canonical_json = json.dumps(payload, indent=2, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def extract_metadata_and_payload(snapshot_data: dict) -> tuple[dict | None, dict]:
    """Separate metadata header from the definitions payload dictionary."""
    metadata = None
    if isinstance(snapshot_data, dict):
        if "_metadata" in snapshot_data and isinstance(snapshot_data["_metadata"], dict):
            metadata = snapshot_data["_metadata"]
        elif "metadata" in snapshot_data and isinstance(snapshot_data["metadata"], dict):
            metadata = snapshot_data["metadata"]

    payload = {
        k: v
        for k, v in snapshot_data.items()
        if k not in ("_metadata", "metadata")
    }
    return metadata, payload


def verify_snapshot_integrity(
    snapshot_data: dict, snapshot_path: str = None
) -> tuple[bool, str, dict]:
    """Verify inline checksum header of a snapshot data dictionary.

    Returns (is_valid, error_message, payload_definitions).
    """
    if not isinstance(snapshot_data, dict):
        path_str = f" in {snapshot_path}" if snapshot_path else ""
        return (
            False,
            f"Error: Invalid snapshot format (expected JSON object){path_str}.",
            {},
        )

    metadata, payload = extract_metadata_and_payload(snapshot_data)
    if not metadata:
        path_str = f" in {snapshot_path}" if snapshot_path else ""
        return (
            False,
            f"Error: Snapshot file integrity check failed: missing embedded metadata header{path_str}.",
            payload,
        )

    embedded_checksum = metadata.get("checksum") or metadata.get("sha256")
    if not embedded_checksum:
        path_str = f" in {snapshot_path}" if snapshot_path else ""
        return (
            False,
            f"Error: Snapshot file integrity check failed: missing checksum in metadata header{path_str}.",
            payload,
        )

    computed_checksum = compute_payload_checksum(payload)
    if embedded_checksum != computed_checksum:
        path_str = f" in {snapshot_path}" if snapshot_path else ""
        err = (
            f"Error: Snapshot file integrity verification failed! Checksum mismatch{path_str}.\n"
            f"  Embedded checksum: {embedded_checksum}\n"
            f"  Computed checksum: {computed_checksum}"
        )
        return False, err, payload

    return True, "", payload


def main():
    """Run CLI snapshot validation engine."""
    parser = argparse.ArgumentParser(
        description="Verify public protocol signatures and CLI interfaces statically."
    )
    parser.add_argument(
        "--update",
        "--regenerate",
        action="store_true",
        dest="regenerate",
        help="Update/regenerate the verified API/CLI baseline snapshot file.",
    )
    args = parser.parse_args()

    # Collect current codebase signatures
    current_definitions = collect_current_definitions()

    is_ci = os.environ.get("CI", "").lower() in ("true", "1")

    if args.regenerate:
        if is_ci:
            print(
                "Error: Baseline regeneration is disabled in continuous integration.",
                file=sys.stderr,
            )
            sys.exit(1)

        checksum = compute_payload_checksum(current_definitions)
        snapshot_data = {
            "_metadata": {
                "checksum": checksum,
            },
            **current_definitions,
        }

        # Create directory if missing
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot_data, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"Successfully generated new baseline snapshot at {SNAPSHOT_PATH}")
        sys.exit(0)

    # Check if snapshot baseline exists
    if not os.path.exists(SNAPSHOT_PATH):
        print(
            f"Error: Baseline snapshot file does not exist at {SNAPSHOT_PATH}.\n"
            f"Run this script with --regenerate to initialize it.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load checked-in baseline snapshot
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        try:
            snapshot_data = json.load(f)
        except Exception as e:
            print(
                f"Error: Failed to parse checked-in baseline snapshot JSON: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Verify snapshot integrity prior to evaluating interface definitions
    is_valid, err_msg, snapshot_definitions = verify_snapshot_integrity(
        snapshot_data, SNAPSHOT_PATH
    )
    if not is_valid:
        print(err_msg, file=sys.stderr)
        sys.exit(1)

    # Compare current against snapshot payload definitions
    current_json = json.dumps(current_definitions, indent=2, sort_keys=True)
    snapshot_json = json.dumps(snapshot_definitions, indent=2, sort_keys=True)

    if current_json != snapshot_json:
        print(
            "FAIL: Public interface or CLI signature drift detected!",
            file=sys.stderr,
        )
        print(
            "----------------------------------------------------------------",
            file=sys.stderr,
        )
        diff = list(
            difflib.unified_diff(
                snapshot_json.splitlines(keepends=True),
                current_json.splitlines(keepends=True),
                fromfile=f"Snapshot ({safe_relpath(SNAPSHOT_PATH, BASE_DIR)})",
                tofile="Current Codebase",
            )
        )
        sys.stderr.writelines(diff)
        print(
            "----------------------------------------------------------------",
            file=sys.stderr,
        )
        if not is_ci:
            print(
                "If this change was intentional, update the baseline snapshot by running:",
                file=sys.stderr,
            )
            print(
                f"  python3 {safe_relpath(__file__, BASE_DIR)} --regenerate",
                file=sys.stderr,
            )
        else:
            print(
                "In CI, automated baseline regeneration is disabled. "
                f"Please commit the updated snapshot file '{safe_relpath(SNAPSHOT_PATH, BASE_DIR)}'.",
                file=sys.stderr,
            )
        sys.exit(1)

    print("SUCCESS: Codebase signatures match baseline snapshot.")
    sys.exit(0)


if __name__ == "__main__":
    main()
