"""Validates architectural constraints and utility anti-duplication rules across the project codebase."""

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
        """Visit attribute nodes to look for sys.frozen usage."""
        if self.filepath not in ALLOWED_FOR_FROZEN:
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "sys"
                and node.attr == "frozen"
            ):
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Direct 'sys.frozen' usage found. "
                    "Use 'app.core.path_utils.is_packaged()' instead."
                )
        self.generic_visit(node)

    def visit_Call(self, node):
        """Visit function call nodes to look for getattr(sys, 'frozen')."""
        if self.filepath not in ALLOWED_FOR_FROZEN:
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                if len(node.args) >= 2:
                    arg0, arg1 = node.args[0], node.args[1]
                    if isinstance(arg0, ast.Name) and arg0.id == "sys":
                        if isinstance(arg1, ast.Constant) and arg1.value == "frozen":
                            self.errors.append(
                                f"{self.filepath}:{node.lineno}: Direct getattr(sys, 'frozen') usage found. "
                                "Use 'app.core.path_utils.is_packaged()' instead."
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


def validate_codebase_duplication(errors: list) -> None:
    """Traverse app/ directory to run primitive duplicate checks, canonical utility re-definition checks, and structural function hash comparison."""
    function_hashes = {}

    for root, _, files in os.walk("app"):
        if "binaries" in root.split(os.sep) or "app/binaries" in root.replace(
            "\\", "/"
        ):
            continue
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
            prim_visitor = DuplicatePatternVisitor(filepath)
            prim_visitor.visit(tree)
            errors.extend(prim_visitor.errors)

            # Run AST anti-duplication visitor
            anti_dup_visitor = UtilityAntiDuplicationVisitor(
                filepath, errors, function_hashes
            )
            anti_dup_visitor.visit(tree)

    # Compare normalized function hashes across distinct files
    for h, locs in function_hashes.items():
        distinct_files = {loc[0] for loc in locs}
        if len(distinct_files) > 1:
            primary_file, primary_line, primary_name = locs[0]
            for dup_file, dup_line, dup_name in locs[1:]:
                if dup_file != primary_file:
                    errors.append(
                        f"{dup_file}:{dup_line}: Duplicate function body found for '{dup_name}'. "
                        f"Structurally identical to function '{primary_name}' in {primary_file}:{primary_line}. "
                        "Consolidate logic into a shared module."
                    )


def validate_diagram_schema_and_assets(errors: list) -> None:
    """Validate that Pydantic diagram specifications represent all active core modules and match generated assets."""
    try:
        from app.core.diagram_schema import CORE_ARCHITECTURE_SPEC, SYSTEM_DIAGRAM_SPECS

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


def main():
    """Execute all architectural validation checks."""
    errors = []

    # Rule 1 & Rule 2: Async DB writes and UI blocking operations
    for root, _, files in os.walk("app"):
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file).replace("\\", "/")
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                tree = ast.parse(content, filename=filepath)
            except SyntaxError:
                continue

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
    validate_codebase_duplication(errors)

    # Rule 4: Validate Pydantic diagram specs and asset sync
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
