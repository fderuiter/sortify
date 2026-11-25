import json
import subprocess
import sys

from scripts.check_coverage_floors import REPO_ROOT, check, load_floors, main


def _report(**percents):
    return {
        "files": {
            path: {"summary": {"percent_covered": pct}}
            for path, pct in percents.items()
        }
    }


def test_module_at_or_above_its_floor_passes():
    report = _report(**{"app/core/mover.py": 74.0, "app/core/ledger.py": 90.0})

    assert check(report, {"app/core/mover.py": 74, "app/core/ledger.py": 84}) == []


def test_module_below_its_floor_fails():
    failures = check(_report(**{"app/core/mover.py": 73.9}), {"app/core/mover.py": 74})

    assert failures == ["app/core/mover.py: 73.9% is below its floor of 74%"]


def test_module_missing_from_the_report_fails():
    failures = check(_report(), {"app/core/mover.py": 74})

    assert failures == ["app/core/mover.py: not in the coverage report (floor 74%)"]


def test_windows_report_paths_match():
    report = _report(**{"app\\core\\mover.py": 80.0})

    assert check(report, {"app/core/mover.py": 74}) == []


def test_every_configured_floor_names_a_real_module():
    floors = load_floors(REPO_ROOT / "pyproject.toml")

    assert floors, "no floors configured"
    for module in floors:
        assert (REPO_ROOT / module).is_file(), module


def test_main_exit_code_follows_the_check(tmp_path, capsys):
    floors = load_floors(REPO_ROOT / "pyproject.toml")
    passing = tmp_path / "pass.json"
    passing.write_text(json.dumps(_report(**dict.fromkeys(floors, 100.0))))
    failing = tmp_path / "fail.json"
    failing.write_text(json.dumps(_report(**dict.fromkeys(floors, 0.0))))

    assert main(["prog", str(passing)]) == 0
    assert main(["prog", str(failing)]) == 1

    missing = tmp_path / "missing.json"
    assert main(["prog", str(missing)]) == 1
    err = capsys.readouterr().err
    assert "Error:" in err and "does not exist" in err

    assert main(["prog", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "Error:" in err and "not a regular file" in err

    assert main(["prog", "--help"]) == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()


def test_main_missing_file_reports_error(tmp_path, capsys):
    missing = tmp_path / "nonexistent.json"
    assert main(["prog", str(missing)]) == 1
    captured = capsys.readouterr()
    assert f"Error: Coverage report does not exist: {missing}" in captured.err


def test_main_directory_path_reports_error(tmp_path, capsys):
    assert main(["prog", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"Error: Coverage report is not a regular file: {tmp_path}" in captured.err


def test_main_cli_help(capsys):
    assert main(["prog", "--help"]) == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()
    assert "report" in captured.out.lower()

    assert main(["prog", "-h"]) == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out.lower()


def test_cli_subprocess_help_and_errors(tmp_path):
    script_path = str(REPO_ROOT / "scripts" / "check_coverage_floors.py")

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


def test_only_measured_skips_missing_and_zero_executed_modules():
    from scripts.check_coverage_floors import check

    partial_report = {
        "files": {
            "app/core/history.py": {
                "executed_lines": [1, 2, 3],
                "summary": {"percent_covered": 88.0, "num_statements": 10},
            },
            "app/core/mover.py": {
                "executed_lines": [],
                "summary": {"percent_covered": 0.0, "num_statements": 50},
            },
        }
    }
    floors = {
        "app/core/history.py": 84,
        "app/core/mover.py": 74,
        "app/core/crypto.py": 86,
    }
    assert check(partial_report, floors, only_measured=True) == []
    assert len(check(partial_report, floors, only_measured=False)) == 2


def test_max_floor_lag_flags_stale_floors(tmp_path):
    from scripts.check_coverage_floors import check, main

    floors = {"app/core/mover.py": 74, "app/core/history.py": 84}
    report_within = _report(**{"app/core/mover.py": 84.0, "app/core/history.py": 90.0})
    assert check(report_within, floors, max_floor_lag=10) == []

    report_lagging = _report(**{"app/core/mover.py": 84.1, "app/core/history.py": 90.0})
    failures = check(report_lagging, floors, max_floor_lag=10)
    assert len(failures) == 1
    assert "app/core/mover.py" in failures[0]
    assert "exceeds its floor of 74% by 10.1 points" in failures[0]

    # CLI check with negative --max-floor-lag fails closed
    report_file = tmp_path / "cov.json"
    report_file.write_text(json.dumps(report_within), encoding="utf-8")
    assert main(["prog", "--max-floor-lag=-1", str(report_file)]) == 1


def test_check_protected_sync_and_exemptions(tmp_path):
    from scripts.check_coverage_floors import (
        EXEMPT_PROTECTED_MODULES,
        check_protected_sync,
        load_floors,
        main,
    )

    assert EXEMPT_PROTECTED_MODULES == frozenset(
        {"app/core/session.py", "app/core/db_worker.py"}
    )

    # Repository pyproject.toml and PROTECTED_PATHS must stay in sync
    repo_floors = load_floors(REPO_ROOT / "pyproject.toml")
    assert "app/core/moves.py" in repo_floors
    assert "app/core/store.py" in repo_floors
    assert repo_floors["app/core/history.py"] >= 84
    assert check_protected_sync(repo_floors) == []

    # Synthetic missing protected core module must fail sync
    incomplete_floors = {
        k: v for k, v in repo_floors.items() if k != "app/core/store.py"
    }
    sync_errs = check_protected_sync(incomplete_floors)
    assert len(sync_errs) == 1
    assert "app/core/store.py" in sync_errs[0]

    # End-to-end CLI check with --only-measured --max-floor-lag=10 --check-protected-sync
    measured = {mod: min(100.0, float(fl) + 5.0) for mod, fl in repo_floors.items()}
    cov_json = tmp_path / "measured.json"
    cov_json.write_text(json.dumps(_report(**measured)), encoding="utf-8")
    assert (
        main(
            [
                "prog",
                "--only-measured",
                "--max-floor-lag=10",
                "--check-protected-sync",
                str(cov_json),
            ]
        )
        == 0
    )
