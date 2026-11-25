"""Validates architectural constraints and utility anti-duplication rules across the project codebase."""

import argparse
import ast
import builtins
import glob
import hashlib
import os
import re
import sys
from collections import deque
from collections.abc import Iterable, Sequence
from pathlib import Path

# Add project root to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Registry mapping recognized canonical utility names to their home module files
CANONICAL_UTILITY_REGISTRY = {
    "_set_posix_mode": "app/core/resilient_file_ops.py",
    "is_masked_by": "app/core/policy_engine.py",
    "_make_json_serializable": "app/core/domain_contracts.py",
    "_get_val": "app/core/domain_contracts.py",
    "DictCompatMixin": "app/core/domain_contracts.py",
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
    "state",
}

# Canonical dictionary emulation methods registered to DictCompatMixin in app/core/domain_contracts.py
CANONICAL_DICT_METHODS = {
    "__getitem__",
    "__contains__",
    "dict",
    "get",
}

# Domain contract models that must inherit from DictCompatMixin rather than defining redundant adapter methods
REDUNDANT_DICT_MODELS = {
    "QuarantineRecordModel",
    "SortingPlanNodeModel",
    "CorpusExampleModel",
    "VectorBatchPayloadModel",
    "FoundEssentialDocument",
    "MissingEssentialDocument",
    "ClinicalComplianceResult",
    "StudyIngestSummary",
    "MasterPipelineResult",
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
    "app.plugins": ("app.cli", "app.main", "app.ui.tui") + _TOOLING,
    "app.ui": ("app.cli", "app.main", "app.core.history") + _TOOLING,
    "app.cli": ("app.ui", "app.main") + _TOOLING,
    "app.main": _TOOLING,
}

# Modules exempt from IMPORT_LAYER_RULES, with the reason. The docs generator
# that needed an exemption now lives in scripts/, outside the layering.
IMPORT_LAYER_EXEMPT: set[str] = set()


def compute_normalized_ast_hash(node: ast.AST) -> str:
    """Compute SHA256 hash of a function AST node using read-only serialization after normalizing function names, docstrings, type annotations, and local variable names."""
    is_root_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    root_args = node.args if is_root_func else None
    top_arg_ids: set[int] = set()
    if root_args is not None:
        for a in root_args.posonlyargs + root_args.args + root_args.kwonlyargs:
            top_arg_ids.add(id(a))
        if root_args.vararg is not None:
            top_arg_ids.add(id(root_args.vararg))
        if root_args.kwarg is not None:
            top_arg_ids.add(id(root_args.kwarg))

    root_body: list[ast.stmt] | None = None
    if is_root_func:
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            root_body = body[1:]
        else:
            root_body = body

    var_map: dict[str, str] = {}

    def _map_var(name: str) -> str:
        if name and name not in BUILTIN_NAMES:
            mapped = var_map.get(name)
            if mapped is None:
                mapped = f"v_{len(var_map)}"
                var_map[name] = mapped
            return mapped
        return name

    def _serialize(val: object) -> str:
        if isinstance(val, ast.AST):
            if isinstance(val, ast.AnnAssign):
                assert val.value is not None
                target_s = _serialize(val.target)
                value_s = _serialize(val.value)
                return f"Assign([{target_s}], {value_s})"

            cls = type(val)
            if isinstance(val, ast.arg):
                is_top = id(val) in top_arg_ids
                ann_val = None if is_top else getattr(val, "annotation", None)
                tc_val = None if is_top else getattr(val, "type_comment", None)
                ann_s = _serialize(ann_val) if ann_val is not None else None
                tc_s = _serialize(tc_val) if tc_val is not None else None
                arg_name = _map_var(val.arg) if val.arg else val.arg
                field_vals = (
                    ("arg", arg_name, repr(arg_name)),
                    ("annotation", ann_val, ann_s),
                    ("type_comment", tc_val, tc_s),
                )
                args = []
                keywords = False
                for fname, raw_v, fmt_v in field_vals:
                    if raw_v is None and getattr(cls, fname, ...) is None:
                        keywords = True
                        continue
                    s = fmt_v if fmt_v is not None else repr(raw_v)
                    args.append(f"{fname}={s}" if keywords else s)
                return f"arg({', '.join(args)})"

            if isinstance(val, ast.Name):
                ctx_s = _serialize(val.ctx)
                norm_id = _map_var(val.id) if val.id else val.id
                return f"Name({repr(norm_id)}, {ctx_s})"

            if isinstance(val, ast.keyword):
                val_s = _serialize(val.value)
                kw_arg = val.arg
                if kw_arg and kw_arg not in BUILTIN_NAMES and kw_arg in var_map:
                    kw_arg = var_map[kw_arg]
                if kw_arg is None:
                    return f"keyword(value={val_s})"
                return f"keyword({repr(kw_arg)}, {val_s})"

            args = []
            keywords = False
            is_root = val is node and is_root_func
            for fname in val._fields:
                if is_root and fname == "name":
                    fval: object = ""
                elif is_root and fname == "body":
                    fval = root_body
                elif is_root and fname in ("returns", "type_comment"):
                    fval = None
                else:
                    try:
                        fval = getattr(val, fname)
                    except AttributeError:
                        keywords = True
                        continue
                if fval is None and getattr(cls, fname, ...) is None:
                    keywords = True
                    continue
                s = _serialize(fval)
                args.append(f"{fname}={s}" if keywords else s)
            return f"{cls.__name__}({', '.join(args)})"

        if isinstance(val, list):
            items = [
                _serialize(x)
                for x in val
                if not (isinstance(x, ast.AnnAssign) and x.value is None)
            ]
            return f"[{', '.join(items)}]"

        return repr(val)

    if isinstance(node, ast.AnnAssign) and node.value is None:
        dump_str = "None"
    else:
        dump_str = _serialize(node)
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
        self.current_class: str | None = None
        self.function_depth = 0

    def visit_ClassDef(self, node):
        class_name = node.name
        if class_name in CANONICAL_UTILITY_REGISTRY:
            expected_home = CANONICAL_UTILITY_REGISTRY[class_name]
            if self.filepath != expected_home:
                home_module = expected_home.replace(".py", "").replace("/", ".")
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Re-definition of canonical utility '{class_name}' found. "
                    f"Use '{home_module}.{class_name}' instead."
                )
        prev_class = self.current_class
        self.current_class = class_name
        self.generic_visit(node)
        self.current_class = prev_class

    def _check_function(self, node):
        func_name = node.name

        # Requirement 2 & 3: Check for re-definition of canonical utilities outside home module (all depths)
        if func_name in CANONICAL_UTILITY_REGISTRY:
            expected_home = CANONICAL_UTILITY_REGISTRY[func_name]
            if self.filepath != expected_home:
                home_module = expected_home.replace(".py", "").replace("/", ".")
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Re-definition of canonical utility '{func_name}' found. "
                    f"Use '{home_module}.{func_name}' instead."
                )

        # Requirement 3b: Flag redundant dictionary adapter methods on domain contract models
        if (
            self.function_depth == 0
            and (
                self.filepath == "app/core/domain_contracts.py"
                or self.filepath.startswith("app/plugins/clinical_compliance/")
            )
            and (
                self.current_class in REDUNDANT_DICT_MODELS
                or (
                    self.filepath == "app/core/domain_contracts.py"
                    and self.current_class
                    and self.current_class
                    not in (
                        "DictCompatMixin",
                        "SortingPlanModel",
                        "CorpusPreFetchBatchModel",
                    )
                )
            )
            and func_name in CANONICAL_DICT_METHODS
        ):
            self.errors.append(
                f"{self.filepath}:{node.lineno}: Redundant dictionary adapter method '{func_name}' found in class '{self.current_class}'. "
                f"Use canonical 'DictCompatMixin' from 'app.core.domain_contracts' instead."
            )

        # Requirement 4: Record function hash only for outer functions/methods (skip nested closures)
        if (
            self.function_depth == 0
            and func_name not in EXCLUDED_METHOD_NAMES
            and not _is_overload_decorated(node)
        ):
            effective_body = _effective_function_body(node)
            # Check if function body is non-trivial after stripping any leading docstring
            if len(effective_body) > 1 or (
                effective_body and not _is_trivial_stub_statement(effective_body[0])
            ):
                h = compute_normalized_ast_hash(node)
                self.function_hashes.setdefault(h, []).append(
                    (self.filepath, node.lineno, func_name)
                )

        self.function_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self.function_depth -= 1

    def visit_FunctionDef(self, node):
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_function(node)


def _is_overload_decorated(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when function is a typing.overload signature stub."""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "overload":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "overload":
            return True
    return False


def _effective_function_body(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.stmt]:
    """Return function body statements with any leading docstring literal removed."""
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _is_trivial_stub_statement(stmt: ast.stmt) -> bool:
    """Return True for single-statement Pass, Return, Raise, or Ellipsis stubs."""
    if isinstance(stmt, (ast.Pass, ast.Return, ast.Raise)):
        return True
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    )


def _collect_parsed_app_files(app_root: str = "app") -> list[tuple[str, ast.Module]]:
    """Walk app_root once and parse each Python source file into an AST."""
    parsed_files: list[tuple[str, ast.Module]] = []
    for root, _, files in os.walk(app_root):
        for file in files:
            if not file.endswith(".py"):
                continue
            filepath = os.path.join(root, file).replace("\\", "/")
            try:
                with open(filepath, encoding="utf-8") as f:
                    content = f.read()
                tree = ast.parse(content, filename=filepath)
            except Exception:
                continue
            parsed_files.append((filepath, tree))
    return parsed_files


def _report_duplicate_function_hashes(
    errors: list,
    function_hashes: dict[str, list[tuple[str, int, str]]],
    target_files: set[str] | None = None,
) -> None:
    """Compare normalized function hashes across same-file and cross-file definitions."""
    for _h, locs in function_hashes.items():
        if len(locs) > 1:
            if target_files is None:
                for idx, (dup_file, dup_line, dup_name) in enumerate(locs):
                    prior_matches = [
                        loc
                        for loc in locs[:idx]
                        if loc[0] != dup_file or loc[2] == dup_name
                    ]
                    if prior_matches:
                        primary_file, primary_line, primary_name = prior_matches[0]
                        errors.append(
                            f"{dup_file}:{dup_line}: Duplicate function body found for '{dup_name}'. "
                            f"Structurally identical to function '{primary_name}' in {primary_file}:{primary_line}. "
                            "Consolidate logic into a shared module or helper."
                        )
            else:
                for idx, (curr_file, curr_line, curr_name) in enumerate(locs):
                    if curr_file in target_files:
                        other_locs = [
                            loc
                            for j, loc in enumerate(locs)
                            if j != idx and (loc[0] != curr_file or loc[2] == curr_name)
                        ]
                        if other_locs:
                            other_file, other_line, other_name = other_locs[0]
                            errors.append(
                                f"{curr_file}:{curr_line}: Duplicate function body found for '{curr_name}'. "
                                f"Structurally identical to function '{other_name}' in {other_file}:{other_line}. "
                                "Consolidate logic into a shared module or helper."
                            )


def validate_codebase_duplication(
    errors: list,
    target_files: set[str] | None = None,
    parsed_files: list[tuple[str, ast.Module]] | None = None,
) -> None:
    """Traverse app/ ASTs to run primitive duplicate checks, canonical utility re-definition checks, and structural function hash comparison."""
    function_hashes: dict[str, list[tuple[str, int, str]]] = {}
    app_asts = (
        parsed_files if parsed_files is not None else _collect_parsed_app_files("app")
    )

    for filepath, tree in app_asts:
        norm_path = filepath.replace("\\", "/")
        if target_files is None or norm_path in target_files:
            ArchitectureASTVisitor(
                filepath,
                errors,
                function_hashes=function_hashes,
                checks=_CHECK_DUPLICATE_PATTERNS | _CHECK_ANTI_DUPLICATION,
            ).visit(tree)
        else:
            ArchitectureASTVisitor(
                filepath,
                [],
                function_hashes=function_hashes,
                checks=_CHECK_HASH_INDEX_ONLY,
            ).visit(tree)

    _report_duplicate_function_hashes(
        errors, function_hashes, target_files=target_files
    )


def _extract_base_class_name(base: ast.expr) -> str | None:
    """Extract terminal class identifier from a ClassDef base expression."""
    target = base.value if isinstance(base, ast.Subscript) else base
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", flags=re.DOTALL)
_MODAL_BREAKPOINT_RE = re.compile(r"(?<![\w-])\.(?:compact|narrow)(?![\w-])")
_MODAL_AMPERSAND_PREFIX_RE = re.compile(r"&(?:\.[\w-]+)*$")


def _has_inlined_modal_shell_css(css_text: str, cls_name: str = "") -> bool:
    """Return True if a modal CSS string re-inlines shared BASE_MODAL_CSS shell rules."""
    for match in _CSS_RULE_RE.finditer(css_text):
        selector = match.group(1).strip()
        decls = match.group(2)
        if "rgba(0, 0, 0, 0.6)" in decls:
            return True
        if (
            (cls_name and cls_name in selector)
            or "ModalScreen" in selector
            or "ResponsiveModalScreen" in selector
        ) and "align: center middle" in decls:
            return True
        if (
            ".modal-body" in selector
            and "overflow-y: auto" in decls
            and (
                "max-height: 100%" in decls
                or ("height: 1fr" in decls and "min-height: 0" in decls)
            )
        ):
            return True
        if (
            ".button-row" in selector
            and "dock: bottom" in decls
            and "align: right middle" in decls
        ):
            return True
    return False


def _has_unnested_modal_breakpoint_css(css_text: str) -> bool:
    """Return True if a modal stylesheet uses .compact or .narrow without nesting via '&.' inside a modal block."""
    cleaned = _CSS_COMMENT_RE.sub("", css_text)
    depth = 0
    buf: list[str] = []
    for ch in cleaned:
        if ch == "{":
            selector_group = "".join(buf).strip()
            buf.clear()
            for match in _MODAL_BREAKPOINT_RE.finditer(selector_group):
                if depth == 0:
                    return True
                prefix = selector_group[: match.start()]
                if not _MODAL_AMPERSAND_PREFIX_RE.search(prefix):
                    return True
            depth += 1
        elif ch == "}":
            buf.clear()
            depth = max(0, depth - 1)
        elif ch == ";":
            buf.clear()
        else:
            buf.append(ch)
    return False


def validate_modal_shell_conventions(
    errors: list,
    target_files: set[str] | None = None,
    parsed_files: list[tuple[str, ast.Module]] | None = None,
) -> None:
    """Enforce ResponsiveModalScreen inheritance, shared shell rules, and nested modal breakpoint CSS."""
    app_asts = (
        parsed_files if parsed_files is not None else _collect_parsed_app_files("app")
    )
    for filepath, tree in app_asts:
        norm_path = filepath.replace("\\", "/")
        if target_files is not None and norm_path not in target_files:
            continue
        if norm_path == "app/ui/layout.py":
            continue
        ArchitectureASTVisitor(norm_path, errors, checks=_CHECK_MODAL_SHELL).visit(tree)


def validate_diagram_schema_and_assets(
    errors: list,
    target_spec_ids: set[str] | None = None,
    check_core_modules: bool = True,
    explicit_diagram_files: set[str] | None = None,
) -> None:
    """Validate that Pydantic diagram specifications represent all active core modules and match generated .mmd assets."""
    try:
        from scripts.diagram_schema import SYSTEM_DIAGRAM_SPECS
        from scripts.diagram_toolchain import validate_mermaid_syntax

        has_live_core = Path("app/core").is_dir()

        # 1. Verify all active app/core/*.py submodules are represented in core_architecture diagram spec
        if check_core_modules:
            core_files = glob.glob("app/core/*.py")
            core_modules = {
                f"app.core.{Path(p).stem}"
                for p in core_files
                if not p.endswith("__init__.py")
            }

            arch_spec = SYSTEM_DIAGRAM_SPECS["core_architecture"]
            represented_labels = {node.label for node in arch_spec.nodes}

            for mod in sorted(core_modules):
                if not any(mod in label for label in represented_labels):
                    errors.append(
                        f"Diagram validation error: Module '{mod}' is not represented in 'core_architecture' diagram schema specification."
                    )

        # 2. Check that compiled .mmd diagram sources exist, have valid Mermaid syntax, and match spec output
        # Hydrate only targeted keys via LazyDiagramSpecsDict rather than .items()
        diagrams_dir = Path("docs/assets/diagrams")
        explicit_set = explicit_diagram_files or set()
        spec_keys = (
            list(SYSTEM_DIAGRAM_SPECS.keys())
            if target_spec_ids is None
            else sorted(target_spec_ids)
        )
        for spec_id in spec_keys:
            if spec_id not in SYSTEM_DIAGRAM_SPECS:
                errors.append(
                    f"Diagram validation error: Unknown diagram spec '{spec_id}'."
                )
                continue
            spec = SYSTEM_DIAGRAM_SPECS[spec_id]
            expected_mmd = spec.to_mermaid().replace("\r\n", "\n")
            for syn_err in validate_mermaid_syntax(expected_mmd):
                errors.append(f"Diagram syntax error in '{spec_id}': {syn_err}")

            mmd_file = diagrams_dir / f"{spec_id}.mmd"
            mmd_rel = f"docs/assets/diagrams/{spec_id}.mmd"
            if (
                spec_id == "core_architecture"
                and has_live_core
                and mmd_rel not in explicit_set
            ):
                # core_architecture is derived dynamically from app/ ASTs; skip static .mmd drift when validating app/ edits
                continue

            if not mmd_file.exists():
                errors.append(
                    f"Diagram asset error: Compiled file '{mmd_file}' does not exist for spec '{spec_id}'."
                )
            else:
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
    ArchitectureASTVisitor(filepath, errors, checks=_CHECK_IMPORT_LAYERS).visit(tree)


SETTINGS_SOURCE = "app/config.py"


def validate_service_ownership(errors: list, filepath: str, tree: ast.AST) -> None:
    """Keep runtime writer construction in the managed composition root."""
    ArchitectureASTVisitor(filepath, errors, checks=_CHECK_SERVICE_OWNERSHIP).visit(
        tree
    )


def _declared_settings_keys_from_tree(tree: ast.Module) -> set[str] | None:
    """Extract declared field and property names on `Settings` from a parsed AST."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            keys: set[str] = set()
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    keys.add(stmt.target.id)
                elif isinstance(stmt, ast.FunctionDef):
                    keys.add(stmt.name)
            return keys
    return None


def declared_settings_keys(
    config_path: str = os.path.join(os.path.dirname(__file__), "..", SETTINGS_SOURCE),
) -> set[str]:
    """Return the field and property names declared on the `Settings` model.

    Read from the source rather than imported, so the check needs no app imports.
    """
    with open(config_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=config_path)
    keys = _declared_settings_keys_from_tree(tree)
    if keys is not None:
        return keys
    raise ValueError(f"No Settings class found in {config_path}")


def _is_settings_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id.endswith("settings")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("settings")
    return False


def validate_settings_keys(
    errors: list, filepath: str, tree: ast.AST, declared: set[str]
) -> None:
    """Flag `getattr(settings, "KEY", ...)` reads of keys `Settings` doesn't declare.

    `AppSettings` raises for unknown keys, so such a read always falls back to
    its default and the setting can never be configured.
    """
    ArchitectureASTVisitor(
        filepath, errors, declared_settings=declared, checks=_CHECK_SETTINGS_KEYS
    ).visit(tree)


def _is_global_target(target: ast.AST) -> bool:
    """True for ``sys.modules[...]`` or an attribute chain rooted at ``socket``."""
    if isinstance(target, ast.Subscript):
        value = target.value
        return (
            isinstance(value, ast.Attribute)
            and value.attr == "modules"
            and isinstance(value.value, ast.Name)
            and value.value.id == "sys"
        )
    if isinstance(target, ast.Attribute):
        root = target.value
        while isinstance(root, ast.Attribute):
            root = root.value
        return isinstance(root, ast.Name) and root.id == "socket"
    return False


def _find_global_target(target: ast.AST) -> ast.AST | None:
    """Breadth-first search over an assignment/delete target for a global patch target."""
    queue: deque[ast.AST] = deque([target])
    while queue:
        part = queue.popleft()
        if _is_global_target(part):
            return part
        queue.extend(ast.iter_child_nodes(part))
    return None


def validate_import_time_globals(errors: list, filepath: str, tree: ast.AST) -> None:
    """Flag module-level writes to ``sys.modules[...]`` or ``socket.*`` (#711).

    Such patches belong in an explicit install() that an entrypoint calls, so
    statements inside functions are allowed; class bodies run at import and
    are checked.
    """
    ArchitectureASTVisitor(filepath, errors, checks=_CHECK_IMPORT_TIME_GLOBALS).visit(
        tree
    )


_TEST_DETECTION_NAMES = frozenset(
    {"MagicMock", "NonCallableMock", "mock_add_spec", "PYTEST_CURRENT_TEST"}
)


def validate_no_test_detection(errors: list, filepath: str, tree: ast.AST) -> None:
    """Flag production code that detects it is under test (#720).

    Branches on ``unittest.mock`` types or ``PYTEST_CURRENT_TEST`` mean tests
    exercise a path production never runs; inject the dependency instead.
    """
    ArchitectureASTVisitor(filepath, errors, checks=_CHECK_NO_TEST_DETECTION).visit(
        tree
    )


HEAVY_ML_PACKAGES = frozenset(
    {
        "torch",
        "transformers",
        "easyocr",
        "onnxruntime",
        "llama_cpp",
        "sentence_transformers",
    }
)


def _is_type_checking_expr(test: ast.AST) -> bool:
    """True if expression is TYPE_CHECKING or typing.TYPE_CHECKING (or logical AND containing it)."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_is_type_checking_expr(v) for v in test.values)
    return False


def _is_type_checking_guard(node: ast.If) -> bool:
    """True if node represents an `if TYPE_CHECKING:` conditional branch."""
    return _is_type_checking_expr(node.test)


class LazyMLImportVisitor(ast.NodeVisitor):
    """AST visitor to detect top-level or class-level eager imports of heavy ML packages."""

    def __init__(self, filepath: str, errors: list):
        self.filepath = filepath.replace("\\", "/")
        self.errors = errors

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Lazy imports placed inside function or method bodies are permitted
        pass

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        # Lazy imports placed inside async function or method bodies are permitted
        pass

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # Deferred evaluation inside lambdas is permitted
        pass

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node):
            # Guarded by TYPE_CHECKING; do not inspect body. Inspect orelse branch if present.
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            pkg = alias.name.split(".", 1)[0]
            if pkg in HEAVY_ML_PACKAGES:
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Top-level import of heavy ML package '{pkg}' found. "
                    "Heavy ML packages must be lazily imported inside function or method bodies, "
                    "or guarded under 'if TYPE_CHECKING:'."
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            pkg = node.module.split(".", 1)[0]
            if pkg in HEAVY_ML_PACKAGES:
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Top-level import of heavy ML package '{pkg}' found. "
                    "Heavy ML packages must be lazily imported inside function or method bodies, "
                    "or guarded under 'if TYPE_CHECKING:'."
                )


def validate_lazy_ml_imports(errors: list, filepath: str, tree: ast.AST) -> None:
    """Validate that heavy ML packages are not imported at top-level or class-level across app/.

    Heavy ML dependencies (torch, transformers, easyocr, onnxruntime, llama_cpp,
    sentence_transformers) must be lazily imported inside function or method
    bodies to prevent startup crashes and bloat in base installations without ML extras.
    Imports guarded by `if TYPE_CHECKING:` are allowed for type hints.
    """
    ArchitectureASTVisitor(filepath, errors, checks=_CHECK_LAZY_ML).visit(tree)


_CHECK_IMPORT_LAYERS = 1 << 0
_CHECK_SERVICE_OWNERSHIP = 1 << 1
_CHECK_SETTINGS_KEYS = 1 << 2
_CHECK_LAZY_ML = 1 << 3
_CHECK_IMPORT_TIME_GLOBALS = 1 << 4
_CHECK_NO_TEST_DETECTION = 1 << 5
_CHECK_ASYNC_AND_UI = 1 << 6
_CHECK_DUPLICATE_PATTERNS = 1 << 7
_CHECK_ANTI_DUPLICATION = 1 << 8
_CHECK_MODAL_SHELL = 1 << 9
_CHECK_HASH_INDEX_ONLY = 1 << 10
_CHECK_ALL = (
    _CHECK_IMPORT_LAYERS
    | _CHECK_SERVICE_OWNERSHIP
    | _CHECK_SETTINGS_KEYS
    | _CHECK_LAZY_ML
    | _CHECK_IMPORT_TIME_GLOBALS
    | _CHECK_NO_TEST_DETECTION
    | _CHECK_ASYNC_AND_UI
    | _CHECK_DUPLICATE_PATTERNS
    | _CHECK_ANTI_DUPLICATION
    | _CHECK_MODAL_SHELL
)


class ArchitectureASTVisitor(ast.NodeVisitor):
    """Single-pass AST visitor consolidating all architectural and anti-duplication checks per file."""

    def __init__(
        self,
        filepath: str,
        errors: list,
        *,
        declared_settings: set[str] | None = None,
        function_hashes: dict[str, list[tuple[str, int, str]]] | None = None,
        checks: int = _CHECK_ALL,
    ) -> None:
        self.raw_filepath = filepath
        self.norm_path = filepath.replace("\\", "/")
        self.errors = errors
        self.declared_settings = declared_settings
        self.function_hashes = function_hashes

        self._hash_index_only = checks == _CHECK_HASH_INDEX_ONLY
        self._do_import_layers = bool(checks & _CHECK_IMPORT_LAYERS)
        self._do_service_ownership = (
            bool(checks & _CHECK_SERVICE_OWNERSHIP)
            and self.norm_path != "app/core/services.py"
        )
        self._do_settings_keys = (
            bool(checks & _CHECK_SETTINGS_KEYS) and declared_settings is not None
        )
        self._do_lazy_ml = bool(checks & _CHECK_LAZY_ML)
        self._do_import_time_globals = bool(checks & _CHECK_IMPORT_TIME_GLOBALS)
        self._do_no_test_detection = bool(checks & _CHECK_NO_TEST_DETECTION)
        self._do_async_and_ui = bool(checks & _CHECK_ASYNC_AND_UI)
        self._do_duplicate_patterns = bool(checks & _CHECK_DUPLICATE_PATTERNS)
        self._do_anti_duplication = bool(checks & _CHECK_ANTI_DUPLICATION)
        self._do_modal_shell = (
            bool(checks & _CHECK_MODAL_SHELL) and self.norm_path != "app/ui/layout.py"
        )

        self._is_ui_file = "app/ui" in self.norm_path
        self._check_frozen = (
            self._do_duplicate_patterns and self.norm_path not in ALLOWED_FOR_FROZEN
        )
        self._check_sessions = (
            self._do_duplicate_patterns and self.norm_path not in ALLOWED_FOR_SESSIONS
        )
        self._check_keys = (
            self._do_duplicate_patterns and self.norm_path not in ALLOWED_FOR_KEYS
        )
        self._check_chars = (
            self._do_duplicate_patterns and self.norm_path not in ALLOWED_FOR_CHARS
        )

        self._module = ""
        self._is_package_init = False
        self._forbidden_layers: tuple[str, ...] = ()
        if self._do_import_layers:
            module = _module_name(filepath)
            if module not in IMPORT_LAYER_EXEMPT:
                layers = [p for p in IMPORT_LAYER_RULES if _matches(module, p)]
                if layers:
                    self._module = module
                    self._is_package_init = Path(filepath).stem == "__init__"
                    self._forbidden_layers = IMPORT_LAYER_RULES[max(layers, key=len)]

        self._db_worker_names: set[str] = {"DBWorker"}
        self._unmatched_name_calls: list[tuple[str, int]] = []
        self._async_context: list[str] = []
        self.current_class: str | None = None
        self.function_depth = 0
        self._func_or_lambda_depth = 0
        self._type_checking_depth = 0

    def _record_function_hash_if_eligible(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        if self.function_hashes is None or self.function_depth != 0:
            return
        func_name = node.name
        if func_name in EXCLUDED_METHOD_NAMES or _is_overload_decorated(node):
            return
        effective_body = _effective_function_body(node)
        if len(effective_body) > 1 or (
            effective_body and not _is_trivial_stub_statement(effective_body[0])
        ):
            h = compute_normalized_ast_hash(node)
            self.function_hashes.setdefault(h, []).append(
                (self.norm_path, node.lineno, func_name)
            )

    def visit_Module(self, node: ast.Module) -> None:
        self.generic_visit(node)
        if (
            self._do_service_ownership
            and len(self._db_worker_names) > 1
            and self._unmatched_name_calls
        ):
            for call_name, lineno in self._unmatched_name_calls:
                if call_name in self._db_worker_names:
                    self.errors.append(
                        f"{self.raw_filepath}:{lineno}: Acquire managed services instead of constructing DBWorker"
                    )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._hash_index_only:
            if self.function_depth == 0:
                for stmt in node.body:
                    self.visit(stmt)
            return

        class_name = node.name
        if self._do_anti_duplication and class_name in CANONICAL_UTILITY_REGISTRY:
            expected_home = CANONICAL_UTILITY_REGISTRY[class_name]
            if self.norm_path != expected_home:
                home_module = expected_home.replace(".py", "").replace("/", ".")
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Re-definition of canonical utility '{class_name}' found. "
                    f"Use '{home_module}.{class_name}' instead."
                )

        if self._do_modal_shell:
            base_names = {
                name
                for b in node.bases
                if (name := _extract_base_class_name(b)) is not None
            }
            if "ModalScreen" in base_names:
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Class '{class_name}' inherits directly from 'ModalScreen'. "
                    "Inherit from 'app.ui.layout.ResponsiveModalScreen' instead."
                )

            is_modal_cls = (
                class_name.endswith("Modal")
                or "ResponsiveModalScreen" in base_names
                or "ModalScreen" in base_names
            )
            if is_modal_cls:
                for stmt in node.body:
                    attr_name: str | None = None
                    val_node: ast.expr | None = None
                    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                        tgt = stmt.targets[0]
                        if isinstance(tgt, ast.Name):
                            attr_name = tgt.id
                            val_node = stmt.value
                    elif isinstance(stmt, ast.AnnAssign) and isinstance(
                        stmt.target, ast.Name
                    ):
                        attr_name = stmt.target.id
                        val_node = stmt.value

                    if attr_name == "CSS":
                        self.errors.append(
                            f"{self.norm_path}:{stmt.lineno}: Modal class '{class_name}' defines 'CSS' directly. "
                            "Declare subclass overrides in 'MODAL_CSS' on 'ResponsiveModalScreen' instead."
                        )
                    if (
                        attr_name in ("CSS", "MODAL_CSS")
                        and isinstance(val_node, ast.Constant)
                        and isinstance(val_node.value, str)
                    ):
                        if _has_inlined_modal_shell_css(val_node.value, class_name):
                            self.errors.append(
                                f"{self.norm_path}:{stmt.lineno}: Modal class '{class_name}' re-inlines shared modal shell CSS rules in '{attr_name}'. "
                                "Keep shared shell styles in 'app.ui.layout.BASE_MODAL_CSS' and put only deltas in 'MODAL_CSS'."
                            )
                        if _has_unnested_modal_breakpoint_css(val_node.value):
                            self.errors.append(
                                f"{self.norm_path}:{stmt.lineno}: Modal class '{class_name}' defines un-nested '.compact' or '.narrow' selectors in '{attr_name}'. "
                                f"Nest '&.compact' and '&.narrow' inside '{class_name} {{ ... }}' so SCOPED_CSS binds breakpoints to the dialog root."
                            )

        prev_class = self.current_class
        self.current_class = class_name
        self.generic_visit(node)
        self.current_class = prev_class

    def _visit_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool
    ) -> None:
        if self._hash_index_only:
            self._record_function_hash_if_eligible(node)
            return

        func_name = node.name
        if self._do_anti_duplication:
            if func_name in CANONICAL_UTILITY_REGISTRY:
                expected_home = CANONICAL_UTILITY_REGISTRY[func_name]
                if self.norm_path != expected_home:
                    home_module = expected_home.replace(".py", "").replace("/", ".")
                    self.errors.append(
                        f"{self.norm_path}:{node.lineno}: Re-definition of canonical utility '{func_name}' found. "
                        f"Use '{home_module}.{func_name}' instead."
                    )

            if (
                self.function_depth == 0
                and (
                    self.norm_path == "app/core/domain_contracts.py"
                    or self.norm_path.startswith("app/plugins/clinical_compliance/")
                )
                and (
                    self.current_class in REDUNDANT_DICT_MODELS
                    or (
                        self.norm_path == "app/core/domain_contracts.py"
                        and self.current_class
                        and self.current_class
                        not in (
                            "DictCompatMixin",
                            "SortingPlanModel",
                            "CorpusPreFetchBatchModel",
                        )
                    )
                )
                and func_name in CANONICAL_DICT_METHODS
            ):
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Redundant dictionary adapter method '{func_name}' found in class '{self.current_class}'. "
                    f"Use canonical 'DictCompatMixin' from 'app.core.domain_contracts' instead."
                )

            self._record_function_hash_if_eligible(node)

        self.function_depth += 1
        self._func_or_lambda_depth += 1
        if is_async and self._do_async_and_ui:
            self._async_context.append(func_name)
        try:
            self.generic_visit(node)
        finally:
            if is_async and self._do_async_and_ui:
                self._async_context.pop()
            self._func_or_lambda_depth -= 1
            self.function_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if self._hash_index_only:
            return
        self._func_or_lambda_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self._func_or_lambda_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        if self._hash_index_only:
            for stmt in node.body:
                self.visit(stmt)
            for stmt in node.orelse:
                self.visit(stmt)
            return
        if self._do_lazy_ml and _is_type_checking_guard(node):
            self.visit(node.test)
            self._type_checking_depth += 1
            try:
                for stmt in node.body:
                    self.visit(stmt)
            finally:
                self._type_checking_depth -= 1
            for stmt in node.orelse:
                self.visit(stmt)
        else:
            self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._hash_index_only:
            return
        if self._forbidden_layers:
            for alias in node.names:
                name = alias.name
                for banned in self._forbidden_layers:
                    if _matches(name, banned):
                        message = f"{self.raw_filepath}:{node.lineno}: Layer violation: '{self._module}' imports '{name}' ('{banned}' is off-limits here)"
                        if message not in self.errors:
                            self.errors.append(message)

        if (
            self._do_lazy_ml
            and self._func_or_lambda_depth == 0
            and self._type_checking_depth == 0
        ):
            for alias in node.names:
                pkg = alias.name.split(".", 1)[0]
                if pkg in HEAVY_ML_PACKAGES:
                    self.errors.append(
                        f"{self.norm_path}:{node.lineno}: Top-level import of heavy ML package '{pkg}' found. "
                        "Heavy ML packages must be lazily imported inside function or method bodies, "
                        "or guarded under 'if TYPE_CHECKING:'."
                    )

        if self._do_no_test_detection:
            found = next(
                (a.name for a in node.names if a.name.startswith("unittest.mock")),
                None,
            )
            if found:
                self.errors.append(
                    f"{self.raw_filepath}:{node.lineno}: Test detection in production code: "
                    f"'{found}'; inject the dependency so tests run the production path"
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._hash_index_only:
            return
        if self._forbidden_layers:
            imported = node.module or ""
            skip_layer = False
            if node.level:
                package = (
                    self._module
                    if self._is_package_init
                    else self._module.rpartition(".")[0]
                )
                parts = package.split(".")
                if node.level > len(parts):
                    skip_layer = True
                else:
                    imported = ".".join(
                        parts[: len(parts) - node.level + 1]
                        + ([imported] if imported else [])
                    )
            if not skip_layer:
                names = [
                    imported if alias.name == "*" else f"{imported}.{alias.name}"
                    for alias in node.names
                ]
                for name in names:
                    for banned in self._forbidden_layers:
                        if _matches(name, banned):
                            message = f"{self.raw_filepath}:{node.lineno}: Layer violation: '{self._module}' imports '{name}' ('{banned}' is off-limits here)"
                            if message not in self.errors:
                                self.errors.append(message)

        if self._do_service_ownership and node.module == "app.core.db_worker":
            self._db_worker_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "DBWorker"
            )

        if (
            self._do_lazy_ml
            and self._func_or_lambda_depth == 0
            and self._type_checking_depth == 0
            and node.level == 0
            and node.module
        ):
            pkg = node.module.split(".", 1)[0]
            if pkg in HEAVY_ML_PACKAGES:
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Top-level import of heavy ML package '{pkg}' found. "
                    "Heavy ML packages must be lazily imported inside function or method bodies, "
                    "or guarded under 'if TYPE_CHECKING:'."
                )

        if self._do_no_test_detection:
            found = None
            if node.module and node.module.startswith("unittest.mock"):
                found = node.module
            elif node.module == "unittest" and any(
                a.name == "mock" for a in node.names
            ):
                found = "unittest.mock"
            if found:
                self.errors.append(
                    f"{self.raw_filepath}:{node.lineno}: Test detection in production code: "
                    f"'{found}'; inject the dependency so tests run the production path"
                )

    def _check_global_targets(self, node: ast.AST, targets: Iterable[ast.AST]) -> None:
        for target in targets:
            part = _find_global_target(target)
            if part is not None:
                lineno = getattr(node, "lineno", 0)
                self.errors.append(
                    f"{self.raw_filepath}:{lineno}: Import-time global patch: "
                    f"'{ast.unparse(part)}' is assigned at module level; "
                    "move it into an explicit install() called from an entrypoint"
                )

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._hash_index_only:
            return
        if self._do_import_time_globals and self._func_or_lambda_depth == 0:
            self._check_global_targets(node, node.targets)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self._hash_index_only:
            return
        if self._do_import_time_globals and self._func_or_lambda_depth == 0:
            self._check_global_targets(node, (node.target,))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._hash_index_only:
            return
        if self._do_import_time_globals and self._func_or_lambda_depth == 0:
            self._check_global_targets(node, (node.target,))
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        if self._hash_index_only:
            return
        if self._do_import_time_globals and self._func_or_lambda_depth == 0:
            self._check_global_targets(node, node.targets)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if self._hash_index_only:
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._hash_index_only:
            return
        func = node.func

        if self._do_service_ownership:
            if isinstance(func, ast.Attribute) and func.attr == "DBWorker":
                self.errors.append(
                    f"{self.raw_filepath}:{node.lineno}: Acquire managed services instead of constructing DBWorker"
                )
            elif isinstance(func, ast.Name):
                if func.id in self._db_worker_names:
                    self.errors.append(
                        f"{self.raw_filepath}:{node.lineno}: Acquire managed services instead of constructing DBWorker"
                    )
                else:
                    self._unmatched_name_calls.append((func.id, node.lineno))

        if (
            self._do_settings_keys
            and self.declared_settings is not None
            and isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and _is_settings_receiver(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            key = node.args[1].value
            if key.isupper() and key not in self.declared_settings:
                self.errors.append(
                    f"{self.raw_filepath}:{node.lineno}: Undeclared setting '{key}' is read from settings; declare it on Settings in {SETTINGS_SOURCE} or drop the read"
                )

        if (
            self._check_frozen
            and isinstance(func, ast.Name)
            and func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
        ):
            arg0, arg1 = node.args[0], node.args[1]
            if (
                isinstance(arg0, ast.Name)
                and arg0.id == "sys"
                and isinstance(arg1, ast.Constant)
                and arg1.value in ("frozen", "_MEIPASS")
            ):
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Direct {func.id}(sys, '{arg1.value}') usage found. "
                    "Use 'app.core.path_utils.is_packaged()' or 'get_meipass_dir()' instead."
                )

        if self._async_context:
            async_fn = self._async_context[-1]
            if isinstance(func, ast.Attribute):
                method_name = func.attr
                if method_name == "commit":
                    self.errors.append(
                        f"{self.raw_filepath}:{node.lineno}: Direct database commit() inside async function '{async_fn}'"
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
                            self.errors.append(
                                f"{self.raw_filepath}:{node.lineno}: Direct database write ({method_name} with {query.split()[0]}) inside async function '{async_fn}'"
                            )

            if self._is_ui_file:
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "sleep"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "time"
                ):
                    self.errors.append(
                        f"{self.raw_filepath}:{node.lineno}: Synchronous time.sleep() inside async function '{async_fn}'"
                    )

                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("get", "post", "put", "delete", "patch")
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "requests"
                ):
                    self.errors.append(
                        f"{self.raw_filepath}:{node.lineno}: Synchronous requests.{func.attr}() inside async function '{async_fn}'"
                    )

                if isinstance(func, ast.Name) and func.id == "open":
                    self.errors.append(
                        f"{self.raw_filepath}:{node.lineno}: Synchronous open() inside async function '{async_fn}'"
                    )

                if isinstance(func, ast.Attribute) and func.attr.startswith(
                    ("read_", "write_")
                ):
                    self.errors.append(
                        f"{self.raw_filepath}:{node.lineno}: Synchronous Path.{func.attr}() inside async function '{async_fn}'"
                    )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._hash_index_only:
            return
        if self._do_no_test_detection and node.attr in _TEST_DETECTION_NAMES:
            self.errors.append(
                f"{self.raw_filepath}:{node.lineno}: Test detection in production code: "
                f"'{node.attr}'; inject the dependency so tests run the production path"
            )
        if (
            self._check_frozen
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
            and node.attr in ("frozen", "_MEIPASS")
        ):
            self.errors.append(
                f"{self.norm_path}:{node.lineno}: Direct 'sys.{node.attr}' usage found. "
                "Use 'app.core.path_utils.is_packaged()' or 'get_meipass_dir()' instead."
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self._hash_index_only:
            return
        if self._do_no_test_detection and node.id in _TEST_DETECTION_NAMES:
            self.errors.append(
                f"{self.raw_filepath}:{node.lineno}: Test detection in production code: "
                f"'{node.id}'; inject the dependency so tests run the production path"
            )

    def visit_Constant(self, node: ast.Constant) -> None:
        if self._hash_index_only:
            return
        val = node.value
        if self._do_no_test_detection and val in _TEST_DETECTION_NAMES:
            self.errors.append(
                f"{self.raw_filepath}:{node.lineno}: Test detection in production code: "
                f"'{val}'; inject the dependency so tests run the production path"
            )
        if isinstance(val, str):
            if self._check_sessions and "autosorter_sessions" in val:
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Direct reference to 'autosorter_sessions' folder found. "
                    "Use 'app.core.path_utils.get_session_base_dir()' or 'setup_session_directory()' instead."
                )
            if self._check_keys and "secret.key" in val:
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Direct reference to 'secret.key' database key file found. "
                    "Use 'app.core.path_utils.resolve_db_crypto(db_path)' instead."
                )
            if self._check_chars and (val == '<>:"|?*' or val == '[<>:"/\\|?*]'):
                self.errors.append(
                    f"{self.norm_path}:{node.lineno}: Hardcoded illegal character set or regex pattern '{val}' found. "
                    "Use shared validators/sanitizers in 'app.core.path_utils' instead."
                )


FULL_SCAN_TRIGGER_PATHS = frozenset(
    {
        "scripts/validate_architecture.py",
        SETTINGS_SOURCE,
        "pyproject.toml",
        "uv.lock",
    }
)
DIAGRAM_SCHEMA_PATH = "scripts/diagram_schema.py"
DIAGRAM_ASSETS_PREFIX = "docs/assets/diagrams/"


def _normalize_cli_path(raw_path: str) -> str:
    """Normalize a CLI path argument relative to the current working directory."""
    abs_cwd = os.path.abspath(".")
    abs_path = os.path.abspath(raw_path)
    try:
        rel = os.path.relpath(abs_path, abs_cwd).replace("\\", "/")
    except ValueError:
        rel = raw_path.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def classify_cli_paths(
    files: Sequence[str],
) -> tuple[bool, set[str] | None, bool, set[str] | None, bool]:
    """Classify CLI file paths into AST and diagram validation execution plans.

    Returns ``(run_ast_checks, target_app_files, run_diagram_checks, target_diagram_specs, check_core_modules)``.
    """
    has_diagram_workspace = (
        Path("docs/assets/diagrams").exists() or Path(DIAGRAM_SCHEMA_PATH).exists()
    )
    if not files:
        return True, None, has_diagram_workspace, None, True

    escalate_full_scan = False
    staged_app_files: set[str] = set()
    run_diagram_checks = False
    full_diagram_scan = False
    check_core_modules = False
    targeted_diagram_specs: set[str] = set()

    for raw in files:
        rel = _normalize_cli_path(raw)
        if rel in FULL_SCAN_TRIGGER_PATHS:
            escalate_full_scan = True
        elif rel == DIAGRAM_SCHEMA_PATH:
            run_diagram_checks = True
            full_diagram_scan = True
            check_core_modules = True
        elif rel.startswith(DIAGRAM_ASSETS_PREFIX):
            run_diagram_checks = True
            rem = rel[len(DIAGRAM_ASSETS_PREFIX) :]
            if rem and "/" not in rem and Path(rem).suffix in {".mmd", ".svg"}:
                spec_id = Path(rem).stem
                targeted_diagram_specs.add(spec_id)
                if spec_id == "core_architecture":
                    check_core_modules = True
            else:
                full_diagram_scan = True
                check_core_modules = True
        elif rel.startswith("app/") and rel.endswith(".py"):
            if rel.startswith("app/core/") and has_diagram_workspace:
                run_diagram_checks = True
                check_core_modules = True
                targeted_diagram_specs.add("core_architecture")
            if os.path.exists(rel):
                staged_app_files.add(rel)

    if escalate_full_scan:
        return True, None, has_diagram_workspace, None, True

    run_ast_checks = bool(staged_app_files)
    target_app_files: set[str] | None = staged_app_files if run_ast_checks else set()
    target_diagram_specs: set[str] | None = (
        None if full_diagram_scan else targeted_diagram_specs
    )
    return (
        run_ast_checks,
        target_app_files,
        run_diagram_checks,
        target_diagram_specs,
        check_core_modules,
    )


def _resolve_declared_settings(
    parsed_files: list[tuple[str, ast.Module]],
) -> set[str]:
    """Extract declared Settings keys from parsed app/config.py if present, else fallback."""
    for filepath, tree in parsed_files:
        if filepath == SETTINGS_SOURCE:
            keys = _declared_settings_keys_from_tree(tree)
            if keys is not None:
                return keys
            break
    if os.path.exists(SETTINGS_SOURCE):
        return declared_settings_keys(SETTINGS_SOURCE)
    return declared_settings_keys()


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

    (
        run_ast_checks,
        target_files,
        run_diagram_checks,
        target_diagram_specs,
        check_core_modules,
    ) = classify_cli_paths(args.files)

    if not run_ast_checks and not run_diagram_checks:
        print("No relevant staged files to validate for architecture.")
        sys.exit(0)

    errors: list[str] = []

    if run_ast_checks:
        parsed_files = _collect_parsed_app_files("app")
        declared_settings = _resolve_declared_settings(parsed_files)
        function_hashes: dict[str, list[tuple[str, int, str]]] = {}

        for filepath, tree in parsed_files:
            if target_files is None or filepath in target_files:
                ArchitectureASTVisitor(
                    filepath,
                    errors,
                    declared_settings=declared_settings,
                    function_hashes=function_hashes,
                ).visit(tree)
            else:
                ArchitectureASTVisitor(
                    filepath,
                    [],
                    function_hashes=function_hashes,
                    checks=_CHECK_HASH_INDEX_ONLY,
                ).visit(tree)

        _report_duplicate_function_hashes(
            errors, function_hashes, target_files=target_files
        )
        if run_diagram_checks and check_core_modules:
            from scripts.diagram_schema import hydrate_core_architecture_spec

            hydrate_core_architecture_spec(parsed_files)

    if run_diagram_checks:
        explicit_diagram_files = {
            _normalize_cli_path(raw)
            for raw in args.files
            if _normalize_cli_path(raw).startswith(DIAGRAM_ASSETS_PREFIX)
        }
        validate_diagram_schema_and_assets(
            errors,
            target_spec_ids=target_diagram_specs,
            check_core_modules=check_core_modules,
            explicit_diagram_files=explicit_diagram_files,
        )

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
