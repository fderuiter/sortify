import ast

from scripts.validate_architecture import (
    CANONICAL_UTILITY_REGISTRY,
    DuplicatePatternVisitor,
    UtilityAntiDuplicationVisitor,
    compute_normalized_ast_hash,
)


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
    visitor = DuplicatePatternVisitor("app/core/path_utils.py")
    tree = ast.parse("""
is_packaged = getattr(sys, 'frozen', False)
path = '/tmp/autosorter_sessions/abc'
key = parent / 'secret.key'
chars = '<>:\"|?*'
""")
    visitor.visit(tree)
    assert len(visitor.errors) == 0


def test_compute_normalized_ast_hash_strips_docstrings_and_annotations():
    code1 = """
def my_func(a: int, b: str) -> bool:
    '''This is a docstring.'''
    val = a + len(b)
    return val > 10
"""
    code2 = """
def my_func(x, y):
    res = x + len(y)
    return res > 10
"""
    node1 = ast.parse(code1).body[0]
    node2 = ast.parse(code2).body[0]

    hash1 = compute_normalized_ast_hash(node1)
    hash2 = compute_normalized_ast_hash(node2)
    assert hash1 == hash2


def test_utility_anti_duplication_visitor_canonical_registry():
    errors = []
    hashes = {}

    # Test re-definition outside home module raises error
    for func_name, home_path in CANONICAL_UTILITY_REGISTRY.items():
        bad_visitor = UtilityAntiDuplicationVisitor(
            "app/ui/random_ui.py", errors, hashes
        )
        tree = ast.parse(f"def {func_name}(x):\n    return x + 1\n")
        bad_visitor.visit(tree)

    assert len(errors) == len(CANONICAL_UTILITY_REGISTRY)
    for func_name, home_path in CANONICAL_UTILITY_REGISTRY.items():
        home_module = home_path.replace(".py", "").replace("/", ".")
        assert any(
            f"Re-definition of canonical utility '{func_name}' found. Use '{home_module}.{func_name}' instead."
            in err
            for err in errors
        )

    # Test definition inside home module is allowed
    good_errors = []
    good_hashes = {}
    good_visitor = UtilityAntiDuplicationVisitor(
        "app/core/resilient_file_ops.py", good_errors, good_hashes
    )
    good_tree = ast.parse("def _set_posix_mode(path, mode):\n    pass\n")
    good_visitor.visit(good_tree)
    assert len(good_errors) == 0


def test_excluded_methods_not_registered_for_duplication_hash():
    errors = []
    hashes = {}
    visitor = UtilityAntiDuplicationVisitor("app/core/some_model.py", errors, hashes)
    tree = ast.parse("""
class MyModel:
    def __init__(self, x):
        self.x = x
    def dict(self):
        return {"x": self.x}
""")
    visitor.visit(tree)
    assert len(hashes) == 0
