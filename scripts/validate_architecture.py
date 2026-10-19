"""Validates architectural constraints and utility anti-duplication rules across the project codebase."""

import argparse
import ast
import builtins
import copy
import glob
import hashlib
import os
import sys
from pathlib import Path

# Add project root to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Registry mapping recognized canonical utility names to their home module files
CANONICAL_UTILITY_REGISTRY = {
    "_set_posix_mode": "app/core/resilient_file_ops.py",
    "is_masked_by": "app/core/policy_engine.py",
    "_make_json_serializable": "app/core/domain_contracts.py",
    "_get_val": "app/core/domain_contracts.py",
}

# Standard python dunder methods and data model adapter methods excluded from AST structural hashing
EXCLUDED_METHOD_NAMES = {
    "__init__",
    "__getitem__",
    "__setitem__",
    "__delitem__",
    "__iter__",
    "__len__",
    "__contains__",
    "__str__",
    "__repr__",
    "__eq__",
    "__bool__",
    "dict",
    "get",
    "get_instance",
}

# Python builtins and special identifiers preserved during AST variable normalization
BUILTIN_NAMES = set(dir(builtins)) | {
    "self",
    "cls",
    "None",
    "True",
    "False",
    "Ellipsis",
}

# Allowed files for specific string constant pattern validations
ALLOWED_FOR_FROZEN = {"app/core/path_utils.py"}
ALLOWED_FOR_SESSIONS = {"app/core/path_utils.py"}
ALLOWED_FOR_KEYS = {"app/core/path_utils.py"}
ALLOWED_FOR_CHARS = {"app/core/path_utils.py"}

# Import layering: each package prefix lists the modules it must never import,
# including function-local imports. The most specific matching prefix wins.
# Tooling (scripts/, sandbox_cli, pytest) is never a runtime dependency of app/.
_TOOLING = ("scripts", "sandbox_cli", "pytest")
IMPORT_LAYER_RULES = {
    "app.core": ("app.ui", "app.cli", "app.main", "app.plugins") + _TOOLING,
    "app.plugins": ("app.cli", "app.main") + _TOOLING,
    "app.ui": ("app.cli", "app.main") + _TOOLING,
    "app.cli": ("app.ui", "app.main") + _TOOLING,
    "app.main": _TOOLING,
}

# Modules exempt from IMPORT_LAYER_RULES, with the reason. The docs generator
# that needed an exemption now lives in scripts/, outside the layering.
IMPORT_LAYER_EXEMPT: set[str] = set()


def compute_normalized_ast_hash(node: ast.AST) -> str:
    """Compute SHA256 hash of a function AST node after normalizing docstrings, type annotations, and local variable names."""
    func_copy = copy.deepcopy(node)

    # 1. Strip docstring if first statement in function body is a string literal expression
    if func_copy.body and isinstance(func_copy.body[0], ast.Expr):
        first_val = func_copy.body[0].value
        if isinstance(first_val, ast.Constant) and isinstance(first_val.value, str):
            func_copy.body.pop(0)

    # 2. Strip type annotations and type comments
    func_copy.returns = None
    if hasattr(func_copy, "type_comment"):
        func_copy.type_comment = None

    if hasattr(func_copy, "args") and func_copy.args:
        args_obj = func_copy.args
        for arg in (
            getattr(args_obj, "posonlyargs", [])
            + getattr(args_obj, "args", [])
            + getattr(args_obj, "kwonlyargs", [])
        ):
            arg.annotation = None
            if hasattr(arg, "type_comment"):
                arg.type_comment = None
        if getattr(args_obj, "vararg", None):
            args_obj.vararg.annotation = None
            if hasattr(args_obj.vararg, "type_comment"):
                args_obj.vararg.type_comment = None
        if getattr(args_obj, "kwarg", None):
            args_obj.kwarg.annotation = None
            if hasattr(args_obj.kwarg, "type_comment"):
                args_obj.kwarg.type_comment = None

    # 3. Normalize local variable / parameter names and walk tree
    var_map = {}

    class ASTNormalizer(ast.NodeTransformer):
        def visit_arg(self, n):
            self.generic_visit(n)
            if n.arg and n.arg not in BUILTIN_NAMES:
                if n.arg not in var_map:
                    var_map[n.arg] = f"v_{len(var_map)}"
                n.arg = var_map[n.arg]
            return n

        def visit_Name(self, n):
            self.generic_visit(n)
            if n.id and n.id not in BUILTIN_NAMES:
                if n.id not in var_map:
                    var_map[n.id] = f"v_{len(var_map)}"
                n.id = var_map[n.id]
            return n

        def visit_keyword(self, n):
            self.generic_visit(n)
            if n.arg and n.arg not in BUILTIN_NAMES:
                if n.arg in var_map:
                    n.arg = var_map[n.arg]
            return n

        def visit_AnnAssign(self, n):
            self.generic_visit(n)
            n.annotation = None
            return n

    normalizer = ASTNormalizer()
    func_copy = normalizer.visit(func_copy)

    # Convert to canonical AST dump without line numbers or column offsets
    dump_str = ast.dump(func_copy, annotate_fields=False, include_attributes=False)
    return hashlib.sha256(dump_str.encode("utf-8")).hexdigest()


class DuplicatePatternVisitor(ast.NodeVisitor):
    """AST visitor to find duplicate system path utilities or illegal character validations."""

    def __init__(self, filepath: str):
        self.filepath = filepath.replace("\\", "/")
        self.errors = []

    def visit_Attribute(self, node):
        """Visit attribute nodes to look for sys.frozen or sys._MEIPASS usage."""
        if self.filepath not in ALLOWED_FOR_FROZEN:
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "sys"
                and node.attr in ("frozen", "_MEIPASS")
            ):
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Direct 'sys.{node.attr}' usage found. "
                    "Use 'app.core.path_utils.is_packaged()' or 'get_meipass_dir()' instead."
                )
        self.generic_visit(node)

    def visit_Call(self, node):
        """Visit function call nodes to look for getattr(sys, 'frozen' / '_MEIPASS') or hasattr(sys, ...)."""
        if self.filepath not in ALLOWED_FOR_FROZEN:
            if isinstance(node.func, ast.Name) and node.func.id in (
                "getattr",
                "hasattr",
            ):
                if len(node.args) >= 2:
                    arg0, arg1 = node.args[0], node.args[1]
                    if isinstance(arg0, ast.Name) and arg0.id == "sys":
                        if isinstance(arg1, ast.Constant) and arg1.value in (
                            "frozen",
                            "_MEIPASS",
                        ):
                            self.errors.append(
                                f"{self.filepath}:{node.lineno}: Direct {node.func.id}(sys, '{arg1.value}') usage found. "
                                "Use 'app.core.path_utils.is_packaged()' or 'get_meipass_dir()' instead."
                            )
        self.generic_visit(node)

    def visit_Constant(self, node):
        """Visit constant nodes to check for forbidden hardcoded strings."""
        if isinstance(node.value, str):
            val = node.value

            # Check for "autosorter_sessions"
            if self.filepath not in ALLOWED_FOR_SESSIONS:
                if "autosorter_sessions" in val:
                    self.errors.append(
                        f"{self.filepath}:{node.lineno}: Direct reference to 'autosorter_sessions' folder found. "
                        "Use 'app.core.path_utils.get_session_base_dir()' or 'setup_session_directory()' instead."
                    )

            # Check for hardcoded "secret.key"
            if self.filepath not in ALLOWED_FOR_KEYS:
                if "secret.key" in val:
                    self.errors.append(
                        f"{self.filepath}:{node.lineno}: Direct reference to 'secret.key' database key file found. "
                        "Use 'app.core.path_utils.resolve_db_crypto(db_path)' instead."
                    )

            # Check for hardcoded character validations
            if self.filepath not in ALLOWED_FOR_CHARS:
                if val == '<>:"|?*' or val == '[<>:"/\\|?*]':
                    self.errors.append(
                        f"{self.filepath}:{node.lineno}: Hardcoded illegal character set or regex pattern '{val}' found. "
                        "Use shared validators/sanitizers in 'app.core.path_utils' instead."
                    )
        self.generic_visit(node)


class UtilityAntiDuplicationVisitor(ast.NodeVisitor):
    """AST visitor to detect re-definitions of canonical utilities and index structural function body hashes."""

    def __init__(self, filepath: str, errors: list, function_hashes: dict):
        self.filepath = filepath.replace("\\", "/")
        self.errors = errors
        self.function_hashes = function_hashes

    def _check_function(self, node):
        func_name = node.name

        # Requirement 2 & 3: Check for re-definition of canonical utilities outside home module
        if func_name in CANONICAL_UTILITY_REGISTRY:
            expected_home = CANONICAL_UTILITY_REGISTRY[func_name]
            if self.filepath != expected_home:
                home_module = expected_home.replace(".py", "").replace("/", ".")
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Re-definition of canonical utility '{func_name}' found. "
                    f"Use '{home_module}.{func_name}' instead."
                )

        # Requirement 4: Record function hash for structural anti-duplication comparison if non-excluded
        if func_name not in EXCLUDED_METHOD_NAMES:
            # Check if function body is non-trivial (more than 1 statement, or not a simple stub)
            if len(node.body) > 1 or (
                node.body
                and not isinstance(node.body[0], (ast.Pass, ast.Return, ast.Raise))
            ):
                h = compute_normalized_ast_hash(node)
                self.function_hashes.setdefault(h, []).append(
                    (self.filepath, node.lineno, func_name)
                )

        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_function(node)


def validate_codebase_duplication(
    errors: list, target_files: set[str] | None = None
) -> None:
    """Traverse app/ directory to run primitive duplicate checks, canonical utility re-definition checks, and structural function hash comparison."""
    function_hashes = {}

    for root, _, files in os.walk("app"):
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file).replace("\\", "/")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                tree = ast.parse(content, filename=filepath)
            except Exception:
                continue

            # Run primitive duplicate pattern checks
            if target_files is None or filepath in target_files:
                prim_visitor = DuplicatePatternVisitor(filepath)
                prim_visitor.visit(tree)
                errors.extend(prim_visitor.errors)

            # Run AST anti-duplication visitor
            file_errors = [] if target_files is not None else errors
            anti_dup_visitor = UtilityAntiDuplicationVisitor(
                filepath, file_errors, function_hashes
            )
            anti_dup_visitor.visit(tree)
            if target_files is not None and filepath in target_files:
                errors.extend(file_errors)

    # Compare normalized function hashes across distinct files
    for h, locs in function_hashes.items():
        distinct_files = {loc[0] for loc in locs}
        if len(distinct_files) > 1:
            if target_files is None:
                primary_file, primary_line, primary_name = locs[0]
                for dup_file, dup_line, dup_name in locs[1:]:
                    if dup_file != primary_file:
                        errors.append(
                            f"{dup_file}:{dup_line}: Duplicate function body found for '{dup_name}'. "
                            f"Structurally identical to function '{primary_name}' in {primary_file}:{primary_line}. "
                            "Consolidate logic into a shared module."
                        )
            else:
                for curr_file, curr_line, curr_name in locs:
                    if curr_file in target_files:
                        other_locs = [
                            (f, line, name) for f, line, name in locs if f != curr_file
                        ]
                        if other_locs:
                            other_file, other_line, other_name = other_locs[0]
                            errors.append(
                                f"{curr_file}:{curr_line}: Duplicate function body found for '{curr_name}'. "
                                f"Structurally identical to function '{other_name}' in {other_file}:{other_line}. "
                                "Consolidate logic into a shared module."
                            )


def validate_diagram_schema_and_assets(errors: list) -> None:
    """Validate that Pydantic diagram specifications represent all active core modules and match generated assets."""
    try:
        from scripts.diagram_schema import CORE_ARCHITECTURE_SPEC, SYSTEM_DIAGRAM_SPECS

        # 1. Verify all active app/core/*.py submodules are represented in core_architecture diagram spec
        core_files = glob.glob("app/core/*.py")
        core_modules = {
            f"app.core.{Path(p).stem}"
            for p in core_files
            if not p.endswith("__init__.py")
        }

        arch_spec = SYSTEM_DIAGRAM_SPECS.get(
            "core_architecture", CORE_ARCHITECTURE_SPEC
        )
        represented_labels = {node.label for node in arch_spec.nodes}

        for mod in sorted(core_modules):
            if not any(mod in label for label in represented_labels):
                errors.append(
                    f"Diagram validation error: Module '{mod}' is not represented in 'core_architecture' diagram schema specification."
                )

        # 2. Check that compiled diagram assets exist and match spec Mermaid output
        diagrams_dir = Path("docs/assets/diagrams")
        for spec_id, spec in SYSTEM_DIAGRAM_SPECS.items():
            mmd_file = diagrams_dir / f"{spec_id}.mmd"
            svg_file = diagrams_dir / f"{spec_id}.svg"
            if not mmd_file.exists():
                errors.append(
                    f"Diagram asset error: Compiled file '{mmd_file}' does not exist for spec '{spec_id}'."
                )
            if not svg_file.exists():
                errors.append(
                    f"Diagram asset error: Compiled SVG file '{svg_file}' does not exist for spec '{spec_id}'."
                )

            if mmd_file.exists():
                expected_mmd = spec.to_mermaid().replace("\r\n", "\n")
                actual_mmd = mmd_file.read_text(encoding="utf-8").replace("\r\n", "\n")
                if expected_mmd != actual_mmd:
                    errors.append(
                        f"Diagram asset error: '{mmd_file}' is out of sync with Pydantic diagram spec '{spec_id}'."
                    )
    except Exception as e:
        errors.append(f"Failed diagram schema validation check: {e}")


def _module_name(filepath: str) -> str:
    parts = list(Path(filepath).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def validate_import_layers(errors: list, filepath: str, tree: ast.AST) -> None:
    """Flag imports that cross a layer boundary in IMPORT_LAYER_RULES.

    Walks the whole tree, so imports hidden inside functions are caught too.
    """
    module = _module_name(filepath)
    if module in IMPORT_LAYER_EXEMPT:
        return
    layers = [p for p in IMPORT_LAYER_RULES if _matches(module, p)]
    if not layers:
        return
    forbidden = IMPORT_LAYER_RULES[max(layers, key=len)]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            for banned in forbidden:
                if _matches(name, banned):
                    errors.append(
                        f"{filepath}:{node.lineno}: Layer violation: '{module}' imports '{name}' ('{banned}' is off-limits here)"
                    )


def main():
    """Execute all architectural validation checks."""
    parser = argparse.ArgumentParser(
        description="Validates architectural constraints and utility anti-duplication rules across the project codebase."
    )
    parser.add_argument(
        "files",
        nargs="*",
        default=[],
        help="Optional staged file path(s) to validate.",
    )
    args = parser.parse_args()

    is_full_scan = not args.files

    if not is_full_scan:
        staged_py_files = set()
        for f in args.files:
            norm_f = f.replace("\\", "/")
            if norm_f.endswith(".py") and os.path.exists(norm_f):
                rel = os.path.relpath(norm_f, ".").replace("\\", "/")
                if rel.startswith("app/"):
                    staged_py_files.add(rel)
        if not staged_py_files:
            print(
                "No relevant staged Python files under app/ to validate for architecture."
            )
            sys.exit(0)
        target_files = staged_py_files
    else:
        target_files = None

    errors = []

    # Rule 1 & Rule 2: Async DB writes and UI blocking operations
    for root, _, files in os.walk("app"):
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file).replace("\\", "/")
            if target_files is not None and filepath not in target_files:
                continue

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                tree = ast.parse(content, filename=filepath)
            except SyntaxError:
                continue

            validate_import_layers(errors, filepath, tree)

            is_ui_file = "app/ui" in filepath

            class Visitor(ast.NodeVisitor):
                def __init__(self):
                    self.async_context = []

                def visit_AsyncFunctionDef(self, node):
                    self.async_context.append(node.name)
                    self.generic_visit(node)
                    self.async_context.pop()

                def visit_Call(self, node):
                    if not self.async_context:
                        self.generic_visit(node)
                        return

                    # Direct DB writes in async def (execute, executemany, commit)
                    if isinstance(node.func, ast.Attribute):
                        method_name = node.func.attr
                        if method_name in ("execute", "executemany", "commit"):
                            if method_name == "commit":
                                errors.append(
                                    f"{filepath}:{node.lineno}: Direct database commit() inside async function '{self.async_context[-1]}'"
                                )
                            elif method_name in ("execute", "executemany"):
                                if (
                                    node.args
                                    and isinstance(node.args[0], ast.Constant)
                                    and isinstance(node.args[0].value, str)
                                ):
                                    query = node.args[0].value.upper()
                                    if any(
                                        q in query
                                        for q in (
                                            "INSERT ",
                                            "UPDATE ",
                                            "DELETE ",
                                            "CREATE ",
                                            "DROP ",
                                        )
                                    ):
                                        errors.append(
                                            f"{filepath}:{node.lineno}: Direct database write ({method_name} with {query.split()[0]}) inside async function '{self.async_context[-1]}'"
                                        )

                    # Synchronous blocking operations in UI-bound logic
                    if is_ui_file:
                        if (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "sleep"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "time"
                        ):
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous time.sleep() inside async function '{self.async_context[-1]}'"
                            )

                        if (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr
                            in ("get", "post", "put", "delete", "patch")
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "requests"
                        ):
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous requests.{node.func.attr}() inside async function '{self.async_context[-1]}'"
                            )

                        if isinstance(node.func, ast.Name) and node.func.id == "open":
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous open() inside async function '{self.async_context[-1]}'"
                            )

                        if isinstance(
                            node.func, ast.Attribute
                        ) and node.func.attr.startswith(("read_", "write_")):
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous Path.{node.func.attr}() inside async function '{self.async_context[-1]}'"
                            )

                    self.generic_visit(node)

            Visitor().visit(tree)

    # Rule 3: Anti-duplication, canonical utility registration, and primitive duplicate pattern checks
    validate_codebase_duplication(errors, target_files=target_files)

    # Rule 4: Validate Pydantic diagram specs and asset sync
    if is_full_scan or (
        target_files is not None
        and any("app/core" in f or "diagram" in f for f in target_files)
    ):
        validate_diagram_schema_and_assets(errors)

    if errors:
        print("Architectural Violations Found:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print("No architectural violations found.")
        sys.exit(0)


if __name__ == "__main__":
    main()
