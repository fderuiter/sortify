"""Fail when ty reports errors, new warnings, or stale per-file warning ceilings.

``[tool.ty.terminal]`` sets ``error-on-warning = false`` while legacy warnings
in ``app/`` are paid down (#740). This guardrail holds every unlisted module to
zero warnings and enforces shrink-only per-file ceilings from
``[tool.sortify.ty-warning-ceilings]`` in pyproject.toml:

- Any error-level diagnostic fails immediately.
- Any warning in an unlisted file or above a configured ceiling fails.
- Any configured ceiling above its file's actual warning count fails so
  resolved warnings ratify a lower (or removed) ceiling immediately.

Usage: uv run python scripts/check_ty_warnings.py [ty_report.json] [--platform PLATFORM] [--github-annotations]
"""

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
WARNING_SEVERITIES = frozenset({"minor", "info"})


def load_ceilings(pyproject: Path) -> dict[str, int]:
    """Load per-file ty warning ceilings from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    raw = config.get("tool", {}).get("sortify", {}).get("ty-warning-ceilings", {})
    return {path.replace("\\", "/"): int(limit) for path, limit in raw.items()}


def _normalize_path(raw_path: str, repo_root: Path) -> str:
    norm = raw_path.replace("\\", "/")
    root_prefix = repo_root.resolve().as_posix().rstrip("/") + "/"
    if norm.startswith(root_prefix):
        norm = norm[len(root_prefix) :]
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _format_diagnostic(item: dict[str, Any], norm_path: str) -> str:
    loc = item.get("location", {})
    begin = (loc.get("positions") or {}).get("begin") or {}
    line = begin.get("line", 1)
    col = begin.get("column", 1)
    rule = item.get("check_name", "ty")
    desc = item.get("description", rule)
    return f"{norm_path}:{line}:{col}: {desc}"


def check(
    report: list[dict[str, Any]],
    ceilings: dict[str, int],
    repo_root: Path | None = None,
) -> list[str]:
    """Return failure messages for errors, ceiling breaches, or stale ceilings."""
    root = (repo_root or REPO_ROOT).resolve()
    normalized_ceilings = {
        _normalize_path(path, root): limit for path, limit in ceilings.items()
    }

    warning_counts: Counter[str] = Counter()
    warnings_by_file: defaultdict[str, list[str]] = defaultdict(list)
    failures: list[str] = []

    for item in report:
        loc = item.get("location") or {}
        raw_path = str(loc.get("path", "<unknown>"))
        norm_path = _normalize_path(raw_path, root)
        severity = str(item.get("severity", "major")).lower()
        if severity in WARNING_SEVERITIES:
            warning_counts[norm_path] += 1
            warnings_by_file[norm_path].append(_format_diagnostic(item, norm_path))
        else:
            failures.append(f"error: {_format_diagnostic(item, norm_path)}")

    for module in sorted(warning_counts):
        count = warning_counts[module]
        if module not in normalized_ceilings:
            details = "; ".join(warnings_by_file[module][:5])
            failures.append(
                f"{module}: {count} warning(s) exceeds its ceiling of 0 "
                f"(unlisted files must remain warning-free: {details})"
            )
        elif count > normalized_ceilings[module]:
            ceiling = normalized_ceilings[module]
            details = "; ".join(warnings_by_file[module][:5])
            failures.append(
                f"{module}: {count} warning(s) exceeds its ceiling of {ceiling} "
                f"({details})"
            )

    for module, ceiling in sorted(normalized_ceilings.items()):
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
        count = warning_counts.get(module, 0)
        if count == 0:
            failures.append(
                f"{module}: 0 warnings is below its ceiling of {ceiling}; "
                "remove it from [tool.sortify.ty-warning-ceilings]"
            )
        elif count < ceiling:
            failures.append(
                f"{module}: {count} warning(s) is below its ceiling of {ceiling}; "
                f"lower it to {count} in [tool.sortify.ty-warning-ceilings]"
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


def _emit_github_annotations(failures: list[str]) -> None:
    for failure in failures:
        target_file = "pyproject.toml"
        if not failure.startswith("error: "):
            candidate = failure.split(":", 1)[0]
            if "below its ceiling" not in failure and "does not exist" not in failure:
                target_file = candidate
        else:
            rest = failure[len("error: ") :]
            target_file = rest.split(":", 1)[0]
        clean_msg = failure.replace("\r", " ").replace("\n", " ")
        print(f"::error file={target_file},title=ty warning ceiling::{clean_msg}")


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when ty reports errors, new warnings, or stale per-file "
            "warning ceilings."
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
        help="Optional target platform passed to ty check (e.g. linux, darwin, win32)",
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

    ty_exit = 0
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
            report = json.load(fh)
        check_root: Path | None = repo_root
    else:
        ty_exit, maybe_report, err_text = run_ty_report(root, platform=args.platform)
        if maybe_report is None:
            print(f"Error running ty check: {err_text}", file=sys.stderr)
            return 1
        report = maybe_report
        check_root = root

    ceilings = load_ceilings(pyproject_path)
    failures = check(report, ceilings, repo_root=check_root)
    if ty_exit != 0 and not failures:
        failures.append(f"ty check exited with non-zero status {ty_exit}")

    for module, ceiling in sorted(ceilings.items()):
        print(f"{module}: ceiling {ceiling}")

    if failures:
        if args.github_annotations:
            _emit_github_annotations(failures)
        print("\nty warning ratchet failed:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    total_warnings = sum(
        1
        for item in report
        if str(item.get("severity", "major")).lower() in WARNING_SEVERITIES
    )
    print(
        f"\nAll {len(ceilings)} ty warning ceilings matched "
        f"({total_warnings} warnings, 0 errors)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
