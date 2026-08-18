#!/usr/bin/env python3
"""Linter to prevent developers from introducing rigid layout sizing classes in UI components."""

import ast
import os
import sys


def is_rigid_layout_class(cls_name: str) -> bool:
    """Determine if a CSS/Tailwind class is a rigid absolute/hardcoded height or width.

    Allows fluid/percentage dimensions (e.g., w-full, w-1/2, w-3/4) and
    boundary limits (e.g., min-w-..., max-w-..., min-h-..., max-h-...).
    """
    # Exclude min/max limit and flex boundaries
    if (
        cls_name.startswith("min-w-")
        or cls_name.startswith("max-w-")
        or cls_name.startswith("min-h-")
        or cls_name.startswith("max-h-")
    ):
        return False

    # Check for hardcoded arbitrary value brackets (e.g. w-[500px], h-[200px])
    if cls_name.startswith("w-[") or cls_name.startswith("h-["):
        return True

    # Check for width classes (e.g. w-96, w-48) but allow fluid fraction/percentage and keywords
    if cls_name.startswith("w-"):
        # Allow w-full, w-auto, w-screen
        if cls_name in ("w-full", "w-auto", "w-screen"):
            return False
        # Allow fluid fraction sizes like w-1/2, w-3/4, w-11/12
        if "/" in cls_name:
            return False
        return True

    # Check for height classes (e.g. h-96, h-48) but allow fluid keywords/fractions
    if cls_name.startswith("h-"):
        if cls_name in ("h-full", "h-auto", "h-screen"):
            return False
        if "/" in cls_name:
            return False
        return True

    return False


class RigidLayoutVisitor(ast.NodeVisitor):
    """AST Visitor to scan string literals and .classes(...) arguments for rigid sizing classes."""

    def __init__(self, filepath: str):
        self.filepath = filepath.replace("\\", "/")
        self.errors = []
        self.reported = set()

    def _check_string(self, text: str, lineno: int):
        for cls in text.split():
            if is_rigid_layout_class(cls):
                key = (lineno, cls)
                if key not in self.reported:
                    self.reported.add(key)
                    self.errors.append(
                        f"{self.filepath}:{lineno}: Prohibited rigid layout class '{cls}' found in string '{text}'."
                    )

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "classes":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self._check_string(arg.value, node.lineno)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant):
        if "app/ui" in self.filepath and isinstance(node.value, str):
            self._check_string(node.value, node.lineno)
        self.generic_visit(node)


def validate_file(filepath: str) -> list[str]:
    """Validate a single python file for rigid layout violations."""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return []

    visitor = RigidLayoutVisitor(filepath)
    visitor.visit(tree)
    return visitor.errors


def main():
    """Run static layout linter across app/ UI components."""
    errors = []

    # Check python files in app/
    for root, _, files in os.walk("app"):
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file)
            errors.extend(validate_file(filepath))

    if errors:
        print("Prohibited Static Layout Utility Classes Found:")
        for err in errors:
            print(f"  - {err}")
        print(
            "\nPlease replace hardcoded static sizing classes (e.g., w-96, h-48, w-[500px]) "
            "with responsive limits (min-w-*, max-w-*), fluid fractions (w-1/2), or fluid keywords (w-full, w-auto)."
        )
        sys.exit(1)
    else:
        print("Static layout validation passed successfully. No rigid layout classes detected.")
        sys.exit(0)


if __name__ == "__main__":
    main()
