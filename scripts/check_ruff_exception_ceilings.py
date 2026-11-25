"""Fail when Ruff reports unratcheted BLE001/S110/SIM105 violations, wildcards, or stale ceilings.

Runs ``ruff check --isolated --select BLE001,S110,SIM105 --output-format json``
across ``app``, ``scripts``, and ``.agents`` and validates both
``[tool.ruff.lint.per-file-ignores]`` and ``[tool.sortify.ruff-exception-ceilings]``:

- Wildcard ignores for ``BLE001``, ``S110``, or ``SIM105`` outside ``tests/`` fail.
- Per-file exception ignores on missing or clean/unratcheted files fail.
- Unlisted files have an implicit ceiling of ``0`` violations.
- Any file whose violation count exceeds its ceiling fails immediately.
- Any file whose violation count drops below its ceiling (or reaches ``0``)
  fails so the ceiling is lowered or removed immediately.

Usage: uv run python scripts/check_ruff_exception_ceilings.py [ruff_report.json] [--github-annotations]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOTS: tuple[str, ...] = ("app", "scripts", ".agents")
EXCEPTION_RULES: tuple[str, ...] = ("BLE001", "S110", "SIM105")
EXCEPTION_RULE_SET = frozenset(EXCEPTION_RULES)
WILDCARD_CHARS = frozenset({"*", "?", "["})


def _normalize_path(raw_path: str, repo_root: Path) -> str:
    norm = raw_path.replace("\\", "/")
    root_prefix = repo_root.resolve().as_posix().rstrip("/") + "/"
    if norm.startswith(root_prefix):
        norm = norm[len(root_prefix) :]
    elif len(norm) >= 2 and norm[1] == ":":
        lower_norm = norm.lower()
        lower_prefix = root_prefix.lower()
        if lower_norm.startswith(lower_prefix):
            norm = norm[len(root_prefix) :]
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _is_tests_path(norm_path: str) -> bool:
    return norm_path == "tests" or norm_path.startswith("tests/")


def _is_wildcard_pattern(pattern: str) -> bool:
    return any(ch in WILDCARD_CHARS for ch in pattern)


def _covers_exception_rule(rule_selector: str) -> bool:
    code = rule_selector.strip().upper()
    if not code:
        return False
    if code in {"ALL", "BLE", "S", "SIM"}:
        return True
    return any(rule.startswith(code) for rule in EXCEPTION_RULES)


def load_ceilings(pyproject: Path) -> dict[str, int]:
    """Load per-file Ruff exception ceilings from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    raw = config.get("tool", {}).get("sortify", {}).get("ruff-exception-ceilings", {})
    return {path.replace("\\", "/"): int(limit) for path, limit in raw.items()}


def check_per_file_ignores(
    pyproject: Path,
    repo_root: Path | None = None,
    ceilings: Mapping[str, int] | None = None,
) -> list[str]:
    """Validate that [tool.ruff.lint.per-file-ignores] avoids wildcards and stale exception ignores."""
    root = (repo_root or pyproject.resolve().parent).resolve()
    try:
        with open(pyproject, "rb") as fh:
            config = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        rel_pyproject = _normalize_path(str(pyproject), root)
        return [f"{rel_pyproject}: failed to parse pyproject.toml ({exc})"]

    per_file = (
        config.get("tool", {})
        .get("ruff", {})
        .get("lint", {})
        .get("per-file-ignores", {})
    )
    if not isinstance(per_file, dict):
        return []

    normalized_ceilings: dict[str, int] | None = None
    if ceilings is not None:
        normalized_ceilings = {
            _normalize_path(path, root): int(limit) for path, limit in ceilings.items()
        }

    failures: list[str] = []
    for raw_pattern, raw_rules in sorted(per_file.items()):
        norm_pattern = _normalize_path(str(raw_pattern), root)
        if _is_tests_path(norm_pattern):
            continue

        if isinstance(raw_rules, str):
            rules_iter: Sequence[Any] = [raw_rules]
        elif isinstance(raw_rules, Sequence):
            rules_iter = raw_rules
        else:
            continue

        matched_rules = [str(r) for r in rules_iter if _covers_exception_rule(str(r))]
        if not matched_rules:
            continue

        rules_label = ", ".join(matched_rules)
        if _is_wildcard_pattern(norm_pattern):
            failures.append(
                f"{norm_pattern}: wildcard per-file-ignores for {rules_label} are "
                "forbidden outside tests/ in [tool.ruff.lint.per-file-ignores]"
            )
            continue

        if repo_root is not None and not (root / norm_pattern).is_file():
            failures.append(
                f"{norm_pattern}: file does not exist (remove {rules_label} from "
                "[tool.ruff.lint.per-file-ignores])"
            )
            continue

        if (
            normalized_ceilings is not None
            and normalized_ceilings.get(norm_pattern, 0) <= 0
        ):
            failures.append(
                f"{norm_pattern}: per-file-ignores for {rules_label} has no active "
                "ceiling in [tool.sortify.ruff-exception-ceilings]; remove stale ignore"
            )

    return failures


def _format_diagnostic(item: Mapping[str, Any], norm_path: str) -> str:
    loc = item.get("location")
    if isinstance(loc, Mapping):
        row = loc.get("row", 1)
        col = loc.get("column", 1)
    else:
        row, col = 1, 1
    code = item.get("code") or "syntax-error"
    msg = item.get("message", code)
    return f"{norm_path}:{row}:{col}: {code} {msg}"


def check(
    report: Sequence[Mapping[str, Any]],
    ceilings: Mapping[str, int],
    repo_root: Path | None = None,
    *,
    enforce_ratchet: bool = True,
) -> list[str]:
    """Return failure messages for unlisted violations, ceiling breaches, missing files, or stale ceilings."""
    root = (repo_root or REPO_ROOT).resolve()
    normalized_ceilings = {
        _normalize_path(path, root): int(limit) for path, limit in ceilings.items()
    }

    failures: list[str] = []
    forbidden_ceiling_modules: set[str] = set()
    for module in sorted(normalized_ceilings):
        if _is_wildcard_pattern(module):
            forbidden_ceiling_modules.add(module)
            failures.append(
                f"{module}: wildcard paths are forbidden in "
                "[tool.sortify.ruff-exception-ceilings]"
            )
        elif _is_tests_path(module):
            forbidden_ceiling_modules.add(module)
            failures.append(
                f"{module}: ceilings are forbidden for tests/ in "
                "[tool.sortify.ruff-exception-ceilings]"
            )

    counts: Counter[str] = Counter()
    diagnostics_by_file: defaultdict[str, list[str]] = defaultdict(list)

    for item in report:
        raw_path = str(item.get("filename", "<unknown>"))
        norm_path = _normalize_path(raw_path, root)
        if _is_tests_path(norm_path):
            continue
        code = item.get("code")
        formatted = _format_diagnostic(item, norm_path)
        if isinstance(code, str) and code in EXCEPTION_RULE_SET:
            counts[norm_path] += 1
            diagnostics_by_file[norm_path].append(formatted)
        else:
            failures.append(f"error: {formatted}")

    for module in sorted(counts):
        ceiling = (
            0
            if module in forbidden_ceiling_modules
            else normalized_ceilings.get(module, 0)
        )
        actual = counts[module]
        if actual <= ceiling:
            continue

        details = "; ".join(diagnostics_by_file[module][:5])
        if module not in normalized_ceilings or module in forbidden_ceiling_modules:
            failures.append(
                f"{module}: {actual} violation(s) exceeds its ceiling of 0 "
                f"(unlisted files must remain violation-free: {details})"
            )
        else:
            failures.append(
                f"{module}: {actual} violation(s) exceeds its ceiling of "
                f"{ceiling} ({details})"
            )

    for module, ceiling in sorted(normalized_ceilings.items()):
        if module in forbidden_ceiling_modules:
            continue
        if ceiling <= 0:
            failures.append(
                f"{module}: invalid ceiling {ceiling}; remove clean files from "
                "[tool.sortify.ruff-exception-ceilings]"
            )
            continue
        if repo_root is not None and not (root / module).is_file():
            failures.append(
                f"{module}: file does not exist (remove stale ceiling of {ceiling} "
                "from [tool.sortify.ruff-exception-ceilings])"
            )
            continue
        if not enforce_ratchet:
            continue

        actual = counts.get(module, 0)
        if actual == 0:
            failures.append(
                f"{module}: 0 violations is below its ceiling of {ceiling}; "
                "remove it from [tool.sortify.ruff-exception-ceilings] and "
                "[tool.ruff.lint.per-file-ignores]"
            )
        elif actual < ceiling:
            failures.append(
                f"{module}: {actual} violation(s) is below its ceiling of {ceiling}; "
                f"lower it to {actual} in [tool.sortify.ruff-exception-ceilings]"
            )

    return failures


def _ruff_command(targets: Sequence[str]) -> list[str]:
    venv_ruff = Path(sys.executable).resolve().parent / (
        "ruff.exe" if sys.platform == "win32" else "ruff"
    )
    if venv_ruff.is_file():
        base = [str(venv_ruff)]
    else:
        which_ruff = shutil.which("ruff")
        if which_ruff:
            base = [which_ruff]
        else:
            base = [sys.executable, "-m", "ruff"]
    return [
        *base,
        "check",
        "--isolated",
        "--select",
        ",".join(EXCEPTION_RULES),
        "--output-format",
        "json",
        *targets,
    ]


def run_ruff_report(
    repo_root: Path,
    targets: Sequence[str] = SOURCE_ROOTS,
) -> tuple[int, list[dict[str, Any]] | None, str]:
    """Execute isolated Ruff check for BLE001/S110/SIM105 and parse the JSON diagnostic list."""
    existing_targets = [t for t in targets if (repo_root / t).exists()]
    if not existing_targets:
        return 0, [], ""

    res = subprocess.run(
        _ruff_command(existing_targets),
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    stdout = res.stdout.strip()
    if res.returncode not in (0, 1):
        return (
            res.returncode,
            None,
            res.stderr.strip() or f"ruff check exited with code {res.returncode}",
        )
    if not stdout:
        return 0, [], res.stderr.strip()
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        err_detail = res.stderr.strip() or str(exc)
        return (
            res.returncode or 1,
            None,
            f"Failed to parse ruff JSON output: {err_detail}",
        )
    if not isinstance(parsed, list):
        return (
            res.returncode or 1,
            None,
            "Unexpected ruff JSON payload (expected a list of diagnostics).",
        )
    return 0, parsed, res.stderr.strip()


def _emit_github_annotations(failures: Sequence[str]) -> None:
    for failure in failures:
        target_file = "pyproject.toml"
        if failure.startswith("error: "):
            rest = failure[len("error: ") :]
            target_file = rest.split(":", 1)[0]
        else:
            candidate = failure.split(":", 1)[0]
            if (
                "below its ceiling" not in failure
                and "does not exist" not in failure
                and "per-file-ignores" not in failure
                and "ceilings are forbidden" not in failure
                and "wildcard paths are forbidden" not in failure
                and "invalid ceiling" not in failure
            ):
                target_file = candidate
        clean_msg = failure.replace("\r", " ").replace("\n", " ")
        print(f"::error file={target_file},title=ruff exception ceiling::{clean_msg}")


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when Ruff reports unratcheted BLE001/S110/SIM105 violations, "
            "wildcard per-file-ignores, missing files, or stale ceilings."
        ),
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=None,
        type=Path,
        help="Optional path to pre-generated Ruff JSON report",
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

    try:
        ceilings = load_ceilings(pyproject_path)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Error loading ceilings from {pyproject_path}: {exc}", file=sys.stderr)
        return 1

    config_failures = check_per_file_ignores(
        pyproject_path, repo_root=root, ceilings=ceilings
    )

    report_data: list[dict[str, Any]]
    if args.report is not None:
        report_path: Path = args.report
        if not report_path.exists():
            print(
                f"Error: ruff report does not exist: {report_path}",
                file=sys.stderr,
            )
            return 1
        if not report_path.is_file():
            print(
                f"Error: ruff report is not a regular file: {report_path}",
                file=sys.stderr,
            )
            return 1
        try:
            with open(report_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"Error: failed to read ruff report {report_path}: {exc}",
                file=sys.stderr,
            )
            return 1
        if not isinstance(loaded, list):
            print(
                f"Error: unexpected ruff report payload in {report_path}",
                file=sys.stderr,
            )
            return 1
        report_data = loaded
        check_root: Path | None = repo_root
    else:
        ruff_exit, maybe_report, err_text = run_ruff_report(root)
        if maybe_report is None or ruff_exit != 0:
            print(f"Error running ruff check: {err_text}", file=sys.stderr)
            return 1
        report_data = maybe_report
        check_root = root

    failures = config_failures + check(
        report_data,
        ceilings,
        repo_root=check_root,
        enforce_ratchet=True,
    )

    for module, ceiling in sorted(ceilings.items()):
        print(f"{module}: ceiling {ceiling}")

    if failures:
        if args.github_annotations:
            _emit_github_annotations(failures)
        print("\nruff exception ceiling ratchet failed:")
        for failure in failures:
            print(f"  {failure}")
        return 1

    total_violations = sum(
        1
        for item in report_data
        if not _is_tests_path(_normalize_path(str(item.get("filename", "")), root))
        and item.get("code") in EXCEPTION_RULE_SET
    )
    print(
        f"\nAll {len(ceilings)} ruff exception ceilings matched "
        f"({total_violations} violations across BLE001/S110/SIM105)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
