"""Fail when a data-safety module's coverage drops below its floor.

The global ``--cov-fail-under`` gate lets coverage fall in the modules that
move, undo and encrypt user files as long as it rises elsewhere. This check
holds each of those modules to its own floor, read from
``[tool.sortify.coverage-floors]`` in pyproject.toml.

Usage: uv run python scripts/check_coverage_floors.py [coverage.json]
"""

import json
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_floors(pyproject: Path) -> dict[str, float]:
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    return config.get("tool", {}).get("sortify", {}).get("coverage-floors", {})


def check(report: dict, floors: dict[str, float]) -> list[str]:
    """Return one message per module below its floor or missing from the report."""
    files = {path.replace("\\", "/"): data for path, data in report["files"].items()}
    failures = []
    for module, floor in sorted(floors.items()):
        data = files.get(module)
        if data is None:
            failures.append(f"{module}: not in the coverage report (floor {floor}%)")
            continue
        covered = data["summary"]["percent_covered"]
        if covered < floor:
            failures.append(f"{module}: {covered:.1f}% is below its floor of {floor}%")
    return failures


def main(argv: list[str]) -> int:
    report_path = Path(argv[1]) if len(argv) > 1 else REPO_ROOT / "coverage.json"
    floors = load_floors(REPO_ROOT / "pyproject.toml")
    if not floors:
        print("No coverage floors configured.")
        return 0
    with open(report_path) as fh:
        report = json.load(fh)
    failures = check(report, floors)
    for module, floor in sorted(floors.items()):
        print(f"{module}: floor {floor}%")
    if failures:
        print("\nCoverage floors not met:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"\nAll {len(floors)} coverage floors met.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
