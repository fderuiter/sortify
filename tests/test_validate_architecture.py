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
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", "README.md", "tests/test_foo.py"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


def test_validate_architecture_global_config_fallback(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["validate_architecture.py", "pyproject.toml"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0


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
