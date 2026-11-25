"""Executable CLI demo and sample workspace seeding helpers for Smart AutoSorter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from app.cli.error_emitter import emit_cli_error
from app.config import AppSettings
from app.core.domain_contracts import _make_json_serializable
from app.core.pipeline import SortPipeline
from app.core.plan_format import format_plan_tree
from app.core.sample_corpus import generate_sample_corpus
from app.core.session import AppSession

DEMO_WORKSPACE_REL = "sandbox/demo_workspace"
_DEMO_PLACEHOLDER_NAMES = frozenset(
    {
        ".gitkeep",
        ".branches",
        ".sortify_duplicates",
        ".sortify_overwritten",
    }
)


def ensure_demo_workspace(
    base_dir: Path | str = DEMO_WORKSPACE_REL,
) -> tuple[Path, list[str]]:
    """Create ``sandbox/demo_workspace`` if needed and seed sample files when empty.

    Existing user files and subfolders are never overwritten; sample documents
    are generated only when the folder is newly created or contains solely
    repository/session placeholder entries (such as ``.gitkeep``).

    Parameters
    ----------
    base_dir : Path | str, optional
        Target demo workspace path (defaults to ``sandbox/demo_workspace``).

    Returns
    -------
    tuple[Path, list[str]]
        Resolved demo workspace directory path and list of newly seeded relative
        filenames (empty when existing non-placeholder entries are already present).
    """
    target = Path(base_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    meaningful_entries = [
        entry for entry in target.iterdir() if entry.name not in _DEMO_PLACEHOLDER_NAMES
    ]
    if not meaningful_entries:
        seeded = generate_sample_corpus(str(target), overwrite=False)
        return target, seeded
    return target, []


def run_demo(
    settings: AppSettings,
    args: argparse.Namespace | None = None,
) -> NoReturn:
    """Seed ``sandbox/demo_workspace``, execute an undoable sort, and print results.

    Parameters
    ----------
    settings : AppSettings
        Active application configuration wrapper.
    args : argparse.Namespace | None, optional
        Parsed CLI namespace carrying output flags (``--json``, ``-q``/``--quiet``).
    """
    json_output = bool(getattr(args, "json", False))
    quiet = bool(getattr(args, "quiet", False))
    dry_run = bool(getattr(args, "dry_run", False))

    try:
        target_path, seeded_files = ensure_demo_workspace(DEMO_WORKSPACE_REL)
    except (OSError, ValueError) as exc:
        emit_cli_error(
            f"Error preparing demo workspace: {exc}",
            json_mode=json_output,
            exit_code=1,
        )

    if not json_output and not quiet and seeded_files:
        print(
            f"Seeded {len(seeded_files)} sample file(s) in '{target_path}'.",
            file=sys.stderr,
        )

    session: AppSession | None = None
    try:
        session = AppSession(settings, base_dir=str(target_path))
        pipeline = SortPipeline(session)
        plan = pipeline.plan()
        serializable_plan = _make_json_serializable(plan)

        if dry_run:
            result: dict[str, Any] = {
                "status": "success",
                "dry_run": True,
                "target_directory": str(target_path),
                "destination_directory": str(target_path),
                "seeded_files": seeded_files,
                "plan": serializable_plan,
            }
            if json_output:
                sys.stdout.write(json.dumps(result, indent=2) + "\n")
                sys.stdout.flush()
            else:
                lines, _n_files, _n_folders = format_plan_tree(serializable_plan)
                if lines:
                    print("\n".join(lines))
                elif not quiet:
                    print(f"Nothing to sort in '{target_path}'.", file=sys.stderr)
            sys.exit(0)

        outcome = pipeline.execute(plan)
        moved_count = len(outcome.moved) + len(outcome.quarantined)
        staged_count = len(outcome.staged)
        trashed_count = len(outcome.trashed)
        reversible_count = moved_count + staged_count

        result = {
            "status": "success",
            "dry_run": False,
            "target_directory": str(target_path),
            "destination_directory": str(target_path),
            "seeded_files": seeded_files,
            "session_id": outcome.session_id,
            "moved_count": moved_count,
            "staged_count": staged_count,
            "trashed_count": trashed_count,
            "plan": serializable_plan,
            "summary": outcome.summary,
        }

        if json_output:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            lines, _n_files, n_folders = format_plan_tree(serializable_plan)
            if lines:
                if not quiet:
                    print(
                        f"Moved {moved_count} file(s) into {n_folders} folder(s) in '{target_path}':",
                        file=sys.stderr,
                    )
                print("\n".join(lines))
            elif moved_count == 0 and staged_count == 0 and trashed_count == 0:
                print(f"Nothing to sort in '{target_path}'.", file=sys.stderr)
            if not quiet:
                print(outcome.describe(), file=sys.stderr)
                for source, reason in outcome.failed:
                    print(f"  Failed: {source}: {reason}", file=sys.stderr)
                if reversible_count > 0 and outcome.session_id:
                    print(
                        "To undo this sort, run: smart-autosorter undo --latest",
                        file=sys.stderr,
                    )
        sys.exit(0)
    except (OSError, RuntimeError, ValueError) as exc:
        emit_cli_error(
            f"Error during demo execution: {exc}",
            json_mode=json_output,
            exit_code=1,
        )
    finally:
        if session is not None:
            session.close()
