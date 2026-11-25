import sys

import pytest

from scripts.validate_architecture import main


@pytest.fixture(scope="module")
def parsed_app_files():
    from scripts.validate_architecture import _collect_parsed_app_files

    return _collect_parsed_app_files("app")


@pytest.mark.slow
def test_validate_architecture_full_scan_pass(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_validate_architecture_incremental_pass(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", "app/core/crypto.py"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_validate_architecture_incremental_irrelevant_file(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["validate_architecture.py", "README.md", "tests/test_foo.py"]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def _run_duplication_check(
    errors: list[str],
    target_files: set[str] | None = None,
) -> None:
    from scripts.validate_architecture import (
        _CHECK_HASH_INDEX_ONLY,
        ArchitectureASTVisitor,
        _collect_parsed_app_files,
        _report_duplicate_function_hashes,
    )

    function_hashes: dict[str, list[tuple[str, int, str]]] = {}
    for filepath, tree in _collect_parsed_app_files("app"):
        if target_files is None or filepath in target_files:
            ArchitectureASTVisitor(
                filepath, errors, function_hashes=function_hashes
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


def test_validate_architecture_incremental_duplication_order_independent(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    core_dir = tmp_path / "app" / "core"
    core_dir.mkdir(parents=True, exist_ok=True)

    # File A and File B have identical functions
    file_a = core_dir / "zz_dup_a.py"
    file_b = core_dir / "zz_dup_b.py"
    fn_code = "def helper_x(val: int) -> int:\n    a = val + 1\n    b = a * 2\n    return b + 3\n"
    file_a.write_text(fn_code, encoding="utf-8")
    file_b.write_text(fn_code, encoding="utf-8")

    path_a = "app/core/zz_dup_a.py"
    path_b = "app/core/zz_dup_b.py"

    # Test incremental target on File A
    errors_a: list[str] = []
    _run_duplication_check(errors_a, target_files={path_a})
    assert any(
        "Duplicate function body found" in err and path_a in err for err in errors_a
    )

    # Test incremental target on File B
    errors_b: list[str] = []
    _run_duplication_check(errors_b, target_files={path_b})
    assert any(
        "Duplicate function body found" in err and path_b in err for err in errors_b
    )


def test_validate_architecture_same_file_ui_duplicate_methods_fail(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    modals_dir = tmp_path / "app" / "ui" / "modals"
    modals_dir.mkdir(parents=True, exist_ok=True)

    modal_file = modals_dir / "dup_modal.py"
    modal_file.write_text(
        "class FirstModal:\n"
        "    def action_cancel(self) -> None:\n"
        '        """Cancel first modal."""\n'
        "        self.action_close()\n\n"
        "class SecondModal:\n"
        "    def action_cancel(self) -> None:\n"
        '        """Cancel second modal."""\n'
        "        self.action_close()\n",
        encoding="utf-8",
    )
    rel_path = "app/ui/modals/dup_modal.py"

    full_errors: list[str] = []
    _run_duplication_check(full_errors)
    assert any(
        "Duplicate function body found for 'action_cancel'" in err and rel_path in err
        for err in full_errors
    )

    inc_errors: list[str] = []
    _run_duplication_check(inc_errors, target_files={rel_path})
    assert any(
        "Duplicate function body found for 'action_cancel'" in err and rel_path in err
        for err in inc_errors
    )


def test_validate_architecture_allows_docstring_stubs_and_overloads(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    (app_dir / "stream_stubs.py").write_text(
        "from typing import overload\n\n"
        "class NullA:\n"
        "    def isatty(self) -> bool:\n"
        '        """Return False for null stream."""\n'
        "        return False\n\n"
        "class NullB:\n"
        "    def isatty(self) -> bool:\n"
        '        """Return False for null stream."""\n'
        "        return False\n\n"
        "@overload\n"
        "def query(sel: str) -> int: ...\n"
        "@overload\n"
        "def query(sel: bytes) -> str: ...\n"
        "def query(sel: object) -> object:\n"
        "    return sel\n",
        encoding="utf-8",
    )

    errors: list[str] = []
    _run_duplication_check(errors)
    assert errors == []


def test_modal_shell_conventions_flag_direct_modal_screen_and_inlined_css():
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    bad_source = (
        "from textual.screen import ModalScreen\n"
        "from app.ui.layout import ResponsiveModalScreen\n\n"
        "class LegacyDialogModal(ModalScreen[bool]):\n"
        '    CSS = "LegacyDialogModal { align: center middle; background: rgba(0, 0, 0, 0.6); }"\n\n'
        "class ReInlinedModal(ResponsiveModalScreen[None]):\n"
        '    MODAL_CSS = ".button-row { dock: bottom; align: right middle; }"\n'
    )
    good_source = (
        "from app.ui.layout import ResponsiveModalScreen\n\n"
        "class CleanModal(ResponsiveModalScreen[bool]):\n"
        '    MODAL_CSS = ".modal-box { max-width: 60; }"\n'
    )

    errors: list[str] = []
    for filepath, tree in (
        ("app/ui/modals/bad.py", ast.parse(bad_source)),
        ("app/ui/modals/good.py", ast.parse(good_source)),
    ):
        ArchitectureASTVisitor(filepath, errors).visit(tree)
    assert any("inherits directly from 'ModalScreen'" in err for err in errors)
    assert any("defines 'CSS' directly" in err for err in errors)
    assert any("re-inlines shared modal shell CSS rules" in err for err in errors)
    assert not any("good.py" in err for err in errors)


def test_modal_shell_conventions_reject_unnested_breakpoint_selectors():
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    bad_top_level = (
        "from app.ui.layout import ResponsiveModalScreen\n\n"
        "class TopLevelCompactModal(ResponsiveModalScreen[None]):\n"
        '    MODAL_CSS = ".compact .modal-box { width: 98%; }"\n\n'
        "class TopLevelNarrowModal(ResponsiveModalScreen[None]):\n"
        '    MODAL_CSS = ".narrow .button-row { grid-size: 2; }"\n\n'
        "class UnboundNestedModal(ResponsiveModalScreen[None]):\n"
        '    MODAL_CSS = "UnboundNestedModal { .compact .button-row { margin-top: 0; } }"\n\n'
        "class TopLevelAmpersandModal(ResponsiveModalScreen[None]):\n"
        '    MODAL_CSS = "&.compact .modal-box { width: 98%; }"\n'
    )
    good_nested = (
        "from app.ui.layout import ResponsiveModalScreen\n\n"
        "class ProperlyNestedModal(ResponsiveModalScreen[None]):\n"
        '    MODAL_CSS = """\n'
        "    .narrow-input { width: 12; }\n"
        "    ProperlyNestedModal {\n"
        "        &.compact .narrow-input { height: 1; }\n"
        "        &.narrow .button-row { grid-size: 2; }\n"
        "    }\n"
        '    """\n'
    )

    errors: list[str] = []
    for filepath, tree in (
        ("app/ui/modals/bad_bp.py", ast.parse(bad_top_level)),
        ("app/ui/modals/good_bp.py", ast.parse(good_nested)),
    ):
        ArchitectureASTVisitor(filepath, errors).visit(tree)
    for cls_name in (
        "TopLevelCompactModal",
        "TopLevelNarrowModal",
        "UnboundNestedModal",
        "TopLevelAmpersandModal",
    ):
        assert any(
            cls_name in err and "un-nested '.compact' or '.narrow'" in err
            for err in errors
        ), f"Expected un-nested breakpoint error for {cls_name}, got {errors}"
    assert not any("good_bp.py" in err for err in errors)


def test_validate_architecture_incremental_violation(monkeypatch, tmp_path):
    bad_file = tmp_path / "bad_module.py"
    bad_file.write_text("import sys\nx = sys.frozen\n", encoding="utf-8")

    rel_path = f"app/{bad_file.name}"
    real_bad_path = tmp_path / rel_path
    real_bad_path.parent.mkdir(parents=True, exist_ok=True)
    real_bad_path.write_text("import sys\nx = sys.frozen\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "app").mkdir(exist_ok=True)

    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", rel_path])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


def _layer_errors(filepath, source):
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    ArchitectureASTVisitor(filepath, errors).visit(ast.parse(source))
    return errors


def test_import_layers_flag_function_local_upward_import():
    source = "def f():\n    from app.ui.notifications import NotificationManager\n"
    errors = _layer_errors("app/core/example.py", source)
    assert len(errors) == 1
    assert "app/core/example.py:2" in errors[0]
    assert "'app.ui'" in errors[0]


def test_import_layers_flag_entrypoint_and_tooling_imports():
    assert _layer_errors("app/ui/view.py", "import app.main\n")
    assert _layer_errors("app/cli/tool.py", "from app.main import build_parser\n")
    assert _layer_errors("app/main.py", "import pytest\n")
    assert _layer_errors("app/core/x.py", "from scripts import validate_links\n")


def test_import_layers_allow_downward_and_similar_names():
    assert not _layer_errors("app/ui/view.py", "from app.core import mover\n")
    assert not _layer_errors("app/core/x.py", "import app.main_helpers\n")
    assert not _layer_errors("app/core/x.py", "from . import ui\n")
    assert not _layer_errors("app/config.py", "import app.ui\n")


def test_import_layers_have_no_docs_generator_exemption():
    # The docs generator moved to scripts/, so app.cli gets no exemption.
    assert _layer_errors("app/cli/generate_docs.py", "import sandbox_cli\n")


@pytest.mark.parametrize(
    "source",
    [
        "import app.ui.tui",
        "from app.ui.tui import AutoSorterTUI",
        "from app.ui import tui",
        "def f():\n    from ...ui import tui",
        "def f():\n    from ...ui.tui import AutoSorterTUI",
    ],
)
def test_plugin_cannot_import_full_tui(source):
    assert _layer_errors("app/plugins/example/views.py", source)


def test_relative_imports_resolve_from_package_initializers():
    assert _layer_errors("app/plugins/example/__init__.py", "from ...ui import tui")
    assert _layer_errors("app/core/example.py", "from ..ui import notifications")
    assert not _layer_errors("app/plugins/example/views.py", "from ...ui import a11y")
    assert not _layer_errors(
        "app/plugins/example/views.py", "from ...ui.a11y import A11yMixin"
    )


def _settings_key_errors(
    source, declared=frozenset({"MAX_WORKERS", "MAX_SPREADSHEET_ROWS"})
):
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    ArchitectureASTVisitor(
        "app/core/x.py", errors, declared_settings=set(declared)
    ).visit(ast.parse(source))
    return errors


def test_settings_keys_flag_undeclared_getattr_reads():
    assert _settings_key_errors('getattr(settings, "WORKER_CONCURRENCY", 4)\n')
    assert _settings_key_errors('getattr(self.settings, "PROTECTED_DIRECTORIES")\n')
    assert _settings_key_errors('getattr(runtime_settings, "MOVE_CHUNK_SIZE", None)\n')


def test_settings_keys_allow_declared_keys_and_unrelated_getattrs():
    assert not _settings_key_errors('getattr(settings, "MAX_WORKERS", 4)\n')
    assert not _settings_key_errors('getattr(settings, "MAX_SPREADSHEET_ROWS", 10)\n')
    assert not _settings_key_errors('getattr(settings, "cancel_check", None)\n')
    assert not _settings_key_errors('getattr(obj, "UNDECLARED", None)\n')


def test_declared_settings_keys_include_fields_and_property_aliases():
    from scripts.validate_architecture import declared_settings_keys

    keys = declared_settings_keys()
    assert {"MAX_QUEUE_CAPACITY", "DEDUP_WINDOW", "WORKER_TIMEOUT"} <= keys
    assert "MAX_SPREADSHEET_ROWS" in keys
    assert "WORKER_CONCURRENCY" not in keys


def _global_errors(source):
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    ArchitectureASTVisitor("app/core/x.py", errors).visit(ast.parse(source))
    return errors


def test_import_time_globals_flag_module_level_patches():
    assert _global_errors('import sys\nsys.modules["sqlite3"] = object()\n')
    assert _global_errors("import socket\nsocket.socket.connect = None\n")
    assert _global_errors(
        'import sys\ntry:\n    import x\nexcept ImportError:\n    sys.modules["x"] = None\n'
    )
    assert _global_errors("import socket\nclass C:\n    socket.getfqdn = None\n")


def test_import_time_globals_allow_install_functions_and_reads():
    assert not _global_errors(
        "import socket\ndef install():\n    socket.getfqdn = None\n"
    )
    assert not _global_errors('import sys\ntorch = sys.modules["torch"]\n')
    assert not _global_errors('import sys\nsys.modules["torch"].set_num_threads(1)\n')


def test_app_has_no_import_time_global_patches(parsed_app_files):
    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    for filepath, tree in parsed_app_files:
        ArchitectureASTVisitor(filepath, errors).visit(tree)
    assert errors == []


def _detection_errors(source):
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    ArchitectureASTVisitor("app/core/x.py", errors).visit(ast.parse(source))
    return errors


def test_test_detection_flags_mock_checks_and_pytest_env():
    assert _detection_errors("import unittest.mock\n")
    assert _detection_errors("from unittest import mock\n")
    assert _detection_errors("from unittest.mock import MagicMock\n")
    assert _detection_errors("def f(x):\n    return isinstance(x, MagicMock)\n")
    assert _detection_errors("import unittest\nunittest.mock.NonCallableMock\n")
    assert _detection_errors('import os\nos.environ.get("PYTEST_CURRENT_TEST")\n')
    assert _detection_errors('hasattr(obj, "mock_add_spec")\n')


def test_test_detection_allows_ordinary_code():
    assert not _detection_errors("import unittest\nimport shutil\n")
    assert not _detection_errors('import os\nos.environ.get("SORTIFY_DEBUG")\n')
    assert not _detection_errors("mock_mode = False\n")


def test_app_has_no_test_detection(parsed_app_files):
    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    for filepath, tree in parsed_app_files:
        ArchitectureASTVisitor(filepath, errors).visit(tree)
    assert errors == []


def _ml_errors(source, filepath="app/core/example.py"):
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    ArchitectureASTVisitor(filepath, errors).visit(ast.parse(source))
    return errors


@pytest.mark.parametrize(
    "pkg",
    [
        "torch",
        "transformers",
        "easyocr",
        "onnxruntime",
        "llama_cpp",
        "sentence_transformers",
    ],
)
def test_lazy_ml_imports_flag_top_level_packages(pkg):
    errors_import = _ml_errors(f"import {pkg}\n")
    assert len(errors_import) == 1
    assert "app/core/example.py:1" in errors_import[0]
    assert pkg in errors_import[0]
    assert "lazily imported" in errors_import[0]

    errors_from = _ml_errors(f"from {pkg} import helper\n")
    assert len(errors_from) == 1
    assert "app/core/example.py:1" in errors_from[0]
    assert pkg in errors_from[0]


def test_lazy_ml_imports_flag_submodules_and_multiple_aliases():
    assert _ml_errors("import torch.nn as nn\n")
    assert _ml_errors("from transformers.models.bert import BertModel\n")
    assert _ml_errors("from sentence_transformers.util import cos_sim\n")

    multiple = _ml_errors("import os, torch, transformers\n")
    assert len(multiple) == 2
    assert any("torch" in e for e in multiple)
    assert any("transformers" in e for e in multiple)


def test_lazy_ml_imports_flag_class_level_imports():
    source_class_import = """
class TextClassifier:
    import torch
"""
    errors = _ml_errors(source_class_import)
    assert len(errors) == 1
    assert "torch" in errors[0]

    source_class_from = """
class ModelPipeline:
    from sentence_transformers import SentenceTransformer
"""
    errors_from = _ml_errors(source_class_from)
    assert len(errors_from) == 1
    assert "sentence_transformers" in errors_from[0]


def test_lazy_ml_imports_flag_unguarded_control_flow():
    assert _ml_errors("if True:\n    import torch\n")
    assert _ml_errors("try:\n    import easyocr\nexcept ImportError:\n    pass\n")
    assert _ml_errors("if not TYPE_CHECKING:\n    import onnxruntime\n")
    assert _ml_errors("if TYPE_CHECKING:\n    pass\nelse:\n    import llama_cpp\n")


def test_lazy_ml_imports_allow_function_and_method_scopes():
    func_source = """
def run_embedding():
    import torch
    from transformers import AutoModel
    return AutoModel
"""
    assert _ml_errors(func_source) == []

    async_source = """
async def async_embed():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer
"""
    assert _ml_errors(async_source) == []

    method_source = """
class EmbeddingEngine:
    def __init__(self):
        self.ready = True

    def compute(self):
        import onnxruntime as ort
        from llama_cpp import Llama
        return ort

    async def ocr(self):
        import easyocr
        return easyocr
"""
    assert _ml_errors(method_source) == []


def test_lazy_ml_imports_allow_type_checking_guards():
    name_guard = """
if TYPE_CHECKING:
    import torch
    from transformers import AutoModel
    from sentence_transformers import SentenceTransformer
"""
    assert _ml_errors(name_guard) == []

    attr_guard = """
import typing

if typing.TYPE_CHECKING:
    import easyocr
    from onnxruntime import InferenceSession
"""
    assert _ml_errors(attr_guard) == []

    and_guard = """
import sys

if TYPE_CHECKING and sys.version_info >= (3, 11):
    import llama_cpp
"""
    assert _ml_errors(and_guard) == []

    class_guard = """
class Classifier:
    if TYPE_CHECKING:
        from torch import Tensor
"""
    assert _ml_errors(class_guard) == []


def test_lazy_ml_imports_allow_unrelated_packages():
    ordinary_code = """
import os
import sys
import sklearn
from pathlib import Path

class Foo:
    pass
"""
    assert _ml_errors(ordinary_code) == []


def test_app_has_no_eager_ml_imports(parsed_app_files):
    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    for filepath, tree in parsed_app_files:
        ArchitectureASTVisitor(filepath, errors).visit(tree)
    assert errors == []


def test_validate_architecture_incremental_ml_violation(monkeypatch, tmp_path):
    bad_file = tmp_path / "bad_ml_module.py"
    bad_file.write_text("import torch\n", encoding="utf-8")

    rel_path = f"app/{bad_file.name}"
    real_bad_path = tmp_path / rel_path
    real_bad_path.parent.mkdir(parents=True, exist_ok=True)
    real_bad_path.write_text("import torch\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "app").mkdir(exist_ok=True)

    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", rel_path])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


@pytest.mark.parametrize(
    "source",
    [
        "DBWorker()",
        "def f():\n    db_worker.DBWorker()",
        "from app.core.db_worker import DBWorker as Writer\nWriter()",
    ],
)
def test_writer_construction_requires_composition_root(source):
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    errors: list[str] = []
    ArchitectureASTVisitor("app/ui/example.py", errors).visit(ast.parse(source))
    assert errors
    errors = []
    ArchitectureASTVisitor("app/core/services.py", errors).visit(ast.parse(source))
    assert not errors


def test_validate_architecture_single_app_walk_and_zero_deepcopy(monkeypatch, tmp_path):
    """Verify full validate_architecture scan walks app/ once, parses each app file once, and skips copy.deepcopy."""
    import ast
    import copy
    import os

    from scripts.diagram_schema import SYSTEM_DIAGRAM_SPECS

    core_dir = tmp_path / "app" / "core"
    modals_dir = tmp_path / "app" / "ui" / "modals"
    diagrams_dir = tmp_path / "docs" / "assets" / "diagrams"
    core_dir.mkdir(parents=True)
    modals_dir.mkdir(parents=True)
    diagrams_dir.mkdir(parents=True)

    (tmp_path / "app" / "config.py").write_text(
        "class Settings:\n    MAX_DEPTH: int = 3\n",
        encoding="utf-8",
    )
    (core_dir / "scanner.py").write_text(
        "def compute_budget(items: list[int]) -> int:\n"
        "    total = sum(items)\n"
        "    return total * 2\n",
        encoding="utf-8",
    )
    (modals_dir / "sample_modal.py").write_text(
        "from app.ui.layout import ResponsiveModalScreen\n\n"
        "class SampleModal(ResponsiveModalScreen[bool]):\n"
        '    MODAL_CSS = "SampleModal { &.compact .modal-box { max-width: 60; } }"\n\n'
        "    def format_summary(self, count: int) -> str:\n"
        "        label = f'{count} item(s)'\n"
        "        return label.strip()\n",
        encoding="utf-8",
    )
    for spec_id, spec in SYSTEM_DIAGRAM_SPECS.items():
        (diagrams_dir / f"{spec_id}.mmd").write_text(
            spec.to_mermaid(), encoding="utf-8"
        )
        (diagrams_dir / f"{spec_id}.svg").write_text("<svg/>\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    real_walk = os.walk
    app_walk_calls = 0

    def counting_walk(top, *args, **kwargs):
        nonlocal app_walk_calls
        if str(top) == "app":
            app_walk_calls += 1
        return real_walk(top, *args, **kwargs)

    real_parse = ast.parse
    parsed_files: list[str] = []

    def counting_parse(source, filename="<unknown>", *args, **kwargs):
        if isinstance(filename, str) and filename.replace("\\", "/").startswith("app/"):
            parsed_files.append(filename.replace("\\", "/"))
        return real_parse(source, filename, *args, **kwargs)

    monkeypatch.setattr(os, "walk", counting_walk)
    monkeypatch.setattr(ast, "parse", counting_parse)
    monkeypatch.setattr(
        copy,
        "deepcopy",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("copy.deepcopy must not be called")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    assert app_walk_calls == 1
    assert len(parsed_files) == 3
    assert len(parsed_files) == len(set(parsed_files))


def test_validate_architecture_full_scan_visits_each_ast_once_without_ast_walk(
    monkeypatch,
):
    """Verify full scan visits each parsed app/ Module AST once and never calls ast.walk."""
    import ast

    real_parse = ast.parse
    parsed_paths: list[str] = []

    def tracking_parse(source, filename="<unknown>", *args, **kwargs):
        parsed_paths.append(str(filename).replace("\\", "/"))
        return real_parse(source, filename, *args, **kwargs)

    real_visit = ast.NodeVisitor.visit
    visited_module_ids: list[int] = []

    def tracking_visit(self, node):
        if isinstance(node, ast.Module):
            visited_module_ids.append(id(node))
        return real_visit(self, node)

    monkeypatch.setattr(ast, "parse", tracking_parse)
    monkeypatch.setattr(ast.NodeVisitor, "visit", tracking_visit)
    monkeypatch.setattr(
        ast,
        "walk",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("ast.walk must not be called during validate_architecture")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py"])

    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    app_parses = [p for p in parsed_paths if p.startswith("app/") or "/app/" in p]
    assert app_parses
    assert len(app_parses) == len(set(app_parses))
    assert len(visited_module_ids) == len(app_parses)
    assert len(visited_module_ids) == len(set(visited_module_ids))


def test_validate_architecture_drifted_diagram_inputs_fail_without_parsing_app(
    monkeypatch, tmp_path
):
    """Passing drifted diagram asset or diagram_schema.py runs diagram validation without parsing app/."""
    import ast

    diagrams_dir = tmp_path / "docs" / "assets" / "diagrams"
    diagrams_dir.mkdir(parents=True)
    (diagrams_dir / "core_architecture.mmd").write_text(
        "flowchart TD\n    DRIFTED[stale]\n", encoding="utf-8"
    )
    (diagrams_dir / "core_architecture.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "diagram_schema.py").write_text(
        "# stub\n", encoding="utf-8"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        ast,
        "parse",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("app/ ASTs must not be parsed for diagram-only inputs")
        ),
    )

    for cli_arg in (
        "docs/assets/diagrams/core_architecture.mmd",
        "scripts/diagram_schema.py",
    ):
        monkeypatch.setattr(sys, "argv", ["validate_architecture.py", cli_arg])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_validate_architecture_targeted_diagram_hydrates_single_spec(monkeypatch):
    """Passing a single diagram asset hydrates only that spec key in LazyDiagramSpecsDict."""
    import ast

    from scripts import diagram_schema

    saved_specs = dict(diagram_schema._INSTANTIATED_SPECS)
    diagram_schema._INSTANTIATED_SPECS.clear()
    try:
        monkeypatch.setattr(
            ast,
            "parse",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("app/ ASTs must not be parsed for diagram asset checks")
            ),
        )
        monkeypatch.setattr(
            sys,
            "argv",
            ["validate_architecture.py", "docs/assets/diagrams/setup_wizard_flow.mmd"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 0
        assert set(diagram_schema._INSTANTIATED_SPECS.keys()) == {"setup_wizard_flow"}
    finally:
        diagram_schema._INSTANTIATED_SPECS.clear()
        diagram_schema._INSTANTIATED_SPECS.update(saved_specs)


def test_validate_architecture_non_core_edit_defers_diagram_schema_import(monkeypatch):
    """Non-core app/ edits do not import scripts.diagram_schema."""
    monkeypatch.setitem(sys.modules, "scripts.diagram_schema", None)
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", "app/main.py"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_validate_architecture_config_and_pyproject_escalate_to_full_scan(
    monkeypatch, tmp_path
):
    """Passing pyproject.toml or app/config.py runs full Settings and architecture validation."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'test'\n", encoding="utf-8"
    )
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "config.py").write_text(
        "class Settings:\n    DECLARED_KEY: int = 1\n", encoding="utf-8"
    )
    (app_dir / "reader.py").write_text(
        'val = getattr(settings, "UNDECLARED_KEY", 0)\n', encoding="utf-8"
    )

    for trigger in ("pyproject.toml", "app/config.py"):
        monkeypatch.setattr(sys, "argv", ["validate_architecture.py", trigger])
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1


def test_validate_architecture_unrelated_paths_do_not_parse_app(monkeypatch):
    """Unrelated CLI paths exit 0 immediately without walking or parsing app/."""
    import ast
    import os

    monkeypatch.setattr(
        os,
        "walk",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("os.walk must not run on unrelated paths")
        ),
    )
    monkeypatch.setattr(
        ast,
        "parse",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("ast.parse must not run on unrelated paths")
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["validate_architecture.py", "README.md", "tests/test_foo.py"]
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_writer_construction_catches_alias_imported_after_call():
    """Verify single-pass service ownership check catches aliases imported later in module."""
    import ast

    from scripts.validate_architecture import ArchitectureASTVisitor

    source = "def build():\n    return Writer()\n\nfrom app.core.db_worker import DBWorker as Writer\n"
    errors: list[str] = []
    ArchitectureASTVisitor("app/ui/example.py", errors).visit(ast.parse(source))
    assert len(errors) == 1
    assert "DBWorker" in errors[0]
