#!/usr/bin/env python3
"""AST-based Signature & CLI Snapshot Validation.

This script parses developer protocols and CLI tools statically to verify
backwards compatibility against checked-in per-module API/CLI snapshot files under tests/snapshots/api/.
"""

import argparse
import ast
import difflib
import hashlib
import json
import os
import sys

BASE_DIR = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNAPSHOT_DIR = os.path.join(BASE_DIR, "tests", "snapshots", "api")
SNAPSHOT_PATH = SNAPSHOT_DIR  # Backwards-compatibility alias

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
        annotation_str = ast.unparse(kwarg.annotation) if kwarg.annotation else None
        default_str = ast.unparse(default_node) if default_node is not None else None
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
    return_annotation = ast.unparse(func_node.returns) if func_node.returns else None
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
            if node.name.startswith("_"):
                continue

            class_decorators = extract_decorators(node.decorator_list)
            methods = []

            for body_node in node.body:
                if isinstance(body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
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


def source_path_to_snapshot_path(source_rel_path: str) -> str:
    """Map a relative source file path to its snapshot JSON file path under SNAPSHOT_DIR."""
    norm = source_rel_path.replace("\\", "/")
    if norm == "app/main.py":
        rel_snap = "cli/main.json"
    elif norm == "sandbox_cli.py":
        rel_snap = "cli/sandbox_cli.json"
    elif norm.startswith("app/"):
        rel_snap = norm[len("app/") :].removesuffix(".py") + ".json"
    else:
        rel_snap = norm.removesuffix(".py") + ".json"

    return os.path.join(SNAPSHOT_DIR, rel_snap)


def snapshot_path_to_source_path(snapshot_abs_path: str) -> str:
    """Map an absolute snapshot JSON path back to its corresponding relative source file path."""
    rel_snap = safe_relpath(snapshot_abs_path, SNAPSHOT_DIR).replace("\\", "/")
    if rel_snap == "cli/main.json":
        return "app/main.py"
    if rel_snap == "cli/sandbox_cli.json":
        return "sandbox_cli.py"
    if rel_snap.startswith("cli/"):
        return f"app/cli/{rel_snap[len('cli/') :].removesuffix('.json')}.py"
    return f"app/{rel_snap.removesuffix('.json')}.py"


def compute_payload_checksum(payload: dict) -> str:
    """Compute SHA-256 checksum of canonical JSON payload definitions."""
    canonical_json = json.dumps(payload, indent=2, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def extract_metadata_and_payload(snapshot_data: dict) -> tuple[dict | None, dict]:
    """Separate metadata header from the definitions payload dictionary."""
    metadata = None
    if isinstance(snapshot_data, dict):
        if "_metadata" in snapshot_data and isinstance(
            snapshot_data["_metadata"], dict
        ):
            metadata = snapshot_data["_metadata"]
        elif "metadata" in snapshot_data and isinstance(
            snapshot_data["metadata"], dict
        ):
            metadata = snapshot_data["metadata"]

    payload = {
        k: v for k, v in snapshot_data.items() if k not in ("_metadata", "metadata")
    }
    return metadata, payload


def verify_snapshot_integrity(
    snapshot_data: dict, snapshot_path: str = None
) -> tuple[bool, str, dict]:
    """Verify inline checksum header of a snapshot data dictionary."""
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
            f"Error: Snapshot file integrity check failed: missing embedded metadata header{path_str}.\n"
            f"  To regenerate snapshot baseline files, run: python scripts/validate_signatures.py --regenerate",
            payload,
        )

    embedded_checksum = metadata.get("checksum") or metadata.get("sha256")
    if not embedded_checksum:
        path_str = f" in {snapshot_path}" if snapshot_path else ""
        return (
            False,
            f"Error: Snapshot file integrity check failed: missing checksum in metadata header{path_str}.\n"
            f"  To regenerate snapshot baseline files, run: python scripts/validate_signatures.py --regenerate",
            payload,
        )

    computed_checksum = compute_payload_checksum(payload)
    if embedded_checksum != computed_checksum:
        path_str = f" in {snapshot_path}" if snapshot_path else ""
        err = (
            f"Error: Snapshot file integrity verification failed! Checksum mismatch{path_str}.\n"
            f"  Embedded checksum: {embedded_checksum}\n"
            f"  Computed checksum: {computed_checksum}\n"
            f"  To regenerate snapshot baseline files, run: python scripts/validate_signatures.py --regenerate"
        )
        return False, err, payload

    return True, "", payload


def matches_target(source_rel: str, snapshot_abs: str, target_arg: str) -> bool:
    """Check if target_arg matches the given source or snapshot path."""
    norm_target = target_arg.replace("\\", "/").strip()
    norm_source = source_rel.replace("\\", "/")
    norm_snap = safe_relpath(snapshot_abs, BASE_DIR).replace("\\", "/")
    rel_snap = safe_relpath(snapshot_abs, SNAPSHOT_DIR).replace("\\", "/")

    candidates = {
        norm_source,
        norm_snap,
        rel_snap,
        norm_source.removesuffix(".py"),
        rel_snap.removesuffix(".json"),
        os.path.basename(norm_source),
        os.path.splitext(os.path.basename(norm_source))[0],
    }

    if norm_target in candidates:
        return True
    if (
        norm_source.endswith(norm_target)
        or norm_snap.endswith(norm_target)
        or rel_snap.endswith(norm_target)
    ):
        return True
    return False


def collect_modules_and_payloads():
    """Collect all current modules, their target snapshot paths, and extracted payloads."""
    defs = collect_current_definitions()
    modules = []

    if isinstance(defs, dict):
        cli_dict = defs.get("cli", {})
        if isinstance(cli_dict, dict):
            for source_rel, cli_calls in cli_dict.items():
                snap_path = source_path_to_snapshot_path(source_rel)
                payload = {"cli": cli_calls}
                modules.append(
                    {
                        "category": "cli",
                        "source_rel": source_rel,
                        "snapshot_path": snap_path,
                        "payload": payload,
                    }
                )

        core_dict = defs.get("core", {})
        if isinstance(core_dict, dict):
            for source_rel, core_sigs in core_dict.items():
                snap_path = source_path_to_snapshot_path(source_rel)
                payload = core_sigs
                modules.append(
                    {
                        "category": "core",
                        "source_rel": source_rel,
                        "snapshot_path": snap_path,
                        "payload": payload,
                    }
                )

    return modules


def main():
    """Run CLI snapshot validation engine."""
    parser = argparse.ArgumentParser(
        description="Verify public protocol signatures and CLI interfaces statically against modular snapshots."
    )
    parser.add_argument(
        "--update",
        "--regenerate",
        action="store_true",
        dest="regenerate",
        help="Update/regenerate verified API/CLI baseline snapshot file(s).",
    )
    parser.add_argument(
        "module_paths",
        nargs="*",
        default=[],
        help="Optional module path(s) to validate or update.",
    )
    args = parser.parse_args()

    is_ci = os.environ.get("CI", "").lower() in ("true", "1")
    all_modules = collect_modules_and_payloads()

    if args.module_paths:
        target_modules = [
            m
            for m in all_modules
            if any(
                matches_target(m["source_rel"], m["snapshot_path"], t)
                for t in args.module_paths
            )
        ]
        if not target_modules:
            print(
                f"Error: No matching modules found for specified path(s): {', '.join(args.module_paths)}",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        target_modules = all_modules

    if args.regenerate:
        if is_ci:
            print(
                "Error: Baseline regeneration is disabled in continuous integration.",
                file=sys.stderr,
            )
            sys.exit(1)

        updated_count = 0
        for m in target_modules:
            snap_path = m["snapshot_path"]
            payload = m["payload"]
            checksum = compute_payload_checksum(payload)
            file_data = {
                "_metadata": {
                    "checksum": checksum,
                },
                **payload,
            }

            if os.path.exists(snap_path) and not args.module_paths:
                try:
                    with open(snap_path, "r", encoding="utf-8") as f:
                        existing_data = json.load(f)
                    is_v, _, existing_payload = verify_snapshot_integrity(
                        existing_data, snap_path
                    )
                    if is_v and json.dumps(
                        existing_payload, sort_keys=True
                    ) == json.dumps(payload, sort_keys=True):
                        continue
                except Exception:
                    pass

            os.makedirs(os.path.dirname(snap_path), exist_ok=True)
            with open(snap_path, "w", encoding="utf-8") as f:
                json.dump(file_data, f, indent=2, sort_keys=True)
                f.write("\n")
            rel_snap = safe_relpath(snap_path, BASE_DIR)
            print(f"Updated baseline snapshot: {rel_snap}")
            updated_count += 1

        if not args.module_paths and os.path.exists(SNAPSHOT_DIR):
            valid_snap_paths = {
                os.path.realpath(m["snapshot_path"]) for m in all_modules
            }
            for root, _, files in os.walk(SNAPSHOT_DIR):
                for file in files:
                    if file.endswith(".json"):
                        full_snap = os.path.realpath(os.path.join(root, file))
                        if full_snap not in valid_snap_paths:
                            os.remove(full_snap)
                            rel_removed = safe_relpath(full_snap, BASE_DIR)
                            print(f"Removed orphaned snapshot file: {rel_removed}")

        print(
            f"Successfully processed baseline snapshot updates ({updated_count} files written)."
        )
        sys.exit(0)

    # Validation Mode
    has_errors = False
    valid_snap_paths = set()

    for m in target_modules:
        snap_path = m["snapshot_path"]
        rel_snap = safe_relpath(snap_path, BASE_DIR)
        valid_snap_paths.add(os.path.realpath(snap_path))

        if not os.path.exists(snap_path):
            print(
                f"FAIL: Baseline snapshot file missing for module '{m['source_rel']}': {rel_snap}",
                file=sys.stderr,
            )
            has_errors = True
            continue

        try:
            with open(snap_path, "r", encoding="utf-8") as f:
                snapshot_data = json.load(f)
        except Exception as e:
            print(
                f"FAIL: Failed to parse snapshot JSON at {rel_snap}: {e}",
                file=sys.stderr,
            )
            has_errors = True
            continue

        is_valid, err_msg, snapshot_payload = verify_snapshot_integrity(
            snapshot_data, rel_snap
        )
        if not is_valid:
            print(err_msg, file=sys.stderr)
            has_errors = True

        current_json = json.dumps(m["payload"], indent=2, sort_keys=True)
        snapshot_json = json.dumps(snapshot_payload, indent=2, sort_keys=True)

        if current_json != snapshot_json:
            if is_valid:
                print(
                    f"FAIL: Signature drift detected in '{m['source_rel']}' ({rel_snap})!",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Signature diff for '{m['source_rel']}' ({rel_snap}):",
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
                    fromfile=f"Snapshot ({rel_snap})",
                    tofile=f"Current Codebase ({m['source_rel']})",
                )
            )
            sys.stderr.writelines(diff)
            print(
                "----------------------------------------------------------------",
                file=sys.stderr,
            )
            has_errors = True

    if not args.module_paths and os.path.exists(SNAPSHOT_DIR):
        for root, _, files in os.walk(SNAPSHOT_DIR):
            for file in files:
                if file.endswith(".json"):
                    full_snap = os.path.realpath(os.path.join(root, file))
                    if full_snap not in valid_snap_paths:
                        rel_orphaned = safe_relpath(full_snap, BASE_DIR)
                        print(
                            f"FAIL: Orphaned snapshot file detected: {rel_orphaned}",
                            file=sys.stderr,
                        )
                        has_errors = True

    if has_errors:
        print(
            "\nAPI/CLI signature verification failed!",
            file=sys.stderr,
        )
        if not is_ci:
            print(
                "If these changes were intentional, update the baseline snapshot by running:",
                file=sys.stderr,
            )
            print(
                "  python scripts/validate_signatures.py --regenerate",
                file=sys.stderr,
            )
        else:
            print(
                "In CI, automated baseline regeneration is disabled. "
                "Please update snapshot files locally and commit the changes.",
                file=sys.stderr,
            )
        sys.exit(1)

    print("SUCCESS: Codebase signatures match baseline snapshots.")
    sys.exit(0)


if __name__ == "__main__":
    main()
