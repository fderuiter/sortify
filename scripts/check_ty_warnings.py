"""Fail when ty reports errors, new warnings, forbidden suppressions, or stale ceilings.

Runs ``ty check`` concurrently across ``linux``, ``darwin``, and ``win32`` by
default and validates all configured source roots (``app``, ``scripts``,
``tests``, ``.agents``):

- Any error-level diagnostic on any platform fails immediately.
- Warning ceilings and baseline entries are forbidden for ``app/``, ``scripts/``,
  and ``.agents/``.
- Every platform enforces ``warning_count <= ceiling`` (with implicit ceiling 0
  for unlisted files), allowing valid ``sys.platform`` branches without
  requiring exact cross-OS warning count equality.
- Across a multi-platform run, if even the highest warning count across all
  checked platforms is below a configured ceiling, the stale ceiling fails so it
  is lowered or removed immediately.

Usage: uv run python scripts/check_ty_warnings.py [ty_report.json] [--platform PLATFORM] [--github-annotations]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
PLATFORMS: tuple[str, ...] = ("linux", "darwin", "win32")
WARNING_SEVERITIES = frozenset({"minor", "info"})
FORBIDDEN_PREFIXES: tuple[str, ...] = ("app/", "scripts/", ".agents/")
FORBIDDEN_ROOTS = frozenset({"app", "scripts", ".agents"})


def load_ceilings(pyproject: Path) -> dict[str, int]:
    """Load per-file ty warning ceilings from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    raw = config.get("tool", {}).get("sortify", {}).get("ty-warning-ceilings", {})
    return {path.replace("\\", "/"): int(limit) for path, limit in raw.items()}


def load_baseline_path(pyproject: Path) -> Path | None:
    """Return the configured [tool.ty].baseline path resolved against pyproject's directory."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    raw_baseline = config.get("tool", {}).get("ty", {}).get("baseline")
    if not isinstance(raw_baseline, str) or not raw_baseline.strip():
        return None
    candidate = Path(raw_baseline)
    if not candidate.is_absolute():
        candidate = pyproject.resolve().parent / candidate
    return candidate


def _normalize_path(raw_path: str, repo_root: Path) -> str:
    norm = raw_path.replace("\\", "/")
    root_prefix = repo_root.resolve().as_posix().rstrip("/") + "/"
    if norm.startswith(root_prefix):
        norm = norm[len(root_prefix) :]
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _is_forbidden_source_path(norm_path: str) -> bool:
    return norm_path in FORBIDDEN_ROOTS or norm_path.startswith(FORBIDDEN_PREFIXES)


def check_baseline(pyproject: Path, repo_root: Path | None = None) -> list[str]:
    """Validate that [tool.ty].baseline exists (if configured) and excludes app/, scripts/, and .agents/."""
    root = (repo_root or pyproject.resolve().parent).resolve()
    baseline_path = load_baseline_path(pyproject)
    if baseline_path is None:
        return []
    rel_baseline = _normalize_path(str(baseline_path), root)
    if not baseline_path.is_file():
        return [f"{rel_baseline}: configured [tool.ty].baseline file does not exist"]
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{rel_baseline}: failed to parse [tool.ty].baseline JSON ({exc})"]
    if not isinstance(payload, dict):
        return [f"{rel_baseline}: [tool.ty].baseline must be a JSON object"]

    files_obj = payload.get("files", payload)
    if not isinstance(files_obj, dict):
        return [f"{rel_baseline}: [tool.ty].baseline 'files' must be a JSON object"]

    failures: list[str] = []
    for raw_file in sorted(files_obj):
        if raw_file == "version" and files_obj is payload:
            continue
        norm_file = _normalize_path(str(raw_file), root)
        if _is_forbidden_source_path(norm_file):
            failures.append(
                f"{norm_file}: baseline entries are forbidden for app/, scripts/, "
                f"and .agents/ (found in {rel_baseline})"
            )
    return failures


def _format_diagnostic(item: dict[str, Any], norm_path: str) -> str:
    loc = item.get("location", {})
    begin = (loc.get("positions") or {}).get("begin") or {}
    line = begin.get("line", 1)
    col = begin.get("column", 1)
    rule = item.get("check_name", "ty")
    desc = item.get("description", rule)
    return f"{norm_path}:{line}:{col}: {desc}"


def check(
    report: list[dict[str, Any]] | Mapping[str, list[dict[str, Any]]],
    ceilings: dict[str, int],
    repo_root: Path | None = None,
    *,
    enforce_ratchet: bool = True,
) -> list[str]:
    """Return failure messages for errors, forbidden entries, ceiling breaches, or stale ceilings."""
    root = (repo_root or REPO_ROOT).resolve()
    normalized_ceilings = {
        _normalize_path(path, root): limit for path, limit in ceilings.items()
    }

    if isinstance(report, Mapping):
        platform_reports: dict[str, list[dict[str, Any]]] = {
            str(plat): list(items) for plat, items in report.items()
        }
        multi_platform = True
    else:
        platform_reports = {"default": list(report)}
        multi_platform = False

    failures: list[str] = []
    forbidden_ceiling_modules: set[str] = set()
    for module in sorted(normalized_ceilings):
        if _is_forbidden_source_path(module):
            forbidden_ceiling_modules.add(module)
            failures.append(
                f"{module}: warning ceilings are forbidden for app/, scripts/, "
                "and .agents/ in [tool.sortify.ty-warning-ceilings]"
            )

    counts_by_platform: dict[str, Counter[str]] = {}
    warnings_by_platform: dict[str, defaultdict[str, list[str]]] = {}
    errors_seen: dict[str, list[str]] = defaultdict(list)

    for plat_name, items in platform_reports.items():
        plat_counts: Counter[str] = Counter()
        plat_warnings: defaultdict[str, list[str]] = defaultdict(list)
        for item in items:
            loc = item.get("location") or {}
            raw_path = str(loc.get("path", "<unknown>"))
            norm_path = _normalize_path(raw_path, root)
            severity = str(item.get("severity", "major")).lower()
            formatted = _format_diagnostic(item, norm_path)
            if severity in WARNING_SEVERITIES:
                plat_counts[norm_path] += 1
                plat_warnings[norm_path].append(formatted)
            else:
                if plat_name not in errors_seen[formatted]:
                    errors_seen[formatted].append(plat_name)
        counts_by_platform[plat_name] = plat_counts
        warnings_by_platform[plat_name] = plat_warnings

    for formatted_err, err_plats in errors_seen.items():
        if multi_platform:
            plat_suffix = f" [{', '.join(err_plats)}]"
        else:
            plat_suffix = ""
        failures.append(f"error: {formatted_err}{plat_suffix}")

    all_warned_modules = sorted(
        {mod for counts in counts_by_platform.values() for mod in counts}
    )
    for module in all_warned_modules:
        if module in forbidden_ceiling_modules:
            ceiling = 0
        else:
            ceiling = normalized_ceilings.get(module, 0)

        exceeding_plats = [
            plat
            for plat, counts in counts_by_platform.items()
            if counts.get(module, 0) > ceiling
        ]
        if not exceeding_plats:
            continue

        worst_plat = max(
            exceeding_plats, key=lambda p: counts_by_platform[p].get(module, 0)
        )
        worst_count = counts_by_platform[worst_plat][module]
        details = "; ".join(warnings_by_platform[worst_plat][module][:5])
        plat_note = f" on {', '.join(exceeding_plats)}" if multi_platform else ""

        if module not in normalized_ceilings or module in forbidden_ceiling_modules:
            failures.append(
                f"{module}: {worst_count} warning(s){plat_note} exceeds its ceiling "
                f"of 0 (unlisted files must remain warning-free: {details})"
            )
        else:
            failures.append(
                f"{module}: {worst_count} warning(s){plat_note} exceeds its ceiling "
                f"of {ceiling} ({details})"
            )

    for module, ceiling in sorted(normalized_ceilings.items()):
        if module in forbidden_ceiling_modules:
            continue
        if ceiling <= 0:
            failures.append(
                f"{module}: invalid ceiling {ceiling}; remove clean files from "
                "[tool.sortify.ty-warning-ceilings]"
            )
            continue
        if repo_root is not None and not (root / module).is_file():
            failures.append(
                f"{module}: file does not exist (remove stale ceiling of {ceiling} "
                "from [tool.sortify.ty-warning-ceilings])"
            )
            continue
        if not enforce_ratchet or not platform_reports:
            continue

        max_count = max(
            (counts.get(module, 0) for counts in counts_by_platform.values()),
            default=0,
        )
        if max_count == 0:
            failures.append(
                f"{module}: 0 warnings is below its ceiling of {ceiling}; "
                "remove it from [tool.sortify.ty-warning-ceilings]"
            )
        elif max_count < ceiling:
            failures.append(
                f"{module}: {max_count} warning(s) is below its ceiling of {ceiling}; "
                f"lower it to {max_count} in [tool.sortify.ty-warning-ceilings]"
            )

    return failures


def _ty_command(platform: str | None = None) -> list[str]:
    venv_ty = Path(sys.executable).resolve().parent / (
        "ty.exe" if sys.platform == "win32" else "ty"
    )
    if venv_ty.is_file():
        cmd = [str(venv_ty), "check", "--output-format", "gitlab"]
    else:
        which_ty = shutil.which("ty")
        if which_ty:
            cmd = [which_ty, "check", "--output-format", "gitlab"]
        else:
            cmd = [sys.executable, "-m", "ty", "check", "--output-format", "gitlab"]
    if platform:
        cmd.extend(["--platform", platform])
    return cmd


def run_ty_report(
    repo_root: Path, *, platform: str | None = None
) -> tuple[int, list[dict[str, Any]] | None, str]:
    """Execute ty check in GitLab JSON format and parse the diagnostic list."""
    res = subprocess.run(
        _ty_command(platform=platform),
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    stdout = res.stdout.strip()
    if not stdout:
        if res.returncode != 0:
            return (
                res.returncode,
                None,
                res.stderr.strip() or f"ty check exited with code {res.returncode}",
            )
        return 0, [], ""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        err_detail = res.stderr.strip() or str(exc)
        return (
            res.returncode or 1,
            None,
            f"Failed to parse ty JSON output: {err_detail}",
        )
    if not isinstance(parsed, list):
        return (
            res.returncode or 1,
            None,
            "Unexpected ty JSON payload (expected a list of diagnostics).",
        )
    return res.returncode, parsed, res.stderr.strip()


def run_ty_reports_parallel(
    repo_root: Path, platforms: Sequence[str] = PLATFORMS
) -> dict[str, tuple[int, list[dict[str, Any]] | None, str]]:
    """Execute ty check concurrently across all target platforms."""
    target_platforms = tuple(platforms)
    if not target_platforms:
        return {}
    with ThreadPoolExecutor(max_workers=len(target_platforms)) as pool:
        futures = {
            plat: pool.submit(run_ty_report, repo_root, platform=plat)
            for plat in target_platforms
        }
        return {plat: futures[plat].result() for plat in target_platforms}


def _emit_github_annotations(failures: list[str]) -> None:
    for failure in failures:
        target_file = "pyproject.toml"
        if not failure.startswith("error: "):
            candidate = failure.split(":", 1)[0]
            if (
                "below its ceiling" not in failure
                and "does not exist" not in failure
                and "warning ceilings are forbidden" not in failure
                and "baseline entries are forbidden" not in failure
            ):
                target_file = candidate
        else:
            rest = failure[len("error: ") :]
            target_file = rest.split(":", 1)[0]
        clean_msg = failure.replace("\r", " ").replace("\n", " ")
        print(f"::error file={target_file},title=ty warning ceiling::{clean_msg}")


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when ty reports errors, new warnings, forbidden suppressions, "
            "or stale per-file warning ceilings across linux, darwin, and win32."
        ),
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=None,
        type=Path,
        help="Optional path to pre-generated ty GitLab JSON report",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help=(
            "Optional single target platform passed to ty check "
            "(defaults to checking linux, darwin, and win32 concurrently)"
        ),
    )
    parser.add_argument(
        "--github-annotations",
        action="store_true",
        help="Emit GitHub Actions ::error workflow annotations on failure",
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

    root = (repo_root or REPO_ROOT).resolve()
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        print(f"Error: pyproject.toml not found at {pyproject_path}", file=sys.stderr)
        return 1

    baseline_failures = check_baseline(pyproject_path, repo_root=root)

    ty_exits: list[int] = []
    enforce_ratchet = True
    report_data: list[dict[str, Any]] | dict[str, list[dict[str, Any]]]
    if args.report is not None:
        report_path: Path = args.report
        if not report_path.exists():
            print(
                f"Error: ty report does not exist: {report_path}",
                file=sys.stderr,
            )
            return 1
        if not report_path.is_file():
            print(
                f"Error: ty report is not a regular file: {report_path}",
                file=sys.stderr,
            )
            return 1
        with open(report_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if not isinstance(loaded, (list, dict)):
            print(
                f"Error: unexpected ty report payload in {report_path}",
                file=sys.stderr,
            )
            return 1
        report_data = loaded
        check_root: Path | None = repo_root
    elif args.platform is not None:
        ty_exit, maybe_report, err_text = run_ty_report(root, platform=args.platform)
        if maybe_report is None:
            print(f"Error running ty check: {err_text}", file=sys.stderr)
            return 1
        ty_exits.append(ty_exit)
        report_data = maybe_report
        check_root = root
        enforce_ratchet = False
    else:
        parallel_results = run_ty_reports_parallel(root, PLATFORMS)
        platform_reports: dict[str, list[dict[str, Any]]] = {}
        for plat, (plat_exit, maybe_report, err_text) in parallel_results.items():
            if maybe_report is None:
                print(
                    f"Error running ty check (--platform {plat}): {err_text}",
                    file=sys.stderr,
                )
                return 1
            ty_exits.append(plat_exit)
            platform_reports[plat] = maybe_report
        report_data = platform_reports
        check_root = root

    ceilings = load_ceilings(pyproject_path)
    failures = baseline_failures + check(
        report_data,
        ceilings,
        repo_root=check_root,
        enforce_ratchet=enforce_ratchet,
    )
    nonzero_exits = [code for code in ty_exits if code != 0]
    if nonzero_exits and not failures:
        failures.append(f"ty check exited with non-zero status {nonzero_exits[0]}")

    for module, ceiling in sorted(ceilings.items()):
        print(f"{module}: ceiling {ceiling}")

    if failures:
        if args.github_annotations:
            _emit_github_annotations(failures)
        print("\nty warning ratchet failed:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    if isinstance(report_data, Mapping):
        max_warnings = max(
            (
                sum(
                    1
                    for item in items
                    if str(item.get("severity", "major")).lower() in WARNING_SEVERITIES
                )
                for items in report_data.values()
            ),
            default=0,
        )
        platforms_label = ", ".join(report_data.keys())
        print(
            f"\nAll {len(ceilings)} ty warning ceilings matched across "
            f"{platforms_label} (max {max_warnings} warnings, 0 errors)."
        )
    else:
        total_warnings = sum(
            1
            for item in report_data
            if str(item.get("severity", "major")).lower() in WARNING_SEVERITIES
        )
        print(
            f"\nAll {len(ceilings)} ty warning ceilings matched "
            f"({total_warnings} warnings, 0 errors)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
