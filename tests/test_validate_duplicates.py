import ast

from scripts.validate_duplicates import DuplicatePatternVisitor


def test_visitor_catches_frozen():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("is_packaged = getattr(sys, 'frozen', False)")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Direct getattr(sys, 'frozen') usage found" in visitor.errors[0]


def test_visitor_catches_sys_frozen_attr():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("is_packaged = sys.frozen")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Direct 'sys.frozen' usage found" in visitor.errors[0]


def test_visitor_catches_autosorter_sessions():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("path = '/tmp/autosorter_sessions/abc'")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Direct reference to 'autosorter_sessions' folder found" in visitor.errors[0]


def test_visitor_catches_secret_key():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("key = parent / 'secret.key'")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert (
        "Direct reference to 'secret.key' database key file found" in visitor.errors[0]
    )


def test_visitor_catches_hardcoded_illegal_chars():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("chars = '<>:\"|?*' ")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Hardcoded illegal character set or regex pattern" in visitor.errors[0]


def test_visitor_allows_safe_files():
    # DuplicatePatternVisitor should ignore patterns in path_utils.py
    visitor = DuplicatePatternVisitor("app/core/path_utils.py")
    tree = ast.parse("""
is_packaged = getattr(sys, 'frozen', False)
path = '/tmp/autosorter_sessions/abc'
key = parent / 'secret.key'
chars = '<>:\"|?*'
""")
    visitor.visit(tree)
    assert len(visitor.errors) == 0
