"""Lint GitHub Actions workflows with `actionlint` when installed or offline YAML rules otherwise."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from yaml.resolver import BaseResolver

REPO_ROOT = Path(__file__).resolve().parent.parent


class _StrictSafeLoader(yaml.SafeLoader):
    """YAML SafeLoader that rejects duplicate mapping keys."""


def _construct_mapping_no_duplicates(
    loader: _StrictSafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            location = (
                f" at line {mark.line + 1}, column {mark.column + 1}"
                if mark is not None
                else ""
            )
            raise yaml.MarkedYAMLError(
                problem=f"duplicate mapping key {key!r}{location}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictSafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


def _discover_workflows(repo_root: Path, passed_files: list[str]) -> list[Path]:
    """Resolve `.github/workflows/*.y*ml` files to lint."""
    workflows_dir = repo_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []

    all_workflows = sorted(
        p
        for p in workflows_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".yml", ".yaml"}
    )
    if not passed_files:
        return all_workflows

    selected: list[Path] = []
    scan_all = False
    for raw in passed_files:
        candidate = Path(raw)
        abs_candidate = (
            candidate.resolve()
            if candidate.is_absolute()
            else (repo_root / candidate).resolve()
        )
        try:
            rel = abs_candidate.relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel = raw.replace("\\", "/")

        if rel in {".github/actionlint.yaml", ".github/actionlint.yml"}:
            scan_all = True
            break
        if (
            rel.startswith(".github/workflows/")
            and abs_candidate.suffix.lower() in {".yml", ".yaml"}
            and abs_candidate.is_file()
        ):
            selected.append(abs_candidate)

    if scan_all or not selected:
        return all_workflows
    return sorted(set(selected))


def _validate_yaml_mapping(
    file_path: Path, label: str
) -> tuple[dict[Any, Any] | None, list[str]]:
    """Parse a YAML file safely with duplicate-key detection and ensure a mapping root."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"{label}: failed to read file ({exc})"]

    try:
        loaded = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError as exc:
        return None, [f"{label}: invalid YAML ({exc})"]

    if not isinstance(loaded, dict):
        return None, [
            f"{label}: expected top-level YAML mapping, got {type(loaded).__name__}"
        ]
    return loaded, []


def _extract_needs(raw_needs: object) -> tuple[list[str], str | None]:
    """Normalize a job `needs` declaration into a list of job IDs."""
    if raw_needs is None:
        return [], None
    if isinstance(raw_needs, str):
        return ([raw_needs] if raw_needs.strip() else []), None
    if isinstance(raw_needs, list) and all(
        isinstance(item, str) and item.strip() for item in raw_needs
    ):
        return [str(item) for item in raw_needs], None
    return (
        [],
        f"invalid 'needs' value {raw_needs!r} (expected string or list of strings)",
    )


def validate_workflows_offline(repo_root: Path, workflows: list[Path]) -> list[str]:
    """Validate GitHub Actions workflow files offline using strict safe YAML rules."""
    errors: list[str] = []
    root_resolved = repo_root.resolve()

    for cfg_name in ("actionlint.yaml", "actionlint.yml"):
        cfg_path = root_resolved / ".github" / cfg_name
        if cfg_path.is_file():
            rel_cfg = cfg_path.relative_to(root_resolved).as_posix()
            _, cfg_errs = _validate_yaml_mapping(cfg_path, rel_cfg)
            errors.extend(cfg_errs)

    for wf_path in workflows:
        try:
            rel_wf = wf_path.resolve().relative_to(root_resolved).as_posix()
        except ValueError:
            rel_wf = wf_path.as_posix()

        data, yaml_errs = _validate_yaml_mapping(wf_path, rel_wf)
        if yaml_errs or data is None:
            errors.extend(yaml_errs)
            continue

        # In YAML 1.1, unquoted `on:` parses to boolean True in PyYAML.
        triggers = data.get("on", data.get(True))
        if triggers is None:
            errors.append(f"{rel_wf}: missing top-level 'on' trigger configuration")
        elif not isinstance(triggers, (str, list, dict)):
            errors.append(
                f"{rel_wf}: invalid 'on' trigger type {type(triggers).__name__}"
            )

        jobs = data.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            errors.append(f"{rel_wf}: 'jobs' must be a non-empty mapping")
            continue

        job_ids = {str(k) for k in jobs}
        needs_graph: dict[str, list[str]] = {}
        for raw_job_id, job_cfg in jobs.items():
            job_id = str(raw_job_id)
            if not isinstance(job_cfg, dict):
                errors.append(f"{rel_wf}: job {job_id!r} must be a mapping")
                continue

            uses_reusable = job_cfg.get("uses")
            runs_on = job_cfg.get("runs-on")
            if uses_reusable is None and runs_on is None:
                errors.append(
                    f"{rel_wf}: job {job_id!r} must define 'runs-on' or reusable 'uses'"
                )

            needs_list, needs_err = _extract_needs(job_cfg.get("needs"))
            if needs_err is not None:
                errors.append(f"{rel_wf}: job {job_id!r} has {needs_err}")
            else:
                needs_graph[job_id] = needs_list
                for dep in needs_list:
                    if dep == job_id:
                        errors.append(
                            f"{rel_wf}: job {job_id!r} cannot depend on itself"
                        )
                    elif dep not in job_ids:
                        errors.append(
                            f"{rel_wf}: job {job_id!r} depends on unknown job {dep!r}"
                        )

            if uses_reusable is not None:
                continue

            steps = job_cfg.get("steps")
            if not isinstance(steps, list) or not steps:
                errors.append(
                    f"{rel_wf}: job {job_id!r} must define a non-empty 'steps' list"
                )
                continue

            for idx, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    errors.append(
                        f"{rel_wf}: job {job_id!r} step #{idx} must be a mapping"
                    )
                    continue
                has_uses = isinstance(step.get("uses"), str) and bool(
                    str(step["uses"]).strip()
                )
                has_run = isinstance(step.get("run"), str) and bool(
                    str(step["run"]).strip()
                )
                if not has_uses and not has_run:
                    errors.append(
                        f"{rel_wf}: job {job_id!r} step #{idx} must specify 'uses' or 'run'"
                    )

        # Check for dependency cycles across jobs
        visiting: set[str] = set()
        visited: set[str] = set()

        def _has_cycle(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            for nxt in needs_graph.get(node, []):
                if nxt in needs_graph and _has_cycle(nxt):
                    return True
            visiting.remove(node)
            visited.add(node)
            return False

        for jid in sorted(needs_graph):
            if _has_cycle(jid):
                errors.append(f"{rel_wf}: cycle detected in job 'needs' dependencies")
                break

    return errors


def run_workflow_lint(
    repo_root: Path | None = None,
    filenames: list[str] | None = None,
    *,
    force_offline: bool = False,
) -> int:
    """Delegate workflow linting to `actionlint` if installed, else validate offline."""
    root = (repo_root or REPO_ROOT).resolve()
    targets = _discover_workflows(root, filenames or [])
    if not targets:
        return 0

    actionlint_bin = None if force_offline else shutil.which("actionlint")
    if actionlint_bin:
        rel_args: list[str] = []
        for path in targets:
            try:
                rel_args.append(path.resolve().relative_to(root).as_posix())
            except ValueError:
                rel_args.append(str(path))
        res = subprocess.run([actionlint_bin, *rel_args], cwd=root, check=False)
        return res.returncode

    errors = validate_workflows_offline(root, targets)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for GitHub Actions workflow validation."""
    parser = argparse.ArgumentParser(
        description=(
            "Lint GitHub Actions workflows via actionlint when available on PATH, "
            "or validate safe YAML and workflow structure offline."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional repository root path.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force offline Python YAML workflow validation even if actionlint is installed.",
    )
    parser.add_argument(
        "filenames",
        nargs="*",
        help="Optional workflow or config filenames passed by pre-commit.",
    )
    args = parser.parse_args(argv)
    return run_workflow_lint(
        repo_root=args.repo_root,
        filenames=list(args.filenames),
        force_offline=bool(args.offline),
    )


if __name__ == "__main__":
    sys.exit(main())
