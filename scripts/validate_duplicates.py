#!/usr/bin/env python3
"""Linter to prevent developers from introducing duplicate/redundant system utilities or path validation logic."""

import ast
import os
import sys

# Allowed files for specific patterns
ALLOWED_FOR_FROZEN = {"app/core/path_utils.py"}
ALLOWED_FOR_SESSIONS = {"app/core/path_utils.py"}
ALLOWED_FOR_KEYS = {"app/core/path_utils.py"}
ALLOWED_FOR_CHARS = {"app/core/path_utils.py"}


class DuplicatePatternVisitor(ast.NodeVisitor):
    """AST visitor to find duplicate pattern usages in files."""

    def __init__(self, filepath):
        self.filepath = filepath.replace("\\", "/")
        self.errors = []

    def visit_Attribute(self, node):
        """Check for forbidden attribute usage (e.g. sys.frozen)."""
        if self.filepath not in ALLOWED_FOR_FROZEN:
            if isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "frozen":
                self.errors.append(
                    f"{self.filepath}:{node.lineno}: Direct 'sys.frozen' usage found. "
                    "Use 'app.core.path_utils.is_packaged()' instead."
                )
        self.generic_visit(node)

    def visit_Call(self, node):
        """Check for forbidden getattr calls (e.g. getattr(sys, 'frozen'))."""
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
        """Check for hardcoded strings that shouldn't be duplicated."""
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


def main():
    """Execute main duplicate validation checks."""
    errors = []
    
    # Check all python files in the app directory
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

            visitor = DuplicatePatternVisitor(filepath)
            visitor.visit(tree)
            errors.extend(visitor.errors)

    if errors:
        print("Duplicate/Redundant System Utilities Found:")
        for error in errors:
            print(f"  - {error}")
        print("\nPlease clean up these redundancies by utilizing the centralized helpers in 'app/core/path_utils.py'.")
        sys.exit(1)
    else:
        print("No duplicate system or file path utility patterns found in 'app/'. Validation passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
