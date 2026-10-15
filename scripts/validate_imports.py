#!/usr/bin/env python3
"""AST-based Direct Dependency Declaration and Import Gate.

Parses all Python source files across the project codebase (specifically app/),
extracts top-level third-party imports via AST, resolves them to PyPI package names,
and verifies that 100% of direct third-party dependencies are explicitly declared
in pyproject.toml dependencies, optional extras, or dev groups.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Recognized top-level module name to PyPI distribution package name mappings
MODULE_TO_PACKAGE_MAP = {
    "PIL": "pillow",
    "pptx": "python-pptx",
    "docx": "python-docx",
    "sklearn": "scikit-learn",
    "sqlcipher3": "sqlcipher3-wheels",
    "llama_cpp": "llama-cpp-python",
    "pydantic_settings": "pydantic-settings",
    "pylnk3": "pylnk3",
    "pypdf": "pypdf",
    "openpyxl": "openpyxl",
    "cryptography": "cryptography",
    "keyring": "keyring",
    "jsonschema": "jsonschema",
    "textual": "textual",
    "watchdog": "watchdog",
    "pandas": "pandas",
    "scipy": "scipy",
    "numpy": "numpy",
    "pydantic": "pydantic",
    "nicegui": "nicegui",
    "xlrd": "xlrd",
    "torch": "torch",
    "easyocr": "easyocr",
    "transformers": "transformers",
    "onnxruntime": "onnxruntime",
}

STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", set())) | set(
    sys.builtin_module_names
)
INTERNAL_MODULES = {"app"}


def normalize_package_name(name: str) -> str:
    """Normalize package name according to PEP 503 standards (lowercase, dashes)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirement_package(req_spec: str) -> str:
    """Extract raw package name from a PEP 508 dependency specifier string."""
    clean_spec = req_spec.split(";")[0].strip()
    clean_spec = re.sub(r"\[.*?\]", "", clean_spec)
    match = re.match(r"^([a-zA-Z0-9_\-\.]+)", clean_spec)
    if match:
        return normalize_package_name(match.group(1))
    return normalize_package_name(clean_spec)


def load_pyproject_dependencies(
    pyproject_path: Path,
) -> tuple[set[str], set[str], set[str]]:
    """Parse pyproject.toml and return normalized sets of core, optional, and dev dependency package names."""
    if not pyproject_path.exists():
        raise FileNotFoundError(f"pyproject.toml not found at {pyproject_path}")

    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    project_data = data.get("project", {})

    # Extract core dependencies
    raw_core_deps = project_data.get("dependencies", [])
    core_deps = {parse_requirement_package(dep) for dep in raw_core_deps}

    # Extract optional dependencies (e.g., ml extra)
    optional_deps = set()
    raw_opt_deps = project_data.get("optional-dependencies", {})
    for extra_group, deps_list in raw_opt_deps.items():
        for dep in deps_list:
            optional_deps.add(parse_requirement_package(dep))

    # Extract dependency-groups (e.g., dev group)
    dev_deps = set()
    dep_groups = data.get("dependency-groups", {})
    for group_name, deps_list in dep_groups.items():
        for dep in deps_list:
            dev_deps.add(parse_requirement_package(dep))

    return core_deps, optional_deps, dev_deps


def extract_file_imports(file_path: Path) -> list[tuple[str, int]]:
    """AST-parse a Python file and extract imported root module names and line numbers."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(file_path))
    except Exception as e:
        print(f"Warning: Failed to parse AST for {file_path}: {e}", file=sys.stderr)
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                imports.append((root_mod, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                root_mod = node.module.split(".")[0]
                imports.append((root_mod, node.lineno))

    return imports


def validate_imports(target_dir: Path, pyproject_path: Path) -> tuple[bool, list[dict]]:
    """Scan Python files under target_dir and verify third-party imports against pyproject.toml."""
    core_deps, optional_deps, dev_deps = load_pyproject_dependencies(pyproject_path)
    all_declared_deps = core_deps | optional_deps | dev_deps

    violations = []

    python_files = sorted(target_dir.rglob("*.py"))
    for py_file in python_files:
        rel_path = py_file.relative_to(pyproject_path.parent)
        file_imports = extract_file_imports(py_file)

        for root_mod, lineno in file_imports:
            if root_mod in STDLIB_MODULES or root_mod in INTERNAL_MODULES:
                continue

            pkg_name = MODULE_TO_PACKAGE_MAP.get(root_mod, root_mod)
            norm_pkg = normalize_package_name(pkg_name)

            if norm_pkg not in all_declared_deps:
                violations.append(
                    {
                        "file": str(rel_path),
                        "line": lineno,
                        "module": root_mod,
                        "package": pkg_name,
                        "norm_package": norm_pkg,
                    }
                )

    success = len(violations) == 0
    return success, violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate that 100% of top-level third-party imports are declared in pyproject.toml"
    )
    parser.add_argument(
        "--root-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Root directory of the repository",
    )
    args = parser.parse_args()

    repo_root = args.root_dir
    pyproject_path = repo_root / "pyproject.toml"
    target_dir = repo_root / "app"

    if not target_dir.exists():
        print(f"Error: Target app directory not found at {target_dir}", file=sys.stderr)
        return 1

    try:
        success, violations = validate_imports(target_dir, pyproject_path)
    except Exception as e:
        print(f"Error executing import gate validation: {e}", file=sys.stderr)
        return 1

    if not success:
        print(
            "=== IMPORT GATE FAILURE: Undeclared direct third-party dependencies found ==="
        )
        seen = set()
        for v in violations:
            key = (v["file"], v["line"], v["module"])
            if key in seen:
                continue
            seen.add(key)
            print(
                f"  - {v['file']}:{v['line']} imports '{v['module']}' -> package '{v['package']}' is NOT declared in pyproject.toml"
            )
        print(
            "\nPlease add missing dependencies to project.dependencies, project.optional-dependencies.ml, or dependency-groups in pyproject.toml."
        )
        return 1

    print(
        "[IMPORT GATE PASS] 100% of top-level third-party imports are explicitly declared in pyproject.toml."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
