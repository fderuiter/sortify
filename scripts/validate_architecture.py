"""Validates architectural constraints across the project codebase."""

import ast
import os
import sys


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
                    self.aliases = {}

                def visit_Import(self, node):
                    for alias in node.names:
                        local_name = alias.asname if alias.asname else alias.name
                        self.aliases[local_name] = (alias.name, None)
                    self.generic_visit(node)

                def visit_ImportFrom(self, node):
                    module = node.module if node.module else ""
                    for alias in node.names:
                        local_name = alias.asname if alias.asname else alias.name
                        self.aliases[local_name] = (module, alias.name)
                    self.generic_visit(node)

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
                        mod = None
                        name = None
                        if isinstance(node.func, ast.Attribute) and isinstance(
                            node.func.value, ast.Name
                        ):
                            obj = node.func.value.id
                            attr = node.func.attr
                            if obj in self.aliases:
                                alias_mod, alias_name = self.aliases[obj]
                                mod = alias_mod
                                name = (
                                    attr
                                    if alias_name is None
                                    else f"{alias_name}.{attr}"
                                )
                            else:
                                mod = obj
                                name = attr
                        elif isinstance(node.func, ast.Name):
                            func_name = node.func.id
                            if func_name in self.aliases:
                                alias_mod, alias_name = self.aliases[func_name]
                                mod = alias_mod
                                name = alias_name
                            else:
                                mod = None
                                name = func_name

                        # Check for time.sleep
                        if mod == "time" and name == "sleep":
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous time.sleep() inside async function '{self.async_context[-1]}'"
                            )

                        # Check for requests.get, requests.post, etc.
                        if mod == "requests" and name in (
                            "get",
                            "post",
                            "put",
                            "delete",
                            "patch",
                        ):
                            errors.append(
                                f"{filepath}:{node.lineno}: Synchronous requests.{name}() inside async function '{self.async_context[-1]}'"
                            )

                        # Check for open()
                        if name == "open" and (mod is None or mod == "builtins"):
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

    if errors:
        print("Architectural Violations Found:")
        for error in errors:
            print(error)
        sys.exit(1)
    else:
        print("No architectural violations found.")
        sys.exit(0)


if __name__ == "__main__":
    main()
