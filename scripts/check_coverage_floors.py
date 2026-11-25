"""Fail when a data-safety module's coverage drops below its floor or lags behind.

The global ``--cov-fail-under`` gate lets coverage fall in the modules that
move, undo and encrypt user files as long as it rises elsewhere. This check
holds each of those modules to its own floor, read from
``[tool.sortify.coverage-floors]`` in pyproject.toml, and optionally verifies
floor lag bounds and synchronization with ``PROTECTED_PATHS``.

Usage: uv run python scripts/check_coverage_floors.py [options] [coverage.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Orchestration-only protected core modules explicitly exempt from per-module
# unit-coverage floor sync.
EXEMPT_PROTECTED_MODULES: frozenset[str] = frozenset(
    {
        "app/core/session.py",
        "app/core/db_worker.py",
    }
)


def load_floors(pyproject: Path) -> dict[str, float]:
    """Load configured per-module coverage floors from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    return config.get("tool", {}).get("sortify", {}).get("coverage-floors", {})


def load_exempt_protected_modules(pyproject: Path) -> frozenset[str]:
    """Load optional protected-module floor exemptions from pyproject.toml or return defaults."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    configured = (
        config.get("tool", {}).get("sortify", {}).get("coverage-floor-exemptions")
    )
    if isinstance(configured, list):
        return frozenset(str(p).replace("\\", "/") for p in configured)
    return EXEMPT_PROTECTED_MODULES


def _is_unmeasured_entry(data: dict | None) -> bool:
    """Return True when a module was not measured in a partial coverage run."""
    if data is None:
        return True
    if "executed_lines" in data and len(data["executed_lines"]) == 0:
        return True
    summary = data.get("summary", {})
    if "num_statements" in summary and summary.get("num_statements", 0) == 0:
        return True
    return False


def check(
    report: dict,
    floors: Mapping[str, float],
    *,
    only_measured: bool = False,
    max_floor_lag: float | None = None,
) -> list[str]:
    """Return failure messages for modules below floor, missing, or exceeding max_floor_lag."""
    files = {path.replace("\\", "/"): data for path, data in report["files"].items()}
    failures: list[str] = []
    for module, floor in sorted(floors.items()):
        data = files.get(module)
        if only_measured and _is_unmeasured_entry(data):
            continue
        if data is None:
            failures.append(f"{module}: not in the coverage report (floor {floor}%)")
            continue
        covered = data["summary"]["percent_covered"]
        if covered < floor:
            failures.append(f"{module}: {covered:.1f}% is below its floor of {floor}%")
        elif max_floor_lag is not None and (covered - floor) > max_floor_lag:
            lag = covered - floor
            failures.append(
                f"{module}: {covered:.1f}% exceeds its floor of {floor}% by "
                f"{lag:.1f} points (max lag {max_floor_lag:g}%); raise its floor in pyproject.toml"
            )
    return failures


def check_protected_sync(
    floors: Mapping[str, float],
    protected_paths: Iterable[str] | None = None,
    exempt_modules: Iterable[str] = EXEMPT_PROTECTED_MODULES,
) -> list[str]:
    """Verify every non-exempt app/core/*.py in PROTECTED_PATHS has a coverage floor."""
    if protected_paths is None:
        from scripts.classify_pr import PROTECTED_PATHS

        protected_paths = PROTECTED_PATHS

    exempt_set = {p.replace("\\", "/") for p in exempt_modules}
    normalized_floors = {k.replace("\\", "/") for k in floors}
    protected_core = sorted(
        {
            norm
            for raw in protected_paths
            for norm in (raw.replace("\\", "/"),)
            if norm.startswith("app/core/") and norm.endswith(".py")
        }
    )

    failures: list[str] = []
    for mod in protected_core:
        if mod in exempt_set:
            continue
        if mod not in normalized_floors:
            failures.append(
                f"{mod}: listed in PROTECTED_PATHS but missing from [tool.sortify.coverage-floors]"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for coverage floor and ratchet enforcement."""
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
    parser.add_argument(
        "--only-measured",
        action="store_true",
        help="Evaluate only modules measured in the provided coverage JSON report.",
    )
    parser.add_argument(
        "--max-floor-lag",
        type=float,
        default=None,
        help="Fail when actual coverage exceeds configured floor by more than N percentage points.",
    )
    parser.add_argument(
        "--check-protected-sync",
        action="store_true",
        help="Verify non-exempt app/core/*.py entries in PROTECTED_PATHS declare coverage floors.",
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

    if args.max_floor_lag is not None and args.max_floor_lag < 0:
        print("Error: --max-floor-lag must be non-negative.", file=sys.stderr)
        return 1

    pyproject_path = REPO_ROOT / "pyproject.toml"
    floors = load_floors(pyproject_path)
    failures: list[str] = []

    if args.check_protected_sync:
        exempt = load_exempt_protected_modules(pyproject_path)
        failures.extend(check_protected_sync(floors, exempt_modules=exempt))

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

    if not floors:
        if failures:
            print("\nCoverage floor synchronization errors:")
            for failure in failures:
                print(f"  {failure}")
            return 1
        print("No coverage floors configured.")
        return 0

    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)
    failures.extend(
        check(
            report,
            floors,
            only_measured=args.only_measured,
            max_floor_lag=args.max_floor_lag,
        )
    )
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
