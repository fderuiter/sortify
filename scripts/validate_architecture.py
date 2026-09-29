"""Validates architectural constraints across the project codebase."""

import ast
import glob
import os
import sys
from pathlib import Path

# Add project root to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def validate_diagram_schema_and_assets(errors: list) -> None:
    """Validate that Pydantic diagram specifications represent all active core modules and match generated assets."""
    try:
        from app.ui.diagram_schema import CORE_ARCHITECTURE_SPEC, SYSTEM_DIAGRAM_SPECS

        # 1. Verify all active app/core/*.py submodules are represented in core_architecture diagram spec
        core_files = glob.glob("app/core/*.py")
        core_modules = {
            f"app.core.{Path(p).stem}"
            for p in core_files
            if not p.endswith("__init__.py")
        }

        arch_spec = SYSTEM_DIAGRAM_SPECS.get("core_architecture", CORE_ARCHITECTURE_SPEC)
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
    """Execute the architectural validation checks."""
    errors = []

    # Define directories to check
    # Check all python files in the project for rule A (DB writes in async def)
    # Check app/ui/ for rule B (blocking ops in async def)

    for root, _, files in os.walk("app"):
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file)
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            try:
                tree = ast.parse(content, filename=filepath)
            except SyntaxError:
                continue

            is_ui_file = "app/ui" in filepath.replace("\\", "/")

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

                    # Rule A: Direct DB writes in async def (execute, executemany, commit)
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

                    # Rule B: Synchronous blocking operations in UI-bound logic
                    if is_ui_file:
                        # Check for time.sleep
                        if (
                            isinstance(node.func, ast.Attribute)
                            and node.func.attr == "sleep"
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "time"
                        ):
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous time.sleep() inside async function '{self.async_context[-1]}'"
                            )

                        # Check for requests.get, requests.post, etc.
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

                        # Check for open()
                        if isinstance(node.func, ast.Name) and node.func.id == "open":
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous open() inside async function '{self.async_context[-1]}'"
                            )

                        # Check for Path.read_text, Path.write_text, etc.
                        if isinstance(
                            node.func, ast.Attribute
                        ) and node.func.attr.startswith(("read_", "write_")):
                            # This is a heuristic for Path methods
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous Path.{node.func.attr}() inside async function '{self.async_context[-1]}'"
                            )

                    self.generic_visit(node)

            Visitor().visit(tree)

    # Rule C: Validate Pydantic diagram specs and asset sync
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
