"""Fail when a data-safety module's coverage drops below its floor.

The global ``--cov-fail-under`` gate lets coverage fall in the modules that
move, undo and encrypt user files as long as it rises elsewhere. This check
holds each of those modules to its own floor, read from
``[tool.sortify.coverage-floors]`` in pyproject.toml.

Usage: uv run python scripts/check_coverage_floors.py [coverage.json]
"""

import argparse
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a data-safety module's coverage drops below its floor.",
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=REPO_ROOT / "coverage.json",
        type=Path,
        help="Path to coverage JSON report (default: coverage.json)",
    )

    if argv is None:
        args_list = sys.argv[1:]
    elif argv and (
        argv[0] == "prog" or argv[0] == sys.argv[0] or argv[0].endswith(".py")
    ):
        args_list = argv[1:]
    else:
        args_list = argv

    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0

    report_path = args.report
    if not report_path.exists():
        print(f"Error: Coverage report does not exist: {report_path}", file=sys.stderr)
        return 1
    if not report_path.is_file():
        print(
            f"Error: Coverage report is not a regular file: {report_path}",
            file=sys.stderr,
        )
        return 1

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
