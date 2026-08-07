import ast
import glob
import os

import pytest

# Paths to scan
UI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app", "ui"))


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


def test_no_rigid_sizes_in_dialog_cards():
    """Headless unit test to assert that dialog card classes do not use rigid height/width classes."""
    ui_files = glob.glob(os.path.join(UI_DIR, "*.py"))
    assert ui_files, f"No UI files found in {UI_DIR}"

    failures = []

    for file_path in ui_files:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError as e:
            failures.append(f"Syntax error in {file_path}: {e}")
            continue

        # Map to find variable definitions for constants
        assigned_constants = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                # Check for constant assignments like STANDARD_DIALOG_CARD_MD = "..."
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                            assigned_constants[target.id] = node.value.value
                        elif isinstance(node.value, ast.Str):  # compatibility for older Python
                            assigned_constants[target.id] = node.value.s

        class DialogCardVisitor(ast.NodeVisitor):
            def visit_Call(self, node):
                # Look for calls to .classes(...)
                if isinstance(node.func, ast.Attribute) and node.func.attr == "classes":
                    # Check if the method call is chained to a card element (e.g., ui.card() or card())
                    subject = node.func.value
                    is_card_call = False
                    if isinstance(subject, ast.Call):
                        if isinstance(subject.func, ast.Attribute) and subject.func.attr == "card":
                            is_card_call = True
                        elif isinstance(subject.func, ast.Name) and subject.func.id == "card":
                            is_card_call = True

                    # Also detect ui.card() used within dialog context via AST parent/child or general check
                    # To be exhaustive and safe, we validate ALL card classes in the UI files
                    if is_card_call:
                        # Extract the classes passed to the call
                        for arg in node.args:
                            cls_str = ""
                            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                                cls_str = arg.value
                            elif isinstance(arg, ast.Str):
                                cls_str = arg.s
                            elif isinstance(arg, ast.Name) and arg.id in assigned_constants:
                                cls_str = assigned_constants[arg.id]

                            if cls_str:
                                # Split by space and find any offending class names
                                class_list = cls_str.split()
                                for cls in class_list:
                                    if is_rigid_layout_class(cls):
                                        failures.append(
                                            f"Rigid layout class '{cls}' used in card at "
                                            f"{os.path.basename(file_path)}:line {node.lineno}"
                                        )

                self.generic_visit(node)

        visitor = DialogCardVisitor()
        visitor.visit(tree)

        # Also validate any defined string constants with 'CARD' or 'DIALOG' in their names
        for const_name, const_val in assigned_constants.items():
            if "CARD" in const_name or "DIALOG" in const_name:
                for cls in const_val.split():
                    if is_rigid_layout_class(cls):
                        failures.append(
                            f"Rigid layout class '{cls}' used in constant '{const_name}' "
                            f"in {os.path.basename(file_path)}"
                        )

    # If any rigid sizes are found, fail the test suite
    if failures:
        pytest.fail("\n".join(failures))


def test_is_rigid_layout_class_validation():
    """Verify that is_rigid_layout_class correctly flags rigid sizes and allows fluid/boundary sizes."""
    # Rigid / disallowed classes
    assert is_rigid_layout_class("w-96") is True
    assert is_rigid_layout_class("w-48") is True
    assert is_rigid_layout_class("w-[500px]") is True
    assert is_rigid_layout_class("h-96") is True
    assert is_rigid_layout_class("h-[250px]") is True

    # Fluid / allowed classes
    assert is_rigid_layout_class("w-full") is False
    assert is_rigid_layout_class("w-auto") is False
    assert is_rigid_layout_class("w-1/2") is False
    assert is_rigid_layout_class("w-3/4") is False
    assert is_rigid_layout_class("h-full") is False
    assert is_rigid_layout_class("h-auto") is False
    assert is_rigid_layout_class("max-w-md") is False
    assert is_rigid_layout_class("min-w-[320px]") is False
    assert is_rigid_layout_class("max-h-screen") is False
    assert is_rigid_layout_class("min-h-[100px]") is False
    assert is_rigid_layout_class("p-6") is False
    assert is_rigid_layout_class("gap-4") is False

