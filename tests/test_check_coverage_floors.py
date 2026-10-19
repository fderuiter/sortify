import json

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


def test_main_exit_code_follows_the_check(tmp_path):
    floors = load_floors(REPO_ROOT / "pyproject.toml")
    passing = tmp_path / "pass.json"
    passing.write_text(json.dumps(_report(**{m: 100.0 for m in floors})))
    failing = tmp_path / "fail.json"
    failing.write_text(json.dumps(_report(**{m: 0.0 for m in floors})))

    assert main(["prog", str(passing)]) == 0
    assert main(["prog", str(failing)]) == 1
