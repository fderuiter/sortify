import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.check_ty_warnings import (
    PLATFORMS,
    REPO_ROOT,
    check,
    check_baseline,
    load_ceilings,
    main,
    run_ty_reports_parallel,
)


def _diag(
    path: str,
    *,
    severity: str = "minor",
    rule: str = "missing-attribute",
    line: int = 10,
    col: int = 5,
) -> dict[str, Any]:
    return {
        "check_name": rule,
        "description": f"{rule}: sample diagnostic",
        "severity": severity,
        "location": {
            "path": path,
            "positions": {
                "begin": {"line": line, "column": col},
                "end": {"line": line, "column": col + 4},
            },
        },
    }


def test_matching_ceilings_and_clean_unlisted_files_pass() -> None:
    report = [
        _diag("tests/test_alpha.py", line=12),
        _diag("tests/test_alpha.py", line=30),
        _diag("tests/test_beta.py", rule="invalid-parameter-default", line=40),
    ]
    ceilings = {"tests/test_alpha.py": 2, "tests/test_beta.py": 1}

    assert check(report, ceilings) == []


def test_empty_ceilings_and_empty_report_pass() -> None:
    assert check([], {}) == []


def test_warning_in_unlisted_file_fails() -> None:
    failures = check(
        [_diag("scripts/build.py", rule="missing-attribute", line=42)],
        {"tests/test_alpha.py": 1},
    )

    assert any(
        "scripts/build.py: 1 warning(s) exceeds its ceiling of 0" in f for f in failures
    )


def test_warning_above_ceiling_fails() -> None:
    report = [
        _diag("tests/test_alpha.py", line=10),
        _diag("tests/test_alpha.py", line=20),
        _diag("tests/test_alpha.py", line=30),
    ]
    failures = check(report, {"tests/test_alpha.py": 2})

    assert len(failures) == 1
    assert "tests/test_alpha.py: 3 warning(s) exceeds its ceiling of 2" in failures[0]


def test_warning_below_ceiling_prompts_lowering() -> None:
    report = [_diag("tests/test_alpha.py", line=10)]
    failures = check(report, {"tests/test_alpha.py": 2})

    assert failures == [
        "tests/test_alpha.py: 1 warning(s) is below its ceiling of 2; "
        "lower it to 1 in [tool.sortify.ty-warning-ceilings]"
    ]


def test_zero_warnings_prompts_removing_ceiling() -> None:
    failures = check([], {"tests/test_alpha.py": 2})

    assert failures == [
        "tests/test_alpha.py: 0 warnings is below its ceiling of 2; "
        "remove it from [tool.sortify.ty-warning-ceilings]"
    ]


def test_multi_platform_upper_bound_accepts_sys_platform_branches() -> None:
    """Platform-specific warnings pass when <= ceiling on each OS and max == ceiling."""
    reports = {
        "linux": [],
        "darwin": [],
        "win32": [_diag("tests/test_win32_branch.py", line=18)],
    }
    ceilings = {"tests/test_win32_branch.py": 1}

    assert check(reports, ceilings) == []
    # Single-platform mode disables stale-ceiling ratchet so OS-gated ceilings pass
    assert check([], ceilings, enforce_ratchet=False) == []
    # If all 3 platforms drop to 0 warnings, multi-platform ratchet flags stale ceiling
    stale_failures = check({"linux": [], "darwin": [], "win32": []}, ceilings)
    assert len(stale_failures) == 1
    assert "0 warnings is below its ceiling of 1" in stale_failures[0]


def test_forbidden_source_roots_rejected_in_ceilings_and_baseline(
    tmp_path: Path,
) -> None:
    ceiling_failures = check(
        [],
        {
            "app/ui/tui.py": 1,
            "scripts/build.py": 1,
            ".agents/skills/run-sortify/driver.py": 1,
        },
    )
    assert len(ceiling_failures) == 3
    assert all("warning ceilings are forbidden" in f for f in ceiling_failures)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.ty]\nbaseline = "tests/ty_baseline.json"\n', encoding="utf-8"
    )
    baseline = tmp_path / "tests" / "ty_baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(
        json.dumps(
            {
                "version": 0,
                "files": {
                    "tests/test_legacy.py": [],
                    "app/ui/tui.py": [],
                    "scripts/check_ty_warnings.py": [],
                    ".agents/skills/run-sortify/driver.py": [],
                },
            }
        ),
        encoding="utf-8",
    )
    baseline_failures = check_baseline(pyproject, repo_root=tmp_path)
    assert len(baseline_failures) == 3
    assert all("baseline entries are forbidden" in f for f in baseline_failures)


def test_error_diagnostic_fails_immediately() -> None:
    report = [_diag("scripts/validate_architecture.py", severity="major", line=103)]
    failures = check(report, {})

    assert len(failures) == 1
    assert failures[0].startswith("error: scripts/validate_architecture.py:103:")


def test_missing_file_and_non_positive_ceiling_fail(tmp_path: Path) -> None:
    existing = tmp_path / "tests" / "present.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("x = 1\n", encoding="utf-8")

    failures = check(
        [],
        {"tests/missing.py": 2, "tests/present.py": 0},
        repo_root=tmp_path,
    )

    assert any("tests/missing.py: file does not exist" in f for f in failures)
    assert any("tests/present.py: invalid ceiling 0" in f for f in failures)


def test_windows_and_repo_prefixed_paths_normalize(tmp_path: Path) -> None:
    mod = tmp_path / "tests" / "ui" / "test_tui.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("x = 1\n", encoding="utf-8")

    report = [
        _diag("tests\\ui\\test_tui.py", line=10),
        _diag(str(mod.resolve()), line=20),
    ]
    assert check(report, {"tests/ui/test_tui.py": 2}, repo_root=tmp_path) == []


def test_pyproject_has_zero_app_warning_ceilings_and_clean_baseline() -> None:
    pyproject = REPO_ROOT / "pyproject.toml"
    ceilings = load_ceilings(pyproject)
    assert ceilings == {}, f"Expected zero warning ceilings, found {ceilings}"
    assert check_baseline(pyproject, repo_root=REPO_ROOT) == []


def test_main_report_modes_and_github_annotations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.sortify.ty-warning-ceilings]\n"tests/mod.py" = 1\n',
        encoding="utf-8",
    )
    mod = tmp_path / "tests" / "mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("x = 1\n", encoding="utf-8")

    passing = tmp_path / "pass.json"
    passing.write_text(json.dumps([_diag("tests/mod.py")]), encoding="utf-8")
    assert main(["prog", str(passing)], repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "All 1 ty warning ceilings matched" in out

    exceeding = tmp_path / "exceed.json"
    exceeding.write_text(
        json.dumps([_diag("tests/mod.py", line=1), _diag("tests/mod.py", line=2)]),
        encoding="utf-8",
    )
    assert (
        main(["prog", str(exceeding), "--github-annotations"], repo_root=tmp_path) == 1
    )
    out = capsys.readouterr().out
    assert "::error file=tests/mod.py,title=ty warning ceiling::" in out

    stale = tmp_path / "stale.json"
    stale.write_text("[]", encoding="utf-8")
    assert main(["prog", str(stale), "--github-annotations"], repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "::error file=pyproject.toml,title=ty warning ceiling::" in out


def test_parallel_platform_runner_invokes_all_platforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_platforms: list[str] = []

    def _fake_run(
        repo_root: Path, *, platform: str | None = None
    ) -> tuple[int, list[dict[str, Any]], str]:
        assert platform is not None
        seen_platforms.append(platform)
        return 0, [], ""

    monkeypatch.setattr("scripts.check_ty_warnings.run_ty_report", _fake_run)
    results = run_ty_reports_parallel(tmp_path)
    assert set(results.keys()) == set(PLATFORMS)
    assert set(seen_platforms) == set(PLATFORMS)


def test_main_cli_help_and_invalid_report_paths(tmp_path: Path) -> None:
    script_path = str(REPO_ROOT / "scripts" / "check_ty_warnings.py")

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


def test_head_passes_live_ty_warning_ratchet() -> None:
    assert main(["prog"]) == 0
    for platform in PLATFORMS:
        assert main(["prog", "--platform", platform]) == 0


def test_ty_rejects_invalid_parameter_default_probe(tmp_path: Path) -> None:
    """Verify ty fails on invalid-parameter-default and invalid IncrementalAnalyzer model_path types."""
    from scripts.check_ty_warnings import _ty_command

    probe = tmp_path / "probe_invalid_default.py"
    probe.write_text(
        "from app.core.analyzer import IncrementalAnalyzer\n\n"
        "def bad_default(x: str = None) -> str | None:\n"
        "    return x\n\n"
        "def bad_caller(db: object) -> None:\n"
        "    IncrementalAnalyzer(3, set(), db, model_path=123)\n",
        encoding="utf-8",
    )
    cmd = [arg for arg in _ty_command() if arg not in ("--output-format", "gitlab")]
    cmd.extend(["--project", str(REPO_ROOT), str(probe)])
    res = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    assert res.returncode == 1
    combined = f"{res.stdout}\n{res.stderr}"
    assert "invalid-parameter-default" in combined
    assert "invalid-argument-type" in combined
