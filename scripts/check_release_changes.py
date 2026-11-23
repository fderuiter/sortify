"""Release packaging gate for pull request configuration changes.

Determines whether pull request changes require running the cross-platform
PyInstaller and Syft release build matrix in ``.github/workflows/release.yml``.
Tool-only edits in ``pyproject.toml`` (lint, type, test, or coverage tables)
skip the 3-OS release build, whereas runtime, build, lockfile, or unknown
configuration edits trigger it (failing closed on parse or git errors).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Non-pyproject paths that always require running the release binary build.
RELEASE_PACKAGING_PATHS = frozenset(
    {
        ".github/workflows/release.yml",
        "scripts/build.py",
        "scripts/check_release_changes.py",
        "smart-autosorter.spec",
        "scripts/verify_pe_dependencies.py",
        "uv.lock",
        "offline_bundle/requirements.txt",
    }
)

# Allowlisted [tool.*] tables in pyproject.toml that configure only lint, type,
# test, or coverage tooling and never alter built binaries or model weights.
NON_PACKAGING_TOOL_TABLES = frozenset(
    {
        "ruff",
        "ty",
        "pytest",
        "coverage",
        "deptry",
        "mypy",
    }
)

# Allowlisted sub-tables under [tool.sortify] that hold only test/type ratchets.
NON_PACKAGING_SORTIFY_SUBTABLES = frozenset(
    {
        "coverage-floors",
        "ty-warning-ceilings",
    }
)


def _strip_non_packaging_tool_tables(pyproject_data: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of parsed pyproject.toml with lint/type/test tool tables removed."""
    filtered = dict(pyproject_data)
    raw_tool = filtered.get("tool")
    if not isinstance(raw_tool, dict):
        return filtered

    remaining_tools: dict[str, Any] = {}
    for tool_name, tool_cfg in raw_tool.items():
        if tool_name in NON_PACKAGING_TOOL_TABLES:
            continue
        if tool_name == "sortify" and isinstance(tool_cfg, dict):
            remaining_sortify = {
                sub_key: sub_val
                for sub_key, sub_val in tool_cfg.items()
                if sub_key not in NON_PACKAGING_SORTIFY_SUBTABLES
            }
            if remaining_sortify:
                remaining_tools[tool_name] = remaining_sortify
            continue
        remaining_tools[tool_name] = tool_cfg

    if remaining_tools:
        filtered["tool"] = remaining_tools
    else:
        filtered.pop("tool", None)
    return filtered


def pyproject_changes_affect_packaging(
    base_content: str | None,
    head_content: str | None,
) -> bool:
    """Return True if pyproject.toml changes affect build, packaging, or runtime dependencies.

    Fails closed (returns True) if either file is missing or cannot be parsed as valid TOML.
    """
    if base_content is None or head_content is None:
        return True
    try:
        base_data = tomllib.loads(base_content)
        head_data = tomllib.loads(head_content)
    except tomllib.TOMLDecodeError:
        return True

    return _strip_non_packaging_tool_tables(
        base_data
    ) != _strip_non_packaging_tool_tables(head_data)


def should_run_release_build(
    changed_files: Iterable[str],
    *,
    base_pyproject: str | None = None,
    head_pyproject: str | None = None,
) -> bool:
    """Return True when the changed files in a PR require running build-binaries."""
    normalized = {
        path.strip().replace("\\", "/") for path in changed_files if path.strip()
    }
    if normalized & RELEASE_PACKAGING_PATHS:
        return True
    if "pyproject.toml" in normalized:
        return pyproject_changes_affect_packaging(base_pyproject, head_pyproject)
    return False


def _git_output(repo_dir: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _git_file_at_ref(repo_dir: Path, ref: str, path: str) -> str | None:
    res = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout


def evaluate_git_range(
    base_sha: str,
    head_sha: str,
    *,
    repo_dir: Path | None = None,
) -> bool:
    """Evaluate git diff between merge-base(base_sha, head_sha) and head_sha.

    Fails closed (returns True) if git history or refs cannot be resolved.
    """
    cwd = repo_dir or Path.cwd()
    if not base_sha.strip() or not head_sha.strip():
        return True
    try:
        merge_base = _git_output(
            cwd, "merge-base", base_sha.strip(), head_sha.strip()
        ).strip()
        if not merge_base:
            return True
        diff_names = _git_output(
            cwd, "diff", "--name-only", merge_base, head_sha.strip()
        ).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return True

    changed_files = [line.strip() for line in diff_names if line.strip()]
    base_pyproject: str | None = None
    head_pyproject: str | None = None
    if "pyproject.toml" in {p.replace("\\", "/") for p in changed_files}:
        base_pyproject = _git_file_at_ref(cwd, merge_base, "pyproject.toml")
        head_pyproject = _git_file_at_ref(cwd, head_sha.strip(), "pyproject.toml")

    return should_run_release_build(
        changed_files,
        base_pyproject=base_pyproject,
        head_pyproject=head_pyproject,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for release packaging gate."""
    parser = argparse.ArgumentParser(
        description="Determine whether PR changes require cross-platform release binary builds."
    )
    parser.add_argument("--base-sha", required=True, help="Base commit SHA of the PR.")
    parser.add_argument("--head-sha", required=True, help="Head commit SHA of the PR.")
    parser.add_argument(
        "--repo-dir",
        default=".",
        help="Path to the git repository (default: current directory).",
    )
    parser.add_argument(
        "--github-output",
        default="",
        help="Optional path to $GITHUB_OUTPUT file to write should_build=true|false.",
    )
    args = parser.parse_args(argv)

    should_build = evaluate_git_range(
        args.base_sha,
        args.head_sha,
        repo_dir=Path(args.repo_dir),
    )
    value = "true" if should_build else "false"
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as out_f:
            out_f.write(f"should_build={value}\n")
    print(f"should_build={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
