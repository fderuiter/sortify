"""Install Git pre-commit hooks while handling `core.hooksPath` environments cleanly."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_git_repo_root(cwd: Path) -> Path | None:
    """Return the Git repository root if `cwd` is inside a Git work tree."""
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    top = res.stdout.strip()
    return Path(top) if top else cwd


def _get_configured_hooks_path(repo_root: Path) -> str | None:
    """Return the active non-empty `core.hooksPath` value if configured."""
    res = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    value = res.stdout.strip()
    return value if value else None


def install_git_hooks(repo_root: Path | None = None) -> int:
    """Install pre-commit hooks or skip cleanly with exit code 0 when `core.hooksPath` is set."""
    target_dir = (repo_root or REPO_ROOT).resolve()
    git_root = _resolve_git_repo_root(target_dir)
    if git_root is None:
        print(
            f"Error: '{target_dir}' is not inside a Git repository.",
            file=sys.stderr,
        )
        return 1

    hooks_path = _get_configured_hooks_path(git_root)
    if hooks_path is not None:
        print(
            f"Git core.hooksPath is set to '{hooks_path}'; skipping .git/hooks installation. "
            "Run 'SKIP=no-commit-to-branch uv run pre-commit run --all-files' to run checks manually."
        )
        return 0

    res = subprocess.run(
        [sys.executable, "-m", "pre_commit", "install"],
        cwd=git_root,
        check=False,
    )
    return res.returncode


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for installing local Git pre-commit hooks."""
    parser = argparse.ArgumentParser(
        description=(
            "Install pre-commit Git hooks cleanly in standard checkouts and "
            "sandboxes where Git configures core.hooksPath."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional path to the Git repository root (defaults to Sortify repo root).",
    )
    args = parser.parse_args(argv)
    return install_git_hooks(repo_root=args.repo_root)


if __name__ == "__main__":
    sys.exit(main())
