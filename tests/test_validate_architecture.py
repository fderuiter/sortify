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


def test_validate_architecture_global_config_fallback(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", "pyproject.toml"])
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

    # Pass the temporary file path relative to app or directly
    rel_path = f"app/{bad_file.name}"
    # Temporarily place a bad file inside app directory
    real_bad_path = tmp_path / rel_path
    real_bad_path.parent.mkdir(parents=True, exist_ok=True)
    real_bad_path.write_text("import sys\nx = sys.frozen\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "app").mkdir(exist_ok=True)

    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", rel_path])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
