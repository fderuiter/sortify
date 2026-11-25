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
    import tomllib

    pyproject = REPO_ROOT / "pyproject.toml"
    ceilings = load_ceilings(pyproject)
    assert ceilings == {}, f"Expected zero warning ceilings, found {ceilings}"
    assert check_baseline(pyproject, repo_root=REPO_ROOT) == []

    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    ty_cfg = config.get("tool", {}).get("ty", {})
    assert "baseline" not in ty_cfg
    assert "tests" not in ty_cfg.get("src", {}).get("include", [])


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


@pytest.mark.slow
def test_head_passes_live_ty_warning_ratchet() -> None:
    assert main(["prog"]) == 0


def test_main_platform_and_parallel_dispatch_with_mocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.sortify.ty-warning-ceilings]\n"tests/win_only.py" = 1\n',
        encoding="utf-8",
    )
    win_mod = tmp_path / "tests" / "win_only.py"
    win_mod.parent.mkdir(parents=True)
    win_mod.write_text("x = 1\n", encoding="utf-8")

    dispatched: list[tuple[Path, str | None]] = []

    def _fake_run_ty_report(
        repo_root: Path,
        *,
        platform: str | None = None,
        paths: list[str] | None = None,
    ) -> tuple[int, list[dict[str, Any]] | None, str]:
        assert paths is None
        dispatched.append((repo_root, platform))
        if platform == "win32":
            return 0, [_diag("tests/win_only.py")], ""
        return 0, [], ""

    monkeypatch.setattr("scripts.check_ty_warnings.run_ty_report", _fake_run_ty_report)

    # Single-platform mode invokes run_ty_report once and disables stale-ceiling ratchet
    for plat in PLATFORMS:
        assert main(["prog", "--platform", plat], repo_root=tmp_path) == 0
    assert [plat for _, plat in dispatched] == list(PLATFORMS)
    capsys.readouterr()

    # Default multi-platform mode dispatches via run_ty_reports_parallel and enforces ratchet
    parallel_calls: list[tuple[Path, tuple[str, ...]]] = []

    def _fake_parallel(
        repo_root: Path,
        platforms: tuple[str, ...] = PLATFORMS,
        *,
        paths: list[str] | None = None,
    ) -> dict[str, tuple[int, list[dict[str, Any]] | None, str]]:
        assert paths is None
        parallel_calls.append((repo_root, tuple(platforms)))
        return {
            "linux": (0, [], ""),
            "darwin": (0, [], ""),
            "win32": (0, [_diag("tests/win_only.py")], ""),
        }

    monkeypatch.setattr(
        "scripts.check_ty_warnings.run_ty_reports_parallel", _fake_parallel
    )
    assert main(["prog"], repo_root=tmp_path) == 0
    assert parallel_calls == [(tmp_path.resolve(), tuple(PLATFORMS))]
    capsys.readouterr()

    # Error handling when run_ty_report or run_ty_reports_parallel returns None
    monkeypatch.setattr(
        "scripts.check_ty_warnings.run_ty_report",
        lambda repo_root, *, platform=None, paths=None: (2, None, "boom single"),
    )
    assert main(["prog", "--platform", "linux"], repo_root=tmp_path) == 1
    assert "Error running ty check: boom single" in capsys.readouterr().err

    monkeypatch.setattr(
        "scripts.check_ty_warnings.run_ty_reports_parallel",
        lambda repo_root, platforms=PLATFORMS, *, paths=None: {
            "linux": (0, [], ""),
            "darwin": (2, None, "boom darwin"),
            "win32": (0, [], ""),
        },
    )
    assert main(["prog"], repo_root=tmp_path) == 1
    assert "Error running ty check (--platform darwin): boom darwin" in (
        capsys.readouterr().err
    )


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


def test_sandbox_cli_forbidden_in_ceilings_and_baseline(tmp_path: Path) -> None:
    from scripts.check_ty_warnings import FORBIDDEN_ROOTS

    assert "sandbox_cli.py" in FORBIDDEN_ROOTS
    ceiling_failures = check([], {"sandbox_cli.py": 1})
    assert len(ceiling_failures) == 1
    assert "warning ceilings are forbidden" in ceiling_failures[0]

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.ty.environment]\npython-version = '3.12'\n", encoding="utf-8"
    )
    baseline = tmp_path / "tests" / "ty_baseline.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "sandbox_cli.py": {"missing-attribute": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    baseline_failures = check_baseline(pyproject, repo_root=tmp_path)
    assert len(baseline_failures) == 1
    assert "baseline entries are forbidden" in baseline_failures[0]


def test_rule_ceiling_baseline_ignores_column_shifts_and_ratchets_stale_entries(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "test_sample.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("x = 1\n", encoding="utf-8")

    baseline_ceilings = {"tests/test_sample.py": {"missing-union-attribute": 2}}

    # Column shifts (+4 spaces or different lines) still pass
    shifted_reports = {
        "linux": [
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=50,
                col=16,
            ),
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=51,
                col=16,
            ),
        ],
        "darwin": [
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=50,
                col=16,
            ),
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=51,
                col=16,
            ),
        ],
        "win32": [
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=50,
                col=16,
            ),
        ],
    }
    assert (
        check(
            shifted_reports,
            {},
            repo_root=tmp_path,
            baseline_ceilings=baseline_ceilings,
        )
        == []
    )

    # Scoped or single-OS run skips staleness ratchet on untouched files and OS branches
    assert (
        check(
            {"linux": [], "darwin": [], "win32": []},
            {},
            repo_root=tmp_path,
            baseline_ceilings=baseline_ceilings,
            enforce_ratchet=False,
        )
        == []
    )

    # Full 3-OS run catches stale baseline ceiling when max count drops below ceiling
    stale = check(
        {
            "linux": [
                _diag(
                    "tests/test_sample.py",
                    severity="major",
                    rule="missing-union-attribute",
                )
            ],
            "darwin": [],
            "win32": [],
        },
        {},
        repo_root=tmp_path,
        baseline_ceilings=baseline_ceilings,
        enforce_ratchet=True,
    )
    assert len(stale) == 1
    assert "1 diagnostic(s) is below its baseline ceiling of 2" in stale[0]
    assert "--sync-baseline" in stale[0]

    # Exceeding ceiling or introducing a new rule fails immediately
    exceeded = check(
        [
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=1,
            ),
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=2,
            ),
            _diag(
                "tests/test_sample.py",
                severity="major",
                rule="missing-union-attribute",
                line=3,
            ),
        ],
        {},
        repo_root=tmp_path,
        baseline_ceilings=baseline_ceilings,
    )
    assert len(exceeded) == 1
    assert "exceeds its baseline ceiling of 2" in exceeded[0]


def test_sync_baseline_removes_stale_entries_and_is_platform_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.ty.environment]\npython-version = '3.12'\n", encoding="utf-8"
    )
    mod_a = tmp_path / "tests" / "test_a.py"
    mod_a.parent.mkdir(parents=True)
    mod_a.write_text("a = 1\n", encoding="utf-8")

    # Pre-seed stale baseline entry
    baseline = tmp_path / "tests" / "ty_baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "tests/test_a.py": {"missing-attribute": 5, "stale-rule": 2},
                    "tests/deleted_test.py": {"missing-attribute": 1},
                },
            }
        ),
        encoding="utf-8",
    )

    fake_by_plat = {
        "linux": (
            0,
            [_diag("tests/test_a.py", severity="major", rule="missing-attribute")],
            "",
        ),
        "darwin": (
            0,
            [_diag("tests/test_a.py", severity="major", rule="missing-attribute")],
            "",
        ),
        "win32": (
            0,
            [
                _diag(
                    "tests/test_a.py",
                    severity="major",
                    rule="missing-attribute",
                    line=10,
                ),
                _diag(
                    "tests/test_a.py",
                    severity="major",
                    rule="missing-attribute",
                    line=20,
                ),
                _diag(
                    "tests/test_a.py",
                    severity="major",
                    rule="invalid-assignment",
                    line=30,
                ),
            ],
            "",
        ),
    }
    monkeypatch.setattr(
        "scripts.check_ty_warnings.run_ty_reports_parallel",
        lambda _root, _plats=PLATFORMS, *, paths=None: dict(fake_by_plat),
    )

    assert main(["prog", "--sync-baseline"], repo_root=tmp_path) == 0
    first_bytes = baseline.read_bytes()
    parsed = json.loads(first_bytes.decode("utf-8"))
    assert parsed == {
        "version": 1,
        "files": {
            "tests/test_a.py": {
                "invalid-assignment": 1,
                "missing-attribute": 2,
            }
        },
    }

    # Re-running with reversed platform key order writes byte-for-byte identical JSON
    reversed_by_plat = {k: fake_by_plat[k] for k in reversed(list(fake_by_plat))}
    monkeypatch.setattr(
        "scripts.check_ty_warnings.run_ty_reports_parallel",
        lambda _root, _plats=PLATFORMS, *, paths=None: reversed_by_plat,
    )
    assert main(["prog", "--sync-baseline"], repo_root=tmp_path) == 0
    assert baseline.read_bytes() == first_bytes


def test_scoped_cli_mode_vs_config_triggers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.ty.environment]\npython-version = '3.12'\n", encoding="utf-8"
    )
    sandbox = tmp_path / "sandbox_cli.py"
    sandbox.write_text("x = 1\n", encoding="utf-8")

    recorded_paths: list[tuple[str, ...] | None] = []

    def _fake_parallel(
        _root: Path,
        platforms: tuple[str, ...] = PLATFORMS,
        *,
        paths: list[str] | None = None,
    ) -> dict[str, tuple[int, list[dict[str, Any]], str]]:
        recorded_paths.append(tuple(paths) if paths is not None else None)
        return {plat: (0, [], "") for plat in platforms}

    monkeypatch.setattr(
        "scripts.check_ty_warnings.run_ty_reports_parallel", _fake_parallel
    )

    assert main(["prog", "sandbox_cli.py"], repo_root=tmp_path) == 0
    assert recorded_paths[-1] == ("sandbox_cli.py",)

    for cfg_file in ("pyproject.toml", "uv.lock", "tests/ty_baseline.json"):
        assert main(["prog", "sandbox_cli.py", cfg_file], repo_root=tmp_path) == 0
        assert recorded_paths[-1] is None


def test_live_scoped_checks_pass_across_all_platforms_for_os_branch_tests() -> None:
    assert main(["prog", "sandbox_cli.py"]) == 0
    assert main(["prog", "tests/test_env_helper.py", "tests/test_ipc.py"]) == 0
