#!/usr/bin/env python3
"""AST-based Signature & CLI Snapshot Validation.

This script parses developer protocols and CLI tools statically to verify
backwards compatibility against a checked-in API/CLI snapshot file.
"""

import argparse
import ast
import difflib
import json
import os
import sys

# Compute project base directory (/app) based on script location
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def get_ast_value(node):
    """Safely extract literal values from AST nodes or fallback to unparsed code representation."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return ast.unparse(node)


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
                if isinstance(base, ast.Name) and base.id == "Protocol":
                    is_protocol = True
                elif (
                    isinstance(base, ast.Attribute)
                    and isinstance(base.value, ast.Name)
                    and base.value.id == "typing"
                    and base.attr == "Protocol"
                ):
                    is_protocol = True

            if is_protocol:
                class_name = node.name
                methods = []
                for body_node in node.body:
                    if isinstance(body_node, ast.FunctionDef):
                        all_args = body_node.args.posonlyargs + body_node.args.args
                        defaults = body_node.args.defaults
                        default_map = {}
                        for i, default_node in enumerate(defaults):
                            arg_idx = len(all_args) - len(defaults) + i
                            default_map[id(all_args[arg_idx])] = ast.unparse(
                                default_node
                            )

                        params = []
                        for arg_node in all_args:
                            annotation_str = (
                                ast.unparse(arg_node.annotation)
                                if arg_node.annotation
                                else None
                            )
                            default_str = default_map.get(id(arg_node), None)
                            params.append(
                                {
                                    "name": arg_node.arg,
                                    "annotation": annotation_str,
                                    "default": default_str,
                                }
                            )

                        if body_node.args.vararg:
                            arg_node = body_node.args.vararg
                            annotation_str = (
                                ast.unparse(arg_node.annotation)
                                if arg_node.annotation
                                else None
                            )
                            params.append(
                                {
                                    "name": f"*{arg_node.arg}",
                                    "annotation": annotation_str,
                                    "default": None,
                                }
                            )

                        for kwarg, default_node in zip(
                            body_node.args.kwonlyargs, body_node.args.kw_defaults
                        ):
                            annotation_str = (
                                ast.unparse(kwarg.annotation)
                                if kwarg.annotation
                                else None
                            )
                            default_str = (
                                ast.unparse(default_node)
                                if default_node is not None
                                else None
                            )
                            params.append(
                                {
                                    "name": kwarg.arg,
                                    "annotation": annotation_str,
                                    "default": default_str,
                                }
                            )

                        if body_node.args.kwarg:
                            arg_node = body_node.args.kwarg
                            annotation_str = (
                                ast.unparse(arg_node.annotation)
                                if arg_node.annotation
                                else None
                            )
                            params.append(
                                {
                                    "name": f"**{arg_node.arg}",
                                    "annotation": annotation_str,
                                    "default": None,
                                }
                            )

                        return_annotation = (
                            ast.unparse(body_node.returns)
                            if body_node.returns
                            else None
                        )

                        methods.append(
                            {
                                "name": body_node.name,
                                "parameters": params,
                                "returns": return_annotation,
                            }
                        )

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


def extract_function_signature(func_node):
    """Extract standard properties of a function/method signature."""
    all_args = func_node.args.posonlyargs + func_node.args.args
    defaults = func_node.args.defaults
    default_map = {}
    for i, default_node in enumerate(defaults):
        arg_idx = len(all_args) - len(defaults) + i
        default_map[id(all_args[arg_idx])] = ast.unparse(default_node)

    params = []
    for arg_node in all_args:
        annotation_str = (
            ast.unparse(arg_node.annotation)
            if arg_node.annotation
            else None
        )
        default_str = default_map.get(id(arg_node), None)
        params.append(
            {
                "name": arg_node.arg,
                "annotation": annotation_str,
                "default": default_str,
            }
        )

    if func_node.args.vararg:
        arg_node = func_node.args.vararg
        annotation_str = (
            ast.unparse(arg_node.annotation)
            if arg_node.annotation
            else None
        )
        params.append(
            {
                "name": f"*{arg_node.arg}",
                "annotation": annotation_str,
                "default": None,
            }
        )

    for kwarg, default_node in zip(
        func_node.args.kwonlyargs, func_node.args.kw_defaults
    ):
        annotation_str = (
            ast.unparse(kwarg.annotation)
            if kwarg.annotation
            else None
        )
        default_str = (
            ast.unparse(default_node)
            if default_node is not None
            else None
        )
        params.append(
            {
                "name": kwarg.arg,
                "annotation": annotation_str,
                "default": default_str,
            }
        )

    if func_node.args.kwarg:
        arg_node = func_node.args.kwarg
        annotation_str = (
            ast.unparse(arg_node.annotation)
            if arg_node.annotation
            else None
        )
        params.append(
            {
                "name": f"**{arg_node.arg}",
                "annotation": annotation_str,
                "default": None,
            }
        )

    return_annotation = (
        ast.unparse(func_node.returns)
        if func_node.returns
        else None
    )

    return {
        "name": func_node.name,
        "parameters": params,
        "returns": return_annotation,
    }


def extract_file_entities(file_path, rel_path):
    """Parse a python file statically and extract public classes and public standalone functions."""
    if not os.path.exists(file_path):
        return {}, {}

    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()

    try:
        tree = ast.parse(source, filename=file_path)
    except Exception as e:
        print(f"Warning: Failed to parse {file_path}: {e}", file=sys.stderr)
        return {}, {}

    classes = {}
    functions = {}

    for node in tree.body:
        # Public Classes
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                class_name = node.name
                methods = []
                for body_node in node.body:
                    if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not body_node.name.startswith("_"):
                            sig = extract_function_signature(body_node)
                            methods.append(sig)

                classes[class_name] = {
                    "class_name": class_name,
                    "file_path": rel_path,
                    "methods": sorted(methods, key=lambda m: m["name"]),
                }

        # Public Standalone Functions
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                func_name = node.name
                sig = extract_function_signature(node)
                sig["file_path"] = rel_path
                functions[func_name] = sig

    return classes, functions


def scan_core_modules():
    """Recursively scan app/core/ to locate all public classes, public class methods, and standalone public functions."""
    core_dir = os.path.join(BASE_DIR, "app", "core")
    if not os.path.exists(core_dir):
        return {}, {}

    classes_data = {}
    functions_data = {}

    for root, dirs, files in os.walk(core_dir):
        dirs.sort()
        for file in sorted(files):
            if file.endswith(".py"):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, BASE_DIR)
                
                file_classes, file_functions = extract_file_entities(file_path, rel_path)
                
                for class_name, class_info in file_classes.items():
                    classes_data[class_name] = class_info
                for func_name, func_info in file_functions.items():
                    functions_data[func_name] = func_info

    return classes_data, functions_data


def collect_current_definitions():
    """Parse codebase files and return the complete current definitions structure."""
    classes_data, functions_data = scan_core_modules()

    cli_data = {
        "app/main.py": extract_cli(MAIN_CLI_PATH),
        "sandbox_cli.py": extract_cli(SANDBOX_CLI_PATH),
    }

    return {
        "protocols": classes_data,
        "functions": functions_data,
        "cli": cli_data,
    }


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

    if args.regenerate:
        # Create directory if missing
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(current_definitions, f, indent=2, sort_keys=True)
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
            snapshot_definitions = json.load(f)
        except Exception as e:
            print(
                f"Error: Failed to parse checked-in baseline snapshot JSON: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Compare current against snapshot
    current_json = json.dumps(current_definitions, indent=2, sort_keys=True)
    snapshot_json = json.dumps(snapshot_definitions, indent=2, sort_keys=True)

    if current_json != snapshot_json:
        print(
            "FAIL: Public interface or CLI signature drift detected!", file=sys.stderr
        )
        print(
            "----------------------------------------------------------------",
            file=sys.stderr,
        )
        diff = list(
            difflib.unified_diff(
                snapshot_json.splitlines(keepends=True),
                current_json.splitlines(keepends=True),
                fromfile=f"Snapshot ({os.path.relpath(SNAPSHOT_PATH, BASE_DIR)})",
                tofile="Current Codebase",
            )
        )
        sys.stderr.writelines(diff)
        print(
            "----------------------------------------------------------------",
            file=sys.stderr,
        )
        print(
            "If this change was intentional, update the baseline snapshot by running:",
            file=sys.stderr,
        )
        print(
            f"  python3 {os.path.relpath(__file__, BASE_DIR)} --regenerate",
            file=sys.stderr,
        )
        sys.exit(1)

    print("SUCCESS: Codebase signatures match baseline snapshot.")
    sys.exit(0)


if __name__ == "__main__":
    main()
