import sys

from scripts.validate_imports import (
    extract_file_imports,
    load_pyproject_dependencies,
    main,
    normalize_package_name,
    parse_requirement_package,
)
from scripts.validate_imports import (
    validate_imports as run_validate_imports,
)


def test_normalize_package_name():
    assert normalize_package_name("Pillow") == "pillow"
    assert normalize_package_name("python_docx") == "python-docx"
    assert normalize_package_name("PyYAML") == "pyyaml"
    assert normalize_package_name("scikit.learn") == "scikit-learn"


def test_parse_requirement_package():
    assert parse_requirement_package("watchdog>=6.0.0") == "watchdog"
    assert parse_requirement_package("sqlcipher3-wheels==0.5.7") == "sqlcipher3-wheels"
    assert parse_requirement_package("mkdocstrings[python]") == "mkdocstrings"
    assert (
        parse_requirement_package('pydantic-settings; python_version < "3.12"')
        == "pydantic-settings"
    )


def test_load_pyproject_dependencies(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = [
    "pillow",
    "python-pptx>=0.6.0",
]

[project.optional-dependencies]
ml = [
    "torch",
]

[dependency-groups]
dev = [
    "pytest",
]
""",
        encoding="utf-8",
    )

    core, optional, dev = load_pyproject_dependencies(pyproject)
    assert "pillow" in core
    assert "python-pptx" in core
    assert "torch" in optional
    assert "pytest" in dev


def test_extract_file_imports(tmp_path):
    sample_file = tmp_path / "sample.py"
    sample_file.write_text(
        """
import os
import sys
import PIL
from pptx import Presentation
from app.core import db

def foo():
    import scipy
""",
        encoding="utf-8",
    )

    imports = extract_file_imports(sample_file)
    root_mods = [m[0] for m in imports]
    assert "os" in root_mods
    assert "sys" in root_mods
    assert "PIL" in root_mods
    assert "pptx" in root_mods
    assert "app" in root_mods
    assert "scipy" in root_mods


def test_validate_imports_success(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = [
    "pillow",
    "python-pptx",
]
""",
        encoding="utf-8",
    )

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    code_file = app_dir / "module.py"
    code_file.write_text(
        """
import os
import PIL
from pptx import Presentation
""",
        encoding="utf-8",
    )

    success, violations = run_validate_imports(app_dir, pyproject)
    assert success is True
    assert len(violations) == 0


def test_validate_imports_failure(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = [
    "pillow",
]
""",
        encoding="utf-8",
    )

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    code_file = app_dir / "module.py"
    code_file.write_text(
        """
import PIL
import requests
""",
        encoding="utf-8",
    )

    success, violations = run_validate_imports(app_dir, pyproject)
    assert success is False
    assert len(violations) == 1
    assert violations[0]["module"] == "requests"
    assert violations[0]["package"] == "requests"


def test_main_cli_pass(monkeypatch, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = ["pillow"]
""",
        encoding="utf-8",
    )

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "test.py").write_text("import PIL", encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv", ["validate_imports.py", "--root-dir", str(tmp_path)]
    )
    exit_code = main()
    assert exit_code == 0


def test_main_cli_fail(monkeypatch, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = []
""",
        encoding="utf-8",
    )

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "test.py").write_text("import requests", encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv", ["validate_imports.py", "--root-dir", str(tmp_path)]
    )
    exit_code = main()
    assert exit_code == 1
