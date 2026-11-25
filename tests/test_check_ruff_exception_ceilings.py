"""Unit tests for scripts/check_ruff_exception_ceilings.py and CI/pre-commit wiring."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.check_ruff_exception_ceilings import (
    REPO_ROOT,
    check,
    check_per_file_ignores,
    load_ceilings,
    main,
)

CLEAN_PROTECTED_MODULES: tuple[str, ...] = (
    "app/cli/ledger_cli.py",
    "app/core/metadata.py",
    "app/core/session.py",
    "app/core/verifier.py",
    "app/ui/mixins/__init__.py",
    "app/ui/modals/__init__.py",
    "app/ui/modals/command_palette.py",
    "app/ui/modals/help.py",
    "scripts/check_coverage_floors.py",
    "scripts/check_release_changes.py",
    "scripts/check_ruff_exception_ceilings.py",
    "scripts/check_ty_warnings.py",
    "scripts/classify_pr.py",
    "scripts/diagram_schema.py",
    "scripts/prepare_offline.py",
    "scripts/snapshot_utils.py",
)


def _diag(
    filename: str,
    *,
    code: str | None = "BLE001",
    row: int = 10,
    col: int = 5,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": f"{code or 'syntax-error'}: sample diagnostic",
        "filename": filename,
        "location": {"row": row, "column": col},
        "end_location": {"row": row, "column": col + 6},
    }


def test_matching_ceilings_and_ignored_tests_pass() -> None:
    report = [
        _diag("app/core/mover.py", code="BLE001", row=12),
        _diag("app/core/mover.py", code="S110", row=13),
        _diag("scripts/build.py", code="SIM105", row=40),
        _diag("tests/test_sample.py", code="BLE001", row=8),
    ]
    ceilings = {"app/core/mover.py": 2, "scripts/build.py": 1}

    assert check(report, ceilings) == []


def test_empty_ceilings_and_empty_report_pass() -> None:
    assert check([], {}) == []


def test_violation_in_unlisted_clean_file_fails() -> None:
    failures = check(
        [_diag("app/ui/modals/help.py", code="S110", row=42)],
        {"scripts/build.py": 1},
    )

    assert any(
        "app/ui/modals/help.py: 1 violation(s) exceeds its ceiling of 0" in f
        for f in failures
    )


def test_violation_above_ceiling_fails() -> None:
    report = [
        _diag("app/ui/tui.py", code="BLE001", row=10),
        _diag("app/ui/tui.py", code="S110", row=11),
        _diag("app/ui/tui.py", code="SIM105", row=9),
    ]
    failures = check(report, {"app/ui/tui.py": 2})

    assert len(failures) == 1
    assert "app/ui/tui.py: 3 violation(s) exceeds its ceiling of 2" in failures[0]


def test_violation_below_ceiling_prompts_lowering() -> None:
    report = [_diag("app/ui/tui.py", code="BLE001", row=10)]
    failures = check(report, {"app/ui/tui.py": 2})

    assert failures == [
        "app/ui/tui.py: 1 violation(s) is below its ceiling of 2; "
        "lower it to 1 in [tool.sortify.ruff-exception-ceilings]"
    ]


def test_zero_violations_prompts_removing_ceiling() -> None:
    failures = check([], {"app/ui/tui.py": 2})

    assert failures == [
        "app/ui/tui.py: 0 violations is below its ceiling of 2; "
        "remove it from [tool.sortify.ruff-exception-ceilings] and "
        "[tool.ruff.lint.per-file-ignores]"
    ]


def test_missing_file_non_positive_ceiling_and_forbidden_ceilings_fail(
    tmp_path: Path,
) -> None:
    present = tmp_path / "app" / "present.py"
    present.parent.mkdir(parents=True)
    present.write_text("x = 1\n", encoding="utf-8")

    failures = check(
        [],
        {
            "app/missing.py": 2,
            "app/present.py": 0,
            "tests/test_alpha.py": 1,
            "scripts/*.py": 1,
        },
        repo_root=tmp_path,
    )

    assert any("app/missing.py: file does not exist" in f for f in failures)
    assert any("app/present.py: invalid ceiling 0" in f for f in failures)
    assert any("tests/test_alpha.py: ceilings are forbidden" in f for f in failures)
    assert any("scripts/*.py: wildcard paths are forbidden" in f for f in failures)


def test_per_file_ignores_rejects_wildcards_missing_and_stale_clean_entries(
    tmp_path: Path,
) -> None:
    violating = tmp_path / "app" / "legacy.py"
    clean = tmp_path / "app" / "clean.py"
    violating.parent.mkdir(parents=True)
    violating.write_text("x = 1\n", encoding="utf-8")
    clean.write_text("x = 2\n", encoding="utf-8")

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.ruff.lint.per-file-ignores]\n"
        '"tests/*" = ["D", "BLE001", "S110", "SIM105"]\n'
        '"scripts/*" = ["D"]\n'
        '"app/legacy.py" = ["BLE001"]\n'
        '"app/ui/modals/*.py" = ["BLE001", "S110"]\n'
        '".agents/*" = ["SIM105"]\n'
        '"app/clean.py" = ["BLE001"]\n'
        '"app/missing.py" = ["S110"]\n',
        encoding="utf-8",
    )

    failures = check_per_file_ignores(
        pyproject,
        repo_root=tmp_path,
        ceilings={"app/legacy.py": 1},
    )
    assert len(failures) == 4
    assert any("app/ui/modals/*.py: wildcard per-file-ignores" in f for f in failures)
    assert any(".agents/*: wildcard per-file-ignores" in f for f in failures)
    assert any("app/clean.py: per-file-ignores" in f for f in failures)
    assert any("app/missing.py: file does not exist" in f for f in failures)


def test_windows_and_repo_prefixed_paths_normalize(tmp_path: Path) -> None:
    mod = tmp_path / "app" / "ui" / "tui.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("x = 1\n", encoding="utf-8")

    report = [
        _diag("app\\ui\\tui.py", code="BLE001", row=10),
        _diag(str(mod.resolve()), code="S110", row=20),
    ]
    assert check(report, {"app/ui/tui.py": 2}, repo_root=tmp_path) == []


def test_main_report_modes_live_probe_and_github_annotations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.ruff.lint.per-file-ignores]\n"
        '"scripts/*" = ["D"]\n'
        '"app/mod.py" = ["BLE001", "S110", "SIM105"]\n\n'
        "[tool.sortify.ruff-exception-ceilings]\n"
        '"app/mod.py" = 3\n',
        encoding="utf-8",
    )
    mod = tmp_path / "app" / "mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "def run() -> None:\n"
        "    try:\n"
        "        int('bad')\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    clean_script = tmp_path / "scripts" / "clean_tool.py"
    clean_script.parent.mkdir(parents=True)
    clean_script.write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")

    # Live isolated Ruff execution matches exact ceiling of 3 (SIM105 + BLE001 + S110)
    assert main(["prog"], repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "All 1 ruff exception ceilings matched (3 violations" in out

    # Injecting a silent handler into a clean script fails immediately
    clean_script.write_text(
        "def bad() -> None:\n"
        "    try:\n"
        "        int('x')\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    assert main(["prog", "--github-annotations"], repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "::error file=scripts/clean_tool.py,title=ruff exception ceiling::" in out

    # Pre-generated stale report emits annotation against pyproject.toml
    stale = tmp_path / "stale.json"
    stale.write_text("[]", encoding="utf-8")
    assert main(["prog", str(stale), "--github-annotations"], repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "::error file=pyproject.toml,title=ruff exception ceiling::" in out


def test_cli_help_and_invalid_report_paths(tmp_path: Path) -> None:
    script_path = str(REPO_ROOT / "scripts" / "check_ruff_exception_ceilings.py")

    res_help = subprocess.run(
        [sys.executable, script_path, "--help"],
        capture_output=True,
        text=True,
    )
    assert res_help.returncode == 0
    assert "usage:" in res_help.stdout.lower()

    missing = tmp_path / "missing.json"
    res_missing = subprocess.run(
        [sys.executable, script_path, str(missing)],
        capture_output=True,
        text=True,
    )
    assert res_missing.returncode == 1
    assert "does not exist" in res_missing.stderr

    res_dir = subprocess.run(
        [sys.executable, script_path, str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert res_dir.returncode == 1
    assert "not a regular file" in res_dir.stderr


def test_clean_modules_protected_and_head_passes_live_ratchet() -> None:
    pyproject = REPO_ROOT / "pyproject.toml"
    ceilings = load_ceilings(pyproject)
    assert (
        check_per_file_ignores(pyproject, repo_root=REPO_ROOT, ceilings=ceilings) == []
    )
    for clean_mod in CLEAN_PROTECTED_MODULES:
        assert (REPO_ROOT / clean_mod).is_file(), (
            f"Expected module to exist: {clean_mod}"
        )
        assert clean_mod not in ceilings, (
            f"Clean module {clean_mod} must have ceiling 0 (unlisted)"
        )
    assert main(["prog"]) == 0


def test_ci_and_precommit_register_ruff_exception_ceilings() -> None:
    precommit_cfg = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    local_repo = next(r for r in precommit_cfg["repos"] if r["repo"] == "local")
    hook = next(h for h in local_repo["hooks"] if h["id"] == "ruff-exception-ceilings")
    assert "scripts/check_ruff_exception_ceilings.py" in hook["entry"]
    assert hook.get("pass_filenames") is False
    pattern = re.compile(hook["files"])
    for path in (
        "app/ui/tui.py",
        "scripts/build.py",
        ".agents/skills/run-sortify/driver.py",
        "pyproject.toml",
        "uv.lock",
    ):
        assert pattern.search(path), f"Expected hook regex to match {path}"

    ci_cfg = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    static_steps = ci_cfg["jobs"]["static-checks"]["steps"]
    ceiling_step = next(
        (
            s
            for s in static_steps
            if "scripts/check_ruff_exception_ceilings.py" in s.get("run", "")
        ),
        None,
    )
    assert ceiling_step is not None
    assert "--github-annotations" in ceiling_step["run"]
