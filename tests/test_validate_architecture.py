import sys

import pytest

from scripts.validate_architecture import main


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


def test_validate_architecture_incremental_duplication_order_independent(
    monkeypatch, tmp_path
):
    from scripts.validate_architecture import validate_codebase_duplication

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
    errors_a = []
    validate_codebase_duplication(errors_a, target_files={path_a})
    assert any(
        "Duplicate function body found" in err and path_a in err for err in errors_a
    )

    # Test incremental target on File B
    errors_b = []
    validate_codebase_duplication(errors_b, target_files={path_b})
    assert any(
        "Duplicate function body found" in err and path_b in err for err in errors_b
    )


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

    from scripts.validate_architecture import validate_import_layers

    errors = []
    validate_import_layers(errors, filepath, ast.parse(source))
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


def _settings_key_errors(
    source, declared=frozenset({"MAX_WORKERS", "MAX_SPREADSHEET_ROWS"})
):
    import ast

    from scripts.validate_architecture import validate_settings_keys

    errors = []
    validate_settings_keys(errors, "app/core/x.py", ast.parse(source), set(declared))
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

    from scripts.validate_architecture import validate_import_time_globals

    errors = []
    validate_import_time_globals(errors, "app/core/x.py", ast.parse(source))
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


def test_app_has_no_import_time_global_patches():
    import ast
    from pathlib import Path

    from scripts.validate_architecture import validate_import_time_globals

    errors = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        validate_import_time_globals(errors, str(path), tree)
    assert errors == []


def _detection_errors(source):
    import ast

    from scripts.validate_architecture import validate_no_test_detection

    errors = []
    validate_no_test_detection(errors, "app/core/x.py", ast.parse(source))
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


def test_app_has_no_test_detection():
    import ast
    from pathlib import Path

    from scripts.validate_architecture import validate_no_test_detection

    errors = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        validate_no_test_detection(errors, str(path), tree)
    assert errors == []
