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


def test_visitor_catches_meipass():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("p = sys._MEIPASS")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Direct 'sys._MEIPASS' usage found" in visitor.errors[0]


def test_visitor_catches_hasattr_meipass():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("is_pkg = hasattr(sys, '_MEIPASS')")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Direct hasattr(sys, '_MEIPASS') usage found" in visitor.errors[0]


def test_visitor_catches_getattr_meipass():
    visitor = DuplicatePatternVisitor("app/some_module.py")
    tree = ast.parse("p = getattr(sys, '_MEIPASS', None)")
    visitor.visit(tree)
    assert len(visitor.errors) == 1
    assert "Direct getattr(sys, '_MEIPASS') usage found" in visitor.errors[0]


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


def test_compute_normalized_ast_hash_normalizes_annotated_assignments():
    code1 = """
def my_func(a: int, b: str) -> bool:
    marker: str
    val: int = a + len(b)
    return val > 10
"""
    code2 = """
def my_func(x, y):
    res = x + len(y)
    return res > 10
"""
    node1 = ast.parse(code1).body[0]
    node2 = ast.parse(code2).body[0]

    assert compute_normalized_ast_hash(node1) == compute_normalized_ast_hash(node2)


def test_compute_normalized_ast_hash_anonymizes_renamed_functions():
    code1 = """
def _normalize_trusted_model_hosts(hosts: list[str] | None = None) -> set[str]:
    '''Normalize downloader hosts.'''
    cleaned = {h.strip().lower() for h in (hosts or []) if h}
    return cleaned
"""
    code2 = """
def _normalize_trusted_hosts(raw_items=None):
    '''Normalize advisory hosts.'''
    out = {item.strip().lower() for item in (raw_items or []) if item}
    return out
"""
    node1 = ast.parse(code1).body[0]
    node2 = ast.parse(code2).body[0]
    assert isinstance(node1, ast.FunctionDef)
    assert isinstance(node2, ast.FunctionDef)

    assert compute_normalized_ast_hash(node1) == compute_normalized_ast_hash(node2)
    assert node1.name == "_normalize_trusted_model_hosts"
    assert node2.name == "_normalize_trusted_hosts"


def test_validate_codebase_duplication_catches_renamed_clones_and_skips_docstring_wrappers():
    from scripts.validate_architecture import validate_codebase_duplication

    mod_a = ast.parse(
        "def _normalize_model_hosts(items=None):\n"
        '    """Normalize model hosts."""\n'
        "    cleaned = [x.strip().lower() for x in (items or [])]\n"
        "    return frozenset(cleaned)\n\n"
        "def _get_model_hosts(self):\n"
        '    """Thin getter with docstring."""\n'
        "    return _resolve_hosts(self._settings)\n"
    )
    mod_b = ast.parse(
        "def _normalize_advisory_hosts(values=None):\n"
        '    """Normalize advisory hosts."""\n'
        "    normalized = [v.strip().lower() for v in (values or [])]\n"
        "    return frozenset(normalized)\n\n"
        "def _get_advisory_hosts(self):\n"
        '    """Another thin getter with docstring."""\n'
        "    return _resolve_hosts(self._settings)\n"
    )

    errors: list[str] = []
    validate_codebase_duplication(
        errors,
        parsed_files=[
            ("app/core/mod_a.py", mod_a),
            ("app/core/mod_b.py", mod_b),
        ],
    )
    assert len(errors) == 1
    assert "_normalize_advisory_hosts" in errors[0]
    assert "_normalize_model_hosts" in errors[0]
    assert "_get_advisory_hosts" not in errors[0]


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


def test_dict_compat_mixin_canonical_registration():
    assert "DictCompatMixin" in CANONICAL_UTILITY_REGISTRY
    assert (
        CANONICAL_UTILITY_REGISTRY["DictCompatMixin"] == "app/core/domain_contracts.py"
    )

    # Re-defining DictCompatMixin as a class outside home module raises error
    errors = []
    hashes = {}
    bad_visitor = UtilityAntiDuplicationVisitor("app/ui/custom_ui.py", errors, hashes)
    tree = ast.parse("class DictCompatMixin:\n    pass\n")
    bad_visitor.visit(tree)
    assert len(errors) == 1
    assert "Re-definition of canonical utility 'DictCompatMixin' found." in errors[0]

    # Defining DictCompatMixin inside home module is allowed
    good_errors = []
    good_hashes = {}
    good_visitor = UtilityAntiDuplicationVisitor(
        "app/core/domain_contracts.py", good_errors, good_hashes
    )
    good_tree = ast.parse("class DictCompatMixin:\n    pass\n")
    good_visitor.visit(good_tree)
    assert len(good_errors) == 0


def test_redundant_dict_adapter_methods_flagged_in_domain_models():
    # Defining redundant dictionary adapter methods in QuarantineRecordModel is flagged
    errors = []
    hashes = {}
    visitor = UtilityAntiDuplicationVisitor(
        "app/core/domain_contracts.py", errors, hashes
    )
    tree = ast.parse("""
class QuarantineRecordModel:
    def __getitem__(self, item: str):
        return getattr(self, item)
    def get(self, item: str, default=None):
        return getattr(self, item, default)
    def __contains__(self, item: str):
        return item in self.model_fields
    def dict(self, *args, **kwargs):
        return self.model_dump(*args, **kwargs)
""")
    visitor.visit(tree)
    assert len(errors) == 4
    for method_name in ("__getitem__", "get", "__contains__", "dict"):
        assert any(
            f"Redundant dictionary adapter method '{method_name}' found in class 'QuarantineRecordModel'."
            in err
            for err in errors
        )

    # DictCompatMixin itself defining dictionary methods is allowed
    mixin_errors = []
    mixin_hashes = {}
    mixin_visitor = UtilityAntiDuplicationVisitor(
        "app/core/domain_contracts.py", mixin_errors, mixin_hashes
    )
    mixin_tree = ast.parse("""
class DictCompatMixin:
    def __getitem__(self, item: str):
        pass
    def get(self, item: str, default=None):
        pass
    def __contains__(self, item: str):
        pass
    def dict(self, *args, **kwargs):
        pass
""")
    mixin_visitor.visit(mixin_tree)
    assert len(mixin_errors) == 0


def test_redundant_dict_adapter_methods_flagged_in_clinical_models():
    # Defining redundant dictionary adapter methods in clinical compliance models is flagged
    errors = []
    hashes = {}
    visitor = UtilityAntiDuplicationVisitor(
        "app/plugins/clinical_compliance/clinical_compliance.py", errors, hashes
    )
    tree = ast.parse("""
class FoundEssentialDocument:
    def __getitem__(self, item: str):
        return getattr(self, item)
    def get(self, item: str, default=None):
        return getattr(self, item, default)
    def __contains__(self, item: str):
        return item in self.model_fields
    def dict(self, *args, **kwargs):
        return self.model_dump(*args, **kwargs)
""")
    visitor.visit(tree)
    assert len(errors) == 4
    for method_name in ("__getitem__", "get", "__contains__", "dict"):
        assert any(
            f"Redundant dictionary adapter method '{method_name}' found in class 'FoundEssentialDocument'."
            in err
            for err in errors
        )

    pipeline_errors = []
    pipeline_hashes = {}
    pipeline_visitor = UtilityAntiDuplicationVisitor(
        "app/plugins/clinical_compliance/cro_multi_study_pipeline.py",
        pipeline_errors,
        pipeline_hashes,
    )
    pipeline_tree = ast.parse("""
class MasterPipelineResult:
    def __getitem__(self, item: str):
        return getattr(self, item)
    def get(self, item: str, default=None):
        return getattr(self, item, default)
    def __contains__(self, item: str):
        return item in self.model_fields
    def dict(self, *args, **kwargs):
        return self.model_dump(*args, **kwargs)
""")
    pipeline_visitor.visit(pipeline_tree)
    assert len(pipeline_errors) == 4
    for method_name in ("__getitem__", "get", "__contains__", "dict"):
        assert any(
            f"Redundant dictionary adapter method '{method_name}' found in class 'MasterPipelineResult'."
            in err
            for err in pipeline_errors
        )


def test_compute_normalized_ast_hash_zero_deepcopy_and_non_mutating(monkeypatch):
    """Verify compute_normalized_ast_hash never calls copy.deepcopy and leaves input AST untouched."""
    import copy

    monkeypatch.setattr(
        copy,
        "deepcopy",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("copy.deepcopy must not be called")
        ),
    )

    code1 = """
def sample(alpha: int, *items: str, flag: bool = True, **extra: object) -> list[int]:
    '''Docstring ignored.'''
    unused_decl: str
    total: int = alpha + len(items)
    mapped = [elem.upper() for elem in items if flag]
    try:
        result = helper(count=alpha, payload=mapped, **extra)
    except ValueError as err:
        raise RuntimeError(str(err)) from err
    return [total, len(result)]
"""
    code2 = """
def sample(beta, *vals, toggle=True, **kw):
    acc = beta + len(vals)
    transformed = [item.upper() for item in vals if toggle]
    try:
        out = helper(count=beta, payload=transformed, **kw)
    except ValueError as err:
        raise RuntimeError(str(err)) from err
    return [acc, len(out)]
"""
    node1 = ast.parse(code1).body[0]
    before_dump = ast.dump(node1, include_attributes=True)
    node2 = ast.parse(code2).body[0]

    assert compute_normalized_ast_hash(node1) == compute_normalized_ast_hash(node2)
    assert ast.dump(node1, include_attributes=True) == before_dump
