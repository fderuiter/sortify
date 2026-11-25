"""Partial-commit failure recording and journaled MoveLedger retry helpers."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any

from app.core.audit_reporter import generate_audit_report
from app.core.path_utils import safe_relpath


def record_partial_commit_failure(
    *,
    base_dir: str,
    item: dict[str, Any],
    err: BaseException,
    session_id: str | None,
    history_manager: Any,
    db_lock: threading.Lock,
    audit_records: list[dict[str, Any]] | None,
    chunk_hash_map: dict[str, str] | None = None,
    path_map: dict[str, str] | None = None,
    pending_links: list[dict[str, Any]] | None = None,
) -> None:
    """Record a single-item failure in ``MoveLedger`` and ``audit_records`` without aborting the batch.

    Parameters
    ----------
    base_dir : str
        Target base directory for the sorting run.
    item : dict[str, Any]
        Prepared or raw move item dictionary that failed.
    err : BaseException
        Exception raised while preparing, resolving, or moving ``item``.
    session_id : str | None
        Active sort session identifier, if registered.
    history_manager : Any
        History manager instance owning ``MoveLedger``.
    db_lock : threading.Lock
        Synchronization lock guarding ``path_map``, ``pending_links``, and ``audit_records``.
    audit_records : list[dict[str, Any]] | None
        Shared post-sort verification audit record accumulator.
    chunk_hash_map : dict[str, str] | None, optional
        Pre-computed chunk hash map keyed by relative source path.
    path_map : dict[str, str] | None, optional
        Shared path map restored to keep unmoved source paths pointing at themselves.
    pending_links : list[dict[str, Any]] | None, optional
        Pending shortcut/symlink entries pruned when ``source_path`` stays unmoved.
    """
    from app.core.mover import (
        MoveOpState,
        MoveVerificationStatus,
        _record_move_intent,
        _record_move_state,
        _update_path_map_entry,
    )

    source_path = str(item.get("source_path") or "")
    if source_path and os.path.lexists(source_path):
        if path_map is not None:
            _update_path_map_entry(path_map, source_path, source_path, lock=db_lock)
        if pending_links is not None:
            norm_src = os.path.normcase(os.path.abspath(source_path))
            with db_lock:
                pending_links[:] = [
                    entry
                    for entry in pending_links
                    if os.path.normcase(os.path.abspath(entry.get("source_path", "")))
                    != norm_src
                ]
    current_dest = str(item.get("current_dest") or item.get("active_dest") or "")
    filename = str(item.get("filename") or os.path.basename(source_path))
    planned_dst = str(
        item.get("dest_path")
        or (
            os.path.normpath(os.path.join(base_dir, current_dest, filename))
            if (base_dir or current_dest or filename)
            else ""
        )
    )

    pre_hash = str(item.get("pre_hash") or "")
    if not pre_hash and chunk_hash_map and source_path and base_dir:
        rel_src = safe_relpath(source_path, base_dir).replace("\\", "/")
        pre_hash = str(chunk_hash_map.get(rel_src) or "")
    if not pre_hash and source_path and os.path.isfile(source_path):
        try:
            from app.core.extractor import get_file_hash

            pre_hash = get_file_hash(source_path)
        except OSError:
            pre_hash = ""

    moves = getattr(history_manager, "moves", None) if history_manager else None
    if moves is not None and session_id and source_path:
        try:
            already_recorded = False
            if hasattr(moves, "list_moves"):
                norm_src = os.path.normcase(os.path.abspath(source_path))
                existing = moves.list_moves(session_id)
                already_recorded = any(
                    os.path.normcase(os.path.abspath(getattr(m, "src", ""))) == norm_src
                    and getattr(m, "state", None) == MoveOpState.FAILED.value
                    for m in existing
                )
            if not already_recorded:
                seq = _record_move_intent(
                    moves, session_id, source_path, planned_dst, pre_hash
                )
                _record_move_state(
                    moves,
                    session_id,
                    seq,
                    MoveOpState.FAILED,
                    required=False,
                    error=str(err),
                )
        except Exception as ledger_err:  # noqa: BLE001 - ledger recording must never mask partial-commit recovery
            logging.debug(
                f"Could not record failed move ledger state for {source_path}: {ledger_err}"
            )

    if audit_records is not None:
        with db_lock:
            audit_records.append(
                {
                    "source_path": source_path,
                    "destination_path": planned_dst,
                    "pre_hash": pre_hash,
                    "post_hash": None,
                    "status": MoveVerificationStatus.MOVE_FAIL.value,
                    "error": str(err),
                }
            )


def execute_journaled_retry(
    base_dir: str,
    failed_items: list[dict[str, Any]],
    *,
    progress_callback: Any = None,
    db: Any = None,
    history_manager: Any = None,
    runtime_settings: Any = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Re-attempt failed file moves with ``MoveLedger`` journaling and collision-safe renames.

    Journals all retry attempts through ``MoveLedger`` and ``DBWorker`` (ADR 0003,
    ADR 0006) while enforcing collision-safe no-replace renames via
    ``_get_safe_path_with_reservations`` and ``resilient_move`` (ADR 0005).

    Parameters
    ----------
    base_dir : str
        Target base directory path.
    failed_items : list[dict[str, Any]]
        Failed item dictionaries (audit records or retry queue entries).
    progress_callback : Any, optional
        Optional progress emission callback receiving a float in ``[0.0, 1.0]``.
    db : Any, optional
        Optional database handle for batch path and verified-target updates.
    history_manager : Any, optional
        Optional history manager instance owning ``MoveLedger``.
    runtime_settings : Any, optional
        Optional application runtime settings.
    session_id : str | None, optional
        Optional sort session identifier to record retried moves into.

    Returns
    -------
    dict[str, Any]
        Updated execution summary containing regenerated audit report and ``session_id``.
    """
    from app.core.extractor import get_file_hash
    from app.core.mover import (
        MoveOpState,
        MoveVerificationStatus,
        _flush_chunk_completions_and_db,
        _get_safe_path_with_reservations,
        _record_move_intent,
        is_subpath_or_equal,
    )
    from app.core.resilient_file_ops import resilient_move

    base_dir = os.path.normpath(base_dir) if base_dir else ""
    retried_records: list[dict[str, Any]] = []
    retry_completions: list[dict[str, Any]] = []
    db_updates_batch: list[dict[str, Any]] = []
    reserved_retry_dests: set[str] = set()
    total = len(failed_items)

    eff_session_id: str | None = session_id
    if not eff_session_id:
        for item in failed_items:
            cand_sid = item.get("session_id")
            if isinstance(cand_sid, str) and cand_sid:
                eff_session_id = cand_sid
                break

    moves = getattr(history_manager, "moves", None) if history_manager else None
    created_new_session = False
    if (
        history_manager is not None
        and not eff_session_id
        and failed_items
        and base_dir
        and hasattr(history_manager, "start_session")
    ):
        eff_session_id = history_manager.start_session(base_dir)
        created_new_session = bool(eff_session_id)

    quarantine_dir_name = (
        getattr(runtime_settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine")
        or "_Compliance_Quarantine"
    )
    verified_count = 0

    for idx, item in enumerate(failed_items):
        src = item.get("source_path") or item.get("relative_source") or ""
        dst = item.get("destination_path") or item.get("relative_destination") or ""

        if src and not os.path.isabs(src) and base_dir:
            src = os.path.normpath(os.path.join(base_dir, src))
        if dst and not os.path.isabs(dst) and base_dir:
            dst = os.path.normpath(os.path.join(base_dir, dst))

        pre_hash = item.get("pre_hash") or ""
        if src and os.path.isfile(src) and not os.path.islink(src):
            with contextlib.suppress(OSError):
                pre_hash = get_file_hash(src)

        post_hash = None
        move_error = None
        move_seq = None
        target_dst = dst

        if src and os.path.lexists(src) and dst:
            try:
                if base_dir and not is_subpath_or_equal(dst, base_dir):
                    raise ValueError(
                        f"Runtime boundary violation: Destination path '{dst}' attempts to escape target base directory '{base_dir}'."
                    )
                dst_dir = os.path.dirname(dst)
                if dst_dir:
                    os.makedirs(dst_dir, exist_ok=True)
                    target_dst = os.path.normpath(
                        _get_safe_path_with_reservations(
                            dst_dir, os.path.basename(dst), src, reserved_retry_dests
                        )
                    )
                    reserved_retry_dests.add(
                        os.path.normcase(os.path.abspath(target_dst))
                    )
                else:
                    target_dst = dst

                move_seq = _record_move_intent(
                    moves, eff_session_id, src, target_dst, pre_hash
                )
                dst = resilient_move(src, target_dst)
            except Exception as err:  # noqa: BLE001 - capture per-item retry error without aborting remaining items
                move_error = str(err)
                logging.error(f"Retry move failed for {src} -> {target_dst}: {err}")
                if move_seq is not None:
                    retry_completions.append(
                        {
                            "seq": move_seq,
                            "state": MoveOpState.FAILED.value,
                            "error": move_error,
                            "required": False,
                        }
                    )

        if not move_error and dst and os.path.exists(dst):
            try:
                post_hash = get_file_hash(dst)
            except OSError:
                post_hash = None

            if not pre_hash:
                pre_hash = post_hash

            status = (
                MoveVerificationStatus.VERIFIED_PASS
                if (pre_hash == post_hash)
                else MoveVerificationStatus.MISMATCH_FAIL
            )
            if status == MoveVerificationStatus.VERIFIED_PASS:
                if move_seq is not None:
                    retry_completions.append(
                        {
                            "seq": move_seq,
                            "state": MoveOpState.VERIFIED.value,
                            "dst": dst,
                            "displaced": None,
                            "required": True,
                        }
                    )
                verified_count += 1
                if base_dir:
                    src_rel = safe_relpath(src, base_dir).replace("\\", "/")
                    dst_rel = safe_relpath(dst, base_dir).replace("\\", "/")
                    dst_folder = os.path.dirname(dst_rel).replace("\\", "/")
                    if post_hash and dst_folder != quarantine_dir_name:
                        db_updates_batch.append(
                            {
                                "type": "verified_target",
                                "args": (base_dir, post_hash, dst_folder),
                            }
                        )
                    db_updates_batch.append(
                        {
                            "type": "document_path",
                            "args": (base_dir, src_rel, dst_rel),
                        }
                    )
            else:
                if move_seq is not None:
                    retry_completions.append(
                        {
                            "seq": move_seq,
                            "state": MoveOpState.DONE.value,
                            "dst": dst,
                            "displaced": None,
                            "required": True,
                        }
                    )
                move_error = "Pre-sort and post-sort SHA-256 hash mismatch"
        else:
            status = MoveVerificationStatus.MOVE_FAIL
            if not move_error:
                move_error = "Source file does not exist or move failed"

        rec: dict[str, Any] = {
            "source_path": src,
            "destination_path": dst,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
            "status": status.value,
            "error": (
                move_error if status != MoveVerificationStatus.VERIFIED_PASS else None
            ),
        }
        if eff_session_id:
            rec["session_id"] = eff_session_id
        retried_records.append(rec)

        if progress_callback:
            try:
                progress_callback((idx + 1) / total if total > 0 else 1.0)
            except Exception as cb_err:  # noqa: BLE001 - UI progress callbacks must never break retry completion
                logging.debug(f"Retry progress callback ignored error: {cb_err}")

    _flush_chunk_completions_and_db(
        db, moves, eff_session_id, retry_completions, db_updates_batch
    )

    if history_manager is not None and eff_session_id:
        if verified_count > 0 and hasattr(history_manager, "complete_session"):
            curr_status = None
            hm_db_path = getattr(history_manager, "db_path", None)
            if isinstance(hm_db_path, str) and os.path.exists(hm_db_path):
                try:
                    from app.core.db_conn import get_db_connection

                    s_row = (
                        get_db_connection(hm_db_path)
                        .execute(
                            "SELECT status FROM sessions WHERE session_id = ?",
                            (eff_session_id,),
                        )
                        .fetchone()
                    )
                    curr_status = str(s_row[0]).lower() if s_row and s_row[0] else None
                except Exception:  # noqa: BLE001 - best-effort status lookup before completion transition
                    curr_status = None
            if curr_status != "partially_rolled_back":
                try:
                    history_manager.complete_session(eff_session_id)
                except Exception as comp_err:  # noqa: BLE001 - keep existing settled status when transition is a no-op
                    logging.debug(
                        f"Session {eff_session_id} status kept as-is after retry: {comp_err}"
                    )
        elif (
            created_new_session
            and verified_count == 0
            and hasattr(history_manager, "_transition_session_status")
        ):
            try:
                hm_db = getattr(history_manager, "db", None)
                hm_worker = getattr(hm_db, "worker", None)
                if hm_worker is not None and hasattr(hm_worker, "execute_write"):
                    hm_worker.execute_write(
                        lambda: history_manager._transition_session_status(
                            eff_session_id, "failed"
                        )
                    )
                else:
                    history_manager._transition_session_status(eff_session_id, "failed")
            except Exception as fail_err:  # noqa: BLE001 - best-effort failed status update on newly created retry session
                logging.debug(
                    f"Could not mark newly created retry session {eff_session_id} failed: {fail_err}"
                )

    report = generate_audit_report(base_dir, retried_records, session_id=eff_session_id)
    res: dict[str, Any] = {
        "base_dir": base_dir,
        "retried_count": total,
        "audit_report": report,
    }
    if eff_session_id is not None:
        res["session_id"] = eff_session_id
    return res
