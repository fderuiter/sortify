import json
import subprocess
import sys
from pathlib import Path

from scripts.check_ty_warnings import REPO_ROOT, check, load_ceilings, main


def _diag(
    path: str,
    *,
    severity: str = "minor",
    rule: str = "missing-attribute",
    line: int = 10,
    col: int = 5,
) -> dict:
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


def test_matching_ceilings_and_clean_unlisted_files_pass():
    report = [
        _diag("app/ui/tui.py", line=12),
        _diag("app/ui/tui.py", line=30),
        _diag("app/config.py", rule="invalid-parameter-default", line=40),
    ]
    ceilings = {"app/ui/tui.py": 2, "app/config.py": 1}

    assert check(report, ceilings) == []


def test_empty_ceilings_and_empty_report_pass():
    assert check([], {}) == []


def test_warning_in_unlisted_file_fails():
    failures = check(
        [_diag("scripts/build.py", rule="missing-attribute", line=42)],
        {"app/ui/tui.py": 2},
    )

    assert any(
        "scripts/build.py: 1 warning(s) exceeds its ceiling of 0" in f for f in failures
    )


def test_warning_above_ceiling_fails():
    report = [
        _diag("app/ui/tui.py", line=10),
        _diag("app/ui/tui.py", line=20),
        _diag("app/ui/tui.py", line=30),
    ]
    failures = check(report, {"app/ui/tui.py": 2})

    assert len(failures) == 1
    assert "app/ui/tui.py: 3 warning(s) exceeds its ceiling of 2" in failures[0]


def test_warning_below_ceiling_prompts_lowering():
    report = [_diag("app/ui/tui.py", line=10)]
    failures = check(report, {"app/ui/tui.py": 2})

    assert failures == [
        "app/ui/tui.py: 1 warning(s) is below its ceiling of 2; "
        "lower it to 1 in [tool.sortify.ty-warning-ceilings]"
    ]


def test_zero_warnings_prompts_removing_ceiling():
    failures = check([], {"app/ui/tui.py": 2})

    assert failures == [
        "app/ui/tui.py: 0 warnings is below its ceiling of 2; "
        "remove it from [tool.sortify.ty-warning-ceilings]"
    ]


def test_error_diagnostic_fails_immediately():
    report = [_diag("scripts/validate_architecture.py", severity="major", line=103)]
    failures = check(report, {})

    assert len(failures) == 1
    assert failures[0].startswith("error: scripts/validate_architecture.py:103:")


def test_missing_file_and_non_positive_ceiling_fail(tmp_path: Path):
    existing = tmp_path / "app" / "present.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("x = 1\n", encoding="utf-8")

    failures = check(
        [],
        {"app/missing.py": 2, "app/present.py": 0},
        repo_root=tmp_path,
    )

    assert any("app/missing.py: file does not exist" in f for f in failures)
    assert any("app/present.py: invalid ceiling 0" in f for f in failures)


def test_windows_and_repo_prefixed_paths_normalize(tmp_path: Path):
    mod = tmp_path / "app" / "ui" / "tui.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("x = 1\n", encoding="utf-8")

    report = [
        _diag("app\\ui\\tui.py", line=10),
        _diag(str(mod.resolve()), line=20),
    ]
    assert check(report, {"app/ui/tui.py": 2}, repo_root=tmp_path) == []


def test_every_configured_ceiling_names_a_real_module():
    ceilings = load_ceilings(REPO_ROOT / "pyproject.toml")

    assert ceilings, "no ty warning ceilings configured"
    for module, limit in ceilings.items():
        assert (REPO_ROOT / module).is_file(), module
        assert limit > 0, f"{module} must have positive ceiling, got {limit}"


def test_main_report_modes_and_github_annotations(tmp_path: Path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.sortify.ty-warning-ceilings]\n"app/mod.py" = 1\n',
        encoding="utf-8",
    )
    mod = tmp_path / "app" / "mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("x = 1\n", encoding="utf-8")

    passing = tmp_path / "pass.json"
    passing.write_text(json.dumps([_diag("app/mod.py")]), encoding="utf-8")
    assert main(["prog", str(passing)], repo_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "All 1 ty warning ceilings matched" in out

    exceeding = tmp_path / "exceed.json"
    exceeding.write_text(
        json.dumps([_diag("app/mod.py", line=1), _diag("app/mod.py", line=2)]),
        encoding="utf-8",
    )
    assert (
        main(["prog", str(exceeding), "--github-annotations"], repo_root=tmp_path) == 1
    )
    out = capsys.readouterr().out
    assert "::error file=app/mod.py,title=ty warning ceiling::" in out

    stale = tmp_path / "stale.json"
    stale.write_text("[]", encoding="utf-8")
    assert main(["prog", str(stale), "--github-annotations"], repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "::error file=pyproject.toml,title=ty warning ceiling::" in out


def test_main_cli_help_and_invalid_report_paths(tmp_path: Path):
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


def test_head_passes_live_ty_warning_ratchet():
    assert main(["prog"]) == 0
    for platform in ("linux", "darwin", "win32"):
        assert main(["prog", "--platform", platform]) == 0
