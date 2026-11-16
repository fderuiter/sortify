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
    passing.write_text(json.dumps(_report(**{m: 100.0 for m in floors})))
    failing = tmp_path / "fail.json"
    failing.write_text(json.dumps(_report(**{m: 0.0 for m in floors})))

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
