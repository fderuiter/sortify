"""CLI subcommand helpers and execution handler for full and selective session undo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from app.cli.error_emitter import emit_cli_error
from app.cli.settings_args import build_global_subparser_parent
from app.config import AppSettings
from app.core.exceptions import AuditExportError
from app.core.session_catalog import (
    can_undo_session,
    get_session_moves,
    has_remaining_undo_moves,
    list_sessions,
    preview_session_rollback,
)


def register_subparser(
    subparsers: argparse._SubParsersAction,
) -> argparse.ArgumentParser:
    """Register the ``undo`` subcommand parser (global flags only; no transient sort overrides).

    Parameters
    ----------
    subparsers : argparse._SubParsersAction
        Root argument parser subparsers collection.

    Returns
    -------
    argparse.ArgumentParser
        Configured ``undo`` subparser instance.
    """
    parser_undo = subparsers.add_parser(
        "undo",
        parents=[build_global_subparser_parent()],
        help="Undo sorting operations and manage session history",
    )
    parser_undo.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Specific historical session ID UUID to undo",
    )
    parser_undo.add_argument(
        "--list",
        action="store_true",
        help="List active and completed historical sorting sessions",
    )
    parser_undo.add_argument(
        "--latest",
        action="store_true",
        help="Undo the latest historical sorting session",
    )
    parser_undo.add_argument(
        "--export",
        type=str,
        default=None,
        metavar="PATH",
        help="Export audit records for the selected session (or all sessions) to CSV or JSON",
    )
    parser_undo.add_argument(
        "--force",
        action="store_true",
        help="Force undo even if sorted files are missing",
    )
    extend_undo_parser(parser_undo)
    return parser_undo


def extend_undo_parser(parser_undo: argparse.ArgumentParser) -> None:
    """Register selective undo, move listing, export format, and dry-run flags on ``parser_undo``.

    Parameters
    ----------
    parser_undo : argparse.ArgumentParser
        The ``undo`` subcommand parser registered by ``build_parser``.
    """
    parser_undo.add_argument(
        "--list-moves",
        action="store_true",
        help="List remaining undoable file moves for the target session",
    )
    parser_undo.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview undo outcome and potential collisions without moving files",
    )
    parser_undo.add_argument(
        "--step-id",
        dest="step_ids",
        type=int,
        action="append",
        default=None,
        help="Specific move step ID to revert (repeatable for selective undo)",
    )
    parser_undo.add_argument(
        "--file",
        dest="files",
        type=str,
        action="append",
        default=None,
        help="Source or destination file path/name to revert (repeatable for selective undo)",
    )
    parser_undo.add_argument(
        "-f",
        "--format",
        type=str,
        choices=["csv", "json"],
        default=None,
        help="Audit export format when using --export (csv or json)",
    )
    parser_undo.add_argument(
        "--ledger-db",
        type=str,
        default=None,
        help="Optional store database file path when using --export",
    )


def _match_undo_file_arg(
    raw_file: str,
    moves: list[dict[str, Any]],
    base_dir: str | None = None,
) -> list[int]:
    """Match a CLI ``--file`` selector against a session's active move records."""
    cleaned = raw_file.strip()
    if not cleaned:
        return []
    norm_slash = cleaned.replace("\\", "/").strip("/")
    norm_lower = norm_slash.lower()

    candidates_abs: set[str] = {os.path.normcase(os.path.abspath(cleaned))}
    if base_dir and not os.path.isabs(cleaned):
        candidates_abs.add(
            os.path.normcase(os.path.abspath(os.path.join(base_dir, cleaned)))
        )

    exact_matches: list[int] = []
    rel_or_name_matches: list[int] = []
    for move in moves:
        step_id = int(move["step_id"])
        src = str(move.get("source_path") or "")
        dst = str(move.get("target_path") or "")
        src_abs = os.path.normcase(os.path.abspath(src)) if src else ""
        dst_abs = os.path.normcase(os.path.abspath(dst)) if dst else ""
        if src_abs in candidates_abs or dst_abs in candidates_abs:
            exact_matches.append(step_id)
            continue

        rel_candidates = {
            os.path.basename(src).lower(),
            os.path.basename(dst).lower(),
            src.replace("\\", "/").strip("/").lower(),
            dst.replace("\\", "/").strip("/").lower(),
        }
        if base_dir:
            for p_str in (src, dst):
                if p_str:
                    try:
                        rel_p = (
                            Path(p_str)
                            .resolve(strict=False)
                            .relative_to(Path(base_dir).resolve(strict=False))
                            .as_posix()
                            .lower()
                        )
                        rel_candidates.add(rel_p)
                    except ValueError:
                        pass
        if norm_lower in rel_candidates or any(
            rc.endswith("/" + norm_lower) for rc in rel_candidates if rc
        ):
            rel_or_name_matches.append(step_id)

    return exact_matches if exact_matches else rel_or_name_matches


def execute_undo_command(
    args: argparse.Namespace,
    settings: AppSettings,
    *,
    apply_overrides_fn: Callable[[AppSettings, argparse.Namespace], None] | None = None,
    list_sessions_fn: Callable[[], list[dict[str, Any]]] = list_sessions,
) -> None:
    """Execute ``smart-autosorter undo`` for full/selective rollback, listing, or dry-run preview.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed CLI namespace for the ``undo`` subcommand.
    settings : AppSettings
        Active application configuration wrapper.
    apply_overrides_fn : Callable[[AppSettings, argparse.Namespace], None] | None, optional
        Optional callback to apply common CLI config overrides before execution.
    list_sessions_fn : Callable[[], list[dict[str, Any]]], optional
        Session listing callable (defaults to ``app.core.session_catalog.list_sessions``).
    """
    if apply_overrides_fn is not None:
        apply_overrides_fn(settings, args)

    list_flag = getattr(args, "list", False)
    list_moves_flag = getattr(args, "list_moves", False)
    dry_run_flag = getattr(args, "dry_run", False)
    latest_flag = getattr(args, "latest", False)
    export_path = getattr(args, "export", None)
    export_format = getattr(args, "format", None)
    ledger_db_path = getattr(args, "ledger_db", None)
    session_id_arg = getattr(args, "session_id", None)
    force_flag = getattr(args, "force", False)
    json_output = getattr(args, "json", False)
    quiet = getattr(args, "quiet", False)
    raw_step_ids: list[int] = list(getattr(args, "step_ids", None) or [])
    raw_files: list[str] = list(getattr(args, "files", None) or [])

    if export_path is not None:
        from app.core.services import acquire_services

        lease = (
            acquire_services(db_path=ledger_db_path)
            if ledger_db_path
            else acquire_services()
        )
        try:
            history_mgr = lease.history
            target_export_sid: str | None = session_id_arg
            if target_export_sid is None and latest_flag:
                export_sessions = history_mgr.get_sessions()
                if not export_sessions:
                    emit_cli_error(
                        "Error: No historical sorting sessions available to export.",
                        json_mode=json_output,
                        exit_code=1,
                    )
                target_export_sid = str(export_sessions[0]["session_id"])

            export_res = history_mgr.export_audit_log(
                output_path=export_path,
                session_id=target_export_sid,
                format=export_format,
            )
            if json_output:
                sys.stdout.write(json.dumps(export_res, indent=2) + "\n")
                sys.stdout.flush()
            elif not quiet:
                print(
                    f"Audit log exported ({export_res.get('format', '').upper()}) to "
                    f"'{export_res.get('output_path', export_path)}'. "
                    f"{export_res.get('count', 0)} record(s) written.",
                    file=sys.stderr,
                )
            sys.exit(0)
        except (ValueError, AuditExportError) as err:
            emit_cli_error(
                str(err),
                json_mode=json_output,
                exit_code=1,
            )
        finally:
            lease.close()

    # Constraint: Default to --latest when no session ID or --list flag specified
    if not list_flag and not latest_flag and not session_id_arg:
        latest_flag = True

    all_sessions = list_sessions_fn()

    if list_flag:
        if json_output:
            sys.stdout.write(json.dumps(all_sessions, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not all_sessions:
                print("No historical sorting sessions found.", file=sys.stderr)
            else:
                if not quiet:
                    print("Historical Sorting Sessions:", file=sys.stderr)
                for s in all_sessions:
                    ts = s.get("timestamp")
                    ts_str = (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                        if ts
                        else "N/A"
                    )
                    step_cnt = int(s.get("step_count") or 0)
                    file_cnt = int(s.get("file_count") or 0)
                    print(
                        f"  Session ID: {s['session_id']} | Base: {s.get('base_dir', 'N/A')} | "
                        f"Status: {s.get('status', 'N/A')} | Moves: {step_cnt} | Files: {file_cnt} | Time: {ts_str}"
                    )
        sys.exit(0)

    target_session: dict[str, Any] | None = None
    if session_id_arg:
        for s in all_sessions:
            if s["session_id"] == session_id_arg:
                target_session = s
                break
        if not target_session:
            if not list_moves_flag and not dry_run_flag:
                emit_cli_error(
                    f"Error: Session '{session_id_arg}' was not found or cannot be undone.",
                    json_mode=json_output,
                    exit_code=1,
                    extra={"session_id": session_id_arg},
                )
            target_session = {
                "session_id": session_id_arg,
                "history_db_path": None,
                "base_dir": None,
            }
        elif (
            not list_moves_flag
            and not dry_run_flag
            and not raw_step_ids
            and not raw_files
            and not can_undo_session(target_session)
        ):
            sess_status = str(target_session.get("status") or "").strip().lower()
            if sess_status == "rolled_back":
                reason_msg = (
                    f"Error: Session '{session_id_arg}' has already been undone."
                )
            elif sess_status == "abandoned":
                reason_msg = f"Error: Session '{session_id_arg}' was abandoned and cannot be undone."
            elif sess_status == "undo_snapshot":
                reason_msg = f"Error: Session '{session_id_arg}' cannot be undone."
            else:
                reason_msg = f"Error: Session '{session_id_arg}' has no reversible file moves to undo."
            emit_cli_error(
                reason_msg,
                json_mode=json_output,
                exit_code=1,
                extra={"session_id": session_id_arg},
            )
    elif latest_flag:
        # Include completed/partially_rolled_back sessions with remaining moves as well as trash-only completed sessions.
        undoable = [
            s
            for s in all_sessions
            if can_undo_session(s)
            and (
                (
                    str(s.get("status", "")).lower()
                    in ("completed", "partially_rolled_back")
                    and has_remaining_undo_moves(s)
                )
                or (
                    str(s.get("status", "")).lower() == "completed"
                    and int(s.get("trashed_count") or 0) > 0
                )
            )
        ]
        if not undoable:
            undoable = [s for s in all_sessions if can_undo_session(s)]
        if undoable:
            target_session = undoable[0]
        else:
            emit_cli_error(
                "Error: No historical sorting sessions available to undo.",
                json_mode=json_output,
                exit_code=1,
            )

    target_session_dict = cast(dict[str, Any], target_session)
    target_session_id: str = target_session_dict["session_id"]
    target_base_dir: str | None = target_session_dict.get("base_dir")

    active_moves: list[dict[str, Any]] | None = None
    if list_moves_flag or raw_step_ids or raw_files:
        active_moves = get_session_moves(target_session_id)

    if list_moves_flag:
        moves_list = active_moves or []
        if json_output:
            payload = {
                "status": "success",
                "session_id": target_session_id,
                "base_dir": target_base_dir,
                "count": len(moves_list),
                "moves": moves_list,
            }
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not moves_list:
                print(
                    f"No remaining undoable moves for session '{target_session_id}'.",
                    file=sys.stderr,
                )
            else:
                if not quiet:
                    print(
                        f"Remaining Undoable Moves for Session '{target_session_id}' ({len(moves_list)}):",
                        file=sys.stderr,
                    )
                for m in moves_list:
                    print(
                        f"  [{m['step_id']}] {m.get('target_path', '')} -> {m.get('source_path', '')}"
                    )
        sys.exit(0)

    selected_step_ids: list[int] | None = None
    if raw_step_ids or raw_files:
        moves_pool = active_moves or []
        valid_ids = {int(m["step_id"]) for m in moves_pool}
        resolved_ids: list[int] = []
        seen_ids: set[int] = set()

        for sid in raw_step_ids:
            if sid not in valid_ids:
                emit_cli_error(
                    f"Error: Step ID {sid} is not an active undoable move in session '{target_session_id}'.",
                    json_mode=json_output,
                    exit_code=1,
                    extra={"session_id": target_session_id, "step_id": sid},
                )
            if sid not in seen_ids:
                seen_ids.add(sid)
                resolved_ids.append(sid)

        for file_arg in raw_files:
            matched_ids = _match_undo_file_arg(
                file_arg, moves_pool, base_dir=target_base_dir
            )
            if not matched_ids:
                emit_cli_error(
                    f"Error: No active undoable move matching '{file_arg}' in session '{target_session_id}'.",
                    json_mode=json_output,
                    exit_code=1,
                    extra={"session_id": target_session_id, "file": file_arg},
                )
            for mid in matched_ids:
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    resolved_ids.append(mid)

        selected_step_ids = resolved_ids

    if dry_run_flag:
        preview = preview_session_rollback(
            target_session_id,
            selected_step_ids=selected_step_ids,
            ignore_missing=force_flag,
        )
        preview["status"] = "success"
        preview["dry_run"] = True
        if json_output:
            sys.stdout.write(json.dumps(preview, indent=2) + "\n")
            sys.stdout.flush()
        else:
            summary_counts = preview.get("summary", {})
            if not quiet:
                print(
                    f"Dry-Run Undo Preview for Session '{target_session_id}':",
                    file=sys.stderr,
                )
            print(
                f"  Total: {preview.get('total_operations', 0)} | "
                f"Clean: {summary_counts.get('clean', 0)} | "
                f"Collisions: {summary_counts.get('collision', 0)} | "
                f"Missing: {summary_counts.get('missing', 0)}"
            )
            for item in preview.get("items", []):
                marker = str(item.get("status", "clean")).upper()
                print(
                    f"  [{item.get('step_id')}] ({marker}) {item.get('target_path')} -> {item.get('source_path')}"
                )
        sys.exit(0)

    try:
        from app.core.pipeline import SortPipeline

        undo_result = SortPipeline(None, settings=settings).undo(
            target_session_id,
            selected_step_ids=selected_step_ids,
            ignore_missing=force_flag,
        )
        if "error" in undo_result.summary:
            raise RuntimeError(str(undo_result.summary["error"]))

        from app.core.plan_format import format_trashed_unrestorable_warning

        restored_count = len(undo_result.moved)
        restored_cnt = undo_result.summary.get("restored")
        trashed_remaining = int(
            undo_result.summary.get("trashed_unrestorable")
            or len(undo_result.trashed)
            or 0
        )
        trash_warning = format_trashed_unrestorable_warning(
            trashed_remaining, session_id=target_session_id
        )
        is_selective = selected_step_ids is not None
        if is_selective and selected_step_ids is not None:
            msg = f"Successfully undid {len(selected_step_ids)} selected move(s) in session '{target_session_id}'."
        elif restored_count == 0 and trashed_remaining > 0:
            noun = "copy" if trashed_remaining == 1 else "copies"
            verb_rem = "remains" if trashed_remaining == 1 else "remain"
            msg = (
                f"No reversible moves in session '{target_session_id}'; "
                f"{trashed_remaining} duplicate {noun} {verb_rem} in OS Trash."
            )
        else:
            msg = f"Successfully undid sorting session '{target_session_id}'."

        result: dict[str, Any] = {
            "status": "success",
            "session_id": target_session_id,
            "restored_count": restored_count,
            "trashed_remaining": trashed_remaining,
            "message": msg,
        }
        if restored_cnt is not None:
            result["restored"] = restored_cnt
        if selected_step_ids is not None:
            result["selected_step_ids"] = selected_step_ids
        if trash_warning:
            result["warnings"] = [trash_warning]

        if json_output:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not (restored_count == 0 and trashed_remaining > 0) and not quiet:
                if is_selective and selected_step_ids is not None:
                    print(
                        f"Undo completed successfully for {len(selected_step_ids)} selected move(s) in session '{target_session_id}'.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Undo completed successfully for session '{target_session_id}'.",
                        file=sys.stderr,
                    )
            if trash_warning:
                print(trash_warning, file=sys.stderr)

        sys.exit(0)
    except (OSError, RuntimeError, ValueError, LookupError, TypeError) as ex:
        emit_cli_error(
            f"Undo failed for session '{target_session_id}': {ex}",
            json_mode=json_output,
            exit_code=1,
            extra={"session_id": target_session_id},
        )
