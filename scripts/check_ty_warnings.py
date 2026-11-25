"""Fail when ty reports errors, new warnings, forbidden suppressions, or stale ceilings.

Runs ``ty check`` concurrently across ``linux``, ``darwin``, and ``win32`` by
default and validates all configured source roots (``app``, ``scripts``,
``tests``, ``.agents``, ``sandbox_cli.py``) or a scoped list of staged files:

- Any unbaselined error-level diagnostic on any platform fails immediately.
- Warning ceilings and baseline entries are forbidden outside ``tests/``
  (including ``app/``, ``scripts/``, ``.agents/``, and ``sandbox_cli.py``).
- ``tests/ty_baseline.json`` enforces per-``(file, rule)`` upper-bound ceilings
  across platforms without column-span fragility, while
  ``[tool.sortify.ty-warning-ceilings]`` enforces per-file warning ceilings.
- Across a full-repository multi-platform run, any stale ceiling whose highest
  diagnostic count across ``linux``, ``darwin``, and ``win32`` drops below its
  configured limit fails immediately; ``--sync-baseline`` updates
  ``tests/ty_baseline.json`` deterministically from any OS.

Usage:
  uv run python scripts/check_ty_warnings.py [ty_report.json | PATH ...]
      [--platform PLATFORM] [--github-annotations] [--sync-baseline]
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
FORBIDDEN_ROOTS = frozenset({"app", "scripts", ".agents", "sandbox_cli.py"})
DEFAULT_CHECK_PATHS: tuple[str, ...] = (
    "app",
    "scripts",
    "tests",
    ".agents",
    "sandbox_cli.py",
)
TY_INCLUDE_OVERRIDE = (
    'src.include = ["app", "scripts", "tests", ".agents", "sandbox_cli.py"]'
)
DEFAULT_BASELINE_REL_PATH = Path("tests/ty_baseline.json")
CONFIG_TRIGGER_PATHS = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "tests/ty_baseline.json",
        "ty.toml",
    }
)


def load_ceilings(pyproject: Path) -> dict[str, int]:
    """Load per-file ty warning ceilings from pyproject.toml."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    raw = config.get("tool", {}).get("sortify", {}).get("ty-warning-ceilings", {})
    return {path.replace("\\", "/"): int(limit) for path, limit in raw.items()}


def _configured_baseline_path(pyproject: Path) -> Path | None:
    """Return explicitly configured [tool.ty].baseline path if present in pyproject.toml."""
    with open(pyproject, "rb") as fh:
        config = tomllib.load(fh)
    raw_baseline = config.get("tool", {}).get("ty", {}).get("baseline")
    if not isinstance(raw_baseline, str) or not raw_baseline.strip():
        return None
    candidate = Path(raw_baseline)
    if not candidate.is_absolute():
        candidate = pyproject.resolve().parent / candidate
    return candidate


def load_baseline_path(pyproject: Path, repo_root: Path | None = None) -> Path | None:
    """Return the active ty baseline JSON path (explicit config or default tests/ty_baseline.json)."""
    configured = _configured_baseline_path(pyproject)
    if configured is not None:
        return configured
    root = (repo_root or pyproject.resolve().parent).resolve()
    default_candidate = root / DEFAULT_BASELINE_REL_PATH
    if default_candidate.exists():
        return default_candidate
    return None


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


def _is_forbidden_baseline_path(norm_path: str) -> bool:
    return _is_forbidden_source_path(norm_path) or not norm_path.startswith("tests/")


def check_baseline(pyproject: Path, repo_root: Path | None = None) -> list[str]:
    """Validate baseline JSON structure and ensure entries stay restricted to tests/."""
    root = (repo_root or pyproject.resolve().parent).resolve()
    configured = _configured_baseline_path(pyproject)
    baseline_path = configured or load_baseline_path(pyproject, repo_root=root)
    if baseline_path is None:
        return []
    rel_baseline = _normalize_path(str(baseline_path), root)
    if not baseline_path.is_file():
        return [f"{rel_baseline}: configured ty baseline file does not exist"]
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{rel_baseline}: failed to parse ty baseline JSON ({exc})"]
    if not isinstance(payload, dict):
        return [f"{rel_baseline}: ty baseline must be a JSON object"]

    files_obj = payload.get("files", payload)
    if not isinstance(files_obj, dict):
        return [f"{rel_baseline}: ty baseline 'files' must be a JSON object"]

    failures: list[str] = []
    for raw_file in sorted(files_obj):
        if raw_file == "version" and files_obj is payload:
            continue
        norm_file = _normalize_path(str(raw_file), root)
        if _is_forbidden_baseline_path(norm_file):
            failures.append(
                f"{norm_file}: baseline entries are forbidden for app/, scripts/, "
                f".agents/, and sandbox_cli.py (found in {rel_baseline})"
            )
            continue

        raw_rules = files_obj[raw_file]
        if isinstance(raw_rules, dict):
            if not raw_rules:
                failures.append(
                    f"{norm_file}: empty rule ceiling map in {rel_baseline}; "
                    "remove clean files from baseline"
                )
                continue
            for rule_name, limit in sorted(raw_rules.items()):
                if (
                    not isinstance(rule_name, str)
                    or not rule_name.strip()
                    or isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit <= 0
                ):
                    failures.append(
                        f"{norm_file} [{rule_name}]: invalid baseline ceiling "
                        f"{limit!r} in {rel_baseline} (expected positive integer)"
                    )
        elif isinstance(raw_rules, list):
            if raw_rules:
                failures.append(
                    f"{norm_file}: column-locked list baseline entries are not "
                    f"supported in {rel_baseline}; run --sync-baseline to write "
                    "(file, rule) ceilings"
                )
        else:
            failures.append(
                f"{norm_file}: baseline entry in {rel_baseline} must map rule names "
                "to positive integer ceilings"
            )
    return failures


def load_baseline_ceilings(
    pyproject: Path, repo_root: Path | None = None
) -> dict[str, dict[str, int]]:
    """Load (file, rule) integer ceilings from tests/ty_baseline.json when present."""
    root = (repo_root or pyproject.resolve().parent).resolve()
    baseline_path = load_baseline_path(pyproject, repo_root=root)
    if baseline_path is None or not baseline_path.is_file():
        return {}
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    files_obj = payload.get("files", payload)
    if not isinstance(files_obj, dict):
        return {}

    result: dict[str, dict[str, int]] = {}
    for raw_file, raw_rules in files_obj.items():
        if raw_file == "version" and files_obj is payload:
            continue
        if not isinstance(raw_rules, Mapping):
            continue
        norm_file = _normalize_path(str(raw_file), root)
        parsed_rules: dict[str, int] = {}
        for rule_name, limit in raw_rules.items():
            if (
                isinstance(rule_name, str)
                and rule_name.strip()
                and not isinstance(limit, bool)
                and isinstance(limit, int)
            ):
                parsed_rules[rule_name] = limit
        if parsed_rules:
            result[norm_file] = parsed_rules
    return result


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
    baseline_ceilings: Mapping[str, Mapping[str, int]] | None = None,
    enforce_ratchet: bool = True,
) -> list[str]:
    """Return failure messages for errors, forbidden entries, ceiling breaches, or stale ceilings."""
    root = (repo_root or REPO_ROOT).resolve()
    normalized_ceilings = {
        _normalize_path(path, root): limit for path, limit in ceilings.items()
    }
    normalized_baseline: dict[str, dict[str, int]] = {}
    if baseline_ceilings:
        for path, rules_map in baseline_ceilings.items():
            norm_path = _normalize_path(path, root)
            normalized_baseline[norm_path] = {
                str(rule): int(limit) for rule, limit in rules_map.items()
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
                ".agents/, and sandbox_cli.py in [tool.sortify.ty-warning-ceilings]"
            )

    forbidden_baseline_modules: set[str] = set()
    for module in sorted(normalized_baseline):
        if _is_forbidden_baseline_path(module):
            forbidden_baseline_modules.add(module)
            failures.append(
                f"{module}: baseline entries are forbidden for app/, scripts/, "
                ".agents/, and sandbox_cli.py (found in tests/ty_baseline.json)"
            )

    counts_by_platform: dict[str, Counter[str]] = {}
    warnings_by_platform: dict[str, defaultdict[str, list[str]]] = {}
    rule_counts_by_platform: dict[str, Counter[tuple[str, str]]] = {}
    rule_diags_by_platform: dict[str, defaultdict[tuple[str, str], list[str]]] = {}
    errors_seen: dict[str, list[str]] = defaultdict(list)

    for plat_name, items in platform_reports.items():
        plat_counts: Counter[str] = Counter()
        plat_warnings: defaultdict[str, list[str]] = defaultdict(list)
        plat_rule_counts: Counter[tuple[str, str]] = Counter()
        plat_rule_diags: defaultdict[tuple[str, str], list[str]] = defaultdict(list)

        for item in items:
            loc = item.get("location") or {}
            raw_path = str(loc.get("path", "<unknown>"))
            norm_path = _normalize_path(raw_path, root)
            rule = str(item.get("check_name", "ty"))
            severity = str(item.get("severity", "major")).lower()
            formatted = _format_diagnostic(item, norm_path)

            file_rule_map = (
                {}
                if norm_path in forbidden_baseline_modules
                or _is_forbidden_baseline_path(norm_path)
                else normalized_baseline.get(norm_path, {})
            )
            if rule in file_rule_map and file_rule_map[rule] > 0:
                plat_rule_counts[(norm_path, rule)] += 1
                plat_rule_diags[(norm_path, rule)].append(formatted)
            elif severity in WARNING_SEVERITIES:
                plat_counts[norm_path] += 1
                plat_warnings[norm_path].append(formatted)
            else:
                if plat_name not in errors_seen[formatted]:
                    errors_seen[formatted].append(plat_name)

        counts_by_platform[plat_name] = plat_counts
        warnings_by_platform[plat_name] = plat_warnings
        rule_counts_by_platform[plat_name] = plat_rule_counts
        rule_diags_by_platform[plat_name] = plat_rule_diags

    for formatted_err, err_plats in errors_seen.items():
        if multi_platform:
            plat_suffix = f" [{', '.join(err_plats)}]"
        else:
            plat_suffix = ""
        failures.append(f"error: {formatted_err}{plat_suffix}")

    all_baselined_pairs = sorted(
        {pair for rc in rule_counts_by_platform.values() for pair in rc}
    )
    for module, rule in all_baselined_pairs:
        rule_ceiling = normalized_baseline.get(module, {}).get(rule, 0)
        exceeding_plats = [
            plat
            for plat, rc in rule_counts_by_platform.items()
            if rc.get((module, rule), 0) > rule_ceiling
        ]
        if not exceeding_plats:
            continue
        worst_plat = max(
            exceeding_plats,
            key=lambda p: rule_counts_by_platform[p].get((module, rule), 0),
        )
        worst_count = rule_counts_by_platform[worst_plat][(module, rule)]
        details = "; ".join(rule_diags_by_platform[worst_plat][(module, rule)][:5])
        plat_note = f" on {', '.join(exceeding_plats)}" if multi_platform else ""
        failures.append(
            f"{module} [{rule}]: {worst_count} diagnostic(s){plat_note} exceeds its "
            f"baseline ceiling of {rule_ceiling} ({details})"
        )

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

    for module, rules_map in sorted(normalized_baseline.items()):
        if module in forbidden_baseline_modules:
            continue
        if repo_root is not None and not (root / module).is_file():
            failures.append(
                f"{module}: file does not exist (remove stale baseline entry "
                "from tests/ty_baseline.json)"
            )
            continue
        for rule, rule_ceiling in sorted(rules_map.items()):
            if rule_ceiling <= 0:
                failures.append(
                    f"{module} [{rule}]: invalid baseline ceiling {rule_ceiling}; "
                    "remove clean rules from tests/ty_baseline.json"
                )
                continue
            if not enforce_ratchet or not platform_reports:
                continue

            max_rule_count = max(
                (rc.get((module, rule), 0) for rc in rule_counts_by_platform.values()),
                default=0,
            )
            if max_rule_count == 0:
                failures.append(
                    f"{module} [{rule}]: 0 diagnostics is below its baseline "
                    f"ceiling of {rule_ceiling}; run "
                    "'uv run python scripts/check_ty_warnings.py --sync-baseline' "
                    "to remove it from tests/ty_baseline.json"
                )
            elif max_rule_count < rule_ceiling:
                failures.append(
                    f"{module} [{rule}]: {max_rule_count} diagnostic(s) is below "
                    f"its baseline ceiling of {rule_ceiling}; run "
                    "'uv run python scripts/check_ty_warnings.py --sync-baseline' "
                    f"to lower it to {max_rule_count} in tests/ty_baseline.json"
                )

    return failures


def _ty_command(
    platform: str | None = None,
    paths: Sequence[str] | None = None,
) -> list[str]:
    venv_ty = Path(sys.executable).resolve().parent / (
        "ty.exe" if sys.platform == "win32" else "ty"
    )
    if venv_ty.is_file():
        cmd = [
            str(venv_ty),
            "check",
            "-c",
            TY_INCLUDE_OVERRIDE,
            "--output-format",
            "gitlab",
        ]
    else:
        which_ty = shutil.which("ty")
        if which_ty:
            cmd = [
                which_ty,
                "check",
                "-c",
                TY_INCLUDE_OVERRIDE,
                "--output-format",
                "gitlab",
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "ty",
                "check",
                "-c",
                TY_INCLUDE_OVERRIDE,
                "--output-format",
                "gitlab",
            ]
    if platform:
        cmd.extend(["--platform", platform])
    if paths:
        cmd.extend(paths)
    return cmd


def run_ty_report(
    repo_root: Path,
    *,
    platform: str | None = None,
    paths: Sequence[str] | None = None,
) -> tuple[int, list[dict[str, Any]] | None, str]:
    """Execute ty check in GitLab JSON format and parse the diagnostic list."""
    target_paths: Sequence[str] = (
        tuple(paths)
        if paths is not None
        else tuple(p for p in DEFAULT_CHECK_PATHS if (repo_root / p).exists())
    )
    if not target_paths:
        return 0, [], ""
    res = subprocess.run(
        _ty_command(platform=platform, paths=target_paths),
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
    repo_root: Path,
    platforms: Sequence[str] = PLATFORMS,
    *,
    paths: Sequence[str] | None = None,
) -> dict[str, tuple[int, list[dict[str, Any]] | None, str]]:
    """Execute ty check concurrently across all target platforms."""
    target_platforms = tuple(platforms)
    if not target_platforms:
        return {}
    with ThreadPoolExecutor(max_workers=len(target_platforms)) as pool:
        if paths is None:
            futures = {
                plat: pool.submit(run_ty_report, repo_root, platform=plat)
                for plat in target_platforms
            }
        else:
            futures = {
                plat: pool.submit(run_ty_report, repo_root, platform=plat, paths=paths)
                for plat in target_platforms
            }
        return {plat: futures[plat].result() for plat in target_platforms}


def sync_baseline(
    platform_reports: Mapping[str, Sequence[dict[str, Any]]],
    pyproject: Path,
    repo_root: Path,
) -> tuple[Path, dict[str, dict[str, int]], list[str]]:
    """Compute max (file, rule) ceilings across platforms and write tests/ty_baseline.json."""
    root = repo_root.resolve()
    target_path = _configured_baseline_path(pyproject) or (
        root / DEFAULT_BASELINE_REL_PATH
    )

    forbidden_failures: list[str] = []
    seen_forbidden: set[str] = set()
    counts_by_plat: dict[str, Counter[tuple[str, str]]] = {}

    for plat_name, items in platform_reports.items():
        plat_counter: Counter[tuple[str, str]] = Counter()
        for item in items:
            loc = item.get("location") or {}
            raw_path = str(loc.get("path", "<unknown>"))
            norm_path = _normalize_path(raw_path, root)
            rule = str(item.get("check_name", "ty"))
            if _is_forbidden_baseline_path(norm_path):
                formatted = _format_diagnostic(item, norm_path)
                msg = (
                    f"error: {formatted} [{plat_name}] "
                    "(cannot baseline diagnostics outside tests/)"
                )
                if msg not in seen_forbidden:
                    seen_forbidden.add(msg)
                    forbidden_failures.append(msg)
                continue
            if (root / norm_path).is_file():
                plat_counter[(norm_path, rule)] += 1
        counts_by_plat[plat_name] = plat_counter

    if forbidden_failures:
        return target_path, {}, forbidden_failures

    all_pairs = sorted(
        {pair for counter in counts_by_plat.values() for pair in counter}
    )
    files_map: dict[str, dict[str, int]] = {}
    for norm_path, rule in all_pairs:
        max_count = max(
            (counter.get((norm_path, rule), 0) for counter in counts_by_plat.values()),
            default=0,
        )
        if max_count > 0:
            files_map.setdefault(norm_path, {})[rule] = max_count

    ordered_files = {
        file_key: dict(sorted(rules_map.items()))
        for file_key, rules_map in sorted(files_map.items())
    }
    payload = {
        "version": 1,
        "files": ordered_files,
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(payload, indent=2) + "\n")
    return target_path, ordered_files, []


def _emit_github_annotations(failures: list[str]) -> None:
    for failure in failures:
        target_file = "pyproject.toml"
        if not failure.startswith("error: "):
            candidate = failure.split(":", 1)[0].split(" [", 1)[0]
            if (
                "below its ceiling" not in failure
                and "below its baseline ceiling" not in failure
                and "does not exist" not in failure
                and "warning ceilings are forbidden" not in failure
                and "baseline entries are forbidden" not in failure
                and "invalid baseline ceiling" not in failure
                and "empty rule ceiling map" not in failure
                and "column-locked list baseline" not in failure
            ):
                target_file = candidate
            elif "tests/ty_baseline.json" in failure:
                target_file = "tests/ty_baseline.json"
        else:
            rest = failure[len("error: ") :]
            target_file = rest.split(":", 1)[0]
        clean_msg = failure.replace("\r", " ").replace("\n", " ")
        print(f"::error file={target_file},title=ty warning ceiling::{clean_msg}")


def _resolve_cli_mode(
    targets: Sequence[str],
    explicit_paths: Sequence[str] | None,
    pyproject_path: Path,
    root: Path,
) -> tuple[Path | None, list[str] | None]:
    """Classify CLI positional/path arguments into (report_path, scoped_paths).

    Returns:
      - ``(report_path, None)`` when evaluating a pre-generated GitLab JSON report.
      - ``(None, None)`` when running a full-repository check (no paths or config edit).
      - ``(None, scoped_paths)`` when running a scoped check on staged Python files.
    """
    configured_base = _configured_baseline_path(pyproject_path)
    trigger_paths = set(CONFIG_TRIGGER_PATHS)
    if configured_base is not None:
        trigger_paths.add(_normalize_path(str(configured_base), root))

    if explicit_paths is None and len(targets) == 1:
        single_raw = targets[0]
        single_norm = _normalize_path(single_raw, root)
        if (
            not single_raw.endswith((".py", ".pyi"))
            and single_norm not in trigger_paths
        ):
            return Path(single_raw), None

    combined = list(targets) + list(explicit_paths or [])
    if not combined:
        return None, None

    normalized = [_normalize_path(item, root) for item in combined]
    if any(norm in trigger_paths for norm in normalized):
        return None, None

    py_paths: list[str] = []
    seen: set[str] = set()
    for norm in normalized:
        if norm.endswith((".py", ".pyi")) and norm not in seen:
            seen.add(norm)
            py_paths.append(norm)
    return None, py_paths


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when ty reports errors, new warnings, forbidden suppressions, "
            "or stale ceilings across linux, darwin, and win32."
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        default=[],
        help=(
            "Optional pre-generated ty GitLab JSON report path, or staged file "
            "paths to check across linux, darwin, and win32"
        ),
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="Optional explicit list of staged file paths to scope ty check",
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
    parser.add_argument(
        "--sync-baseline",
        action="store_true",
        help=(
            "Run ty across linux, darwin, and win32 and rewrite tests/ty_baseline.json "
            "with deterministic (file, rule) ceilings"
        ),
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

    if args.sync_baseline:
        if args.platform is not None or args.targets or args.paths is not None:
            print(
                "Error: --sync-baseline cannot be combined with --platform or "
                "scoped file paths; it always checks the full repository across "
                f"{', '.join(PLATFORMS)}.",
                file=sys.stderr,
            )
            return 1
        parallel_results = run_ty_reports_parallel(root, PLATFORMS)
        sync_reports: dict[str, list[dict[str, Any]]] = {}
        for plat, (_plat_exit, maybe_report, err_text) in parallel_results.items():
            if maybe_report is None:
                print(
                    f"Error running ty check (--platform {plat}): {err_text}",
                    file=sys.stderr,
                )
                return 1
            sync_reports[plat] = maybe_report

        ceilings = load_ceilings(pyproject_path)
        written_path, synced_map, sync_failures = sync_baseline(
            sync_reports, pyproject_path, root
        )
        if sync_failures:
            print("\nty baseline sync failed:", file=sys.stderr)
            for failure in sync_failures:
                print(f"  {failure}", file=sys.stderr)
            return 1

        post_failures = check_baseline(pyproject_path, repo_root=root) + check(
            sync_reports,
            ceilings,
            repo_root=root,
            baseline_ceilings=synced_map,
            enforce_ratchet=True,
        )
        if post_failures:
            print("\nty warning ratchet failed after baseline sync:", file=sys.stderr)
            for failure in post_failures:
                print(f"  {failure}", file=sys.stderr)
            return 1

        rel_written = _normalize_path(str(written_path), root)
        total_rules = sum(len(rules) for rules in synced_map.values())
        print(
            f"Synced {rel_written} across {', '.join(PLATFORMS)} "
            f"({len(synced_map)} test files, {total_rules} (file, rule) ceilings)."
        )
        return 0

    report_path, scoped_paths = _resolve_cli_mode(
        args.targets, args.paths, pyproject_path, root
    )

    baseline_failures = check_baseline(pyproject_path, repo_root=root)

    ty_exits: list[int] = []
    enforce_ratchet = True
    report_data: list[dict[str, Any]] | dict[str, list[dict[str, Any]]]

    if report_path is not None:
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
    elif scoped_paths is not None:
        enforce_ratchet = False
        check_root = root
        missing_scoped = [
            rel_p for rel_p in scoped_paths if not (root / rel_p).is_file()
        ]
        if missing_scoped and len(missing_scoped) == len(scoped_paths):
            print(
                f"Error: target Python file does not exist: {missing_scoped[0]}",
                file=sys.stderr,
            )
            return 1
        active_paths = [rel_p for rel_p in scoped_paths if (root / rel_p).is_file()]
        if not active_paths:
            report_data = {plat: [] for plat in PLATFORMS}
        elif args.platform is not None:
            ty_exit, maybe_report, err_text = run_ty_report(
                root, platform=args.platform, paths=active_paths
            )
            if maybe_report is None:
                print(f"Error running ty check: {err_text}", file=sys.stderr)
                return 1
            ty_exits.append(ty_exit)
            report_data = maybe_report
        else:
            parallel_results = run_ty_reports_parallel(
                root, PLATFORMS, paths=active_paths
            )
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
        platform_reports = {}
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
    baseline_ceilings = load_baseline_ceilings(pyproject_path, repo_root=root)
    failures = baseline_failures + check(
        report_data,
        ceilings,
        repo_root=check_root,
        baseline_ceilings=baseline_ceilings,
        enforce_ratchet=enforce_ratchet,
    )
    nonzero_exits = [code for code in ty_exits if code != 0]
    if nonzero_exits and not failures and not baseline_ceilings:
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
            f"{platforms_label} (max {max_warnings} warnings, 0 unbaselined errors)."
        )
    else:
        total_warnings = sum(
            1
            for item in report_data
            if str(item.get("severity", "major")).lower() in WARNING_SEVERITIES
        )
        print(
            f"\nAll {len(ceilings)} ty warning ceilings matched "
            f"({total_warnings} warnings, 0 unbaselined errors)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
