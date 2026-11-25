"""The sort sessions recorded in the app store (ADR 0008)."""

import logging
import os
from collections.abc import Collection, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# Statuses of a session whose moves never finished.
UNFINISHED_STATUSES = ("active", "uncommitted", "failed")

# Statuses of a session eligible for user-requested undo when moves exist.
UNDOABLE_SESSION_STATUSES = (
    "completed",
    "partially_rolled_back",
    "active",
    "failed",
    "uncommitted",
)


def _count(conn, table: str, session_id: str) -> int:
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row else 0
    except Exception:  # noqa: BLE001 - table may not exist in legacy database
        return 0


def list_sessions(store=None) -> list:
    """Return every session in the store, newest first.

    Each entry carries the session's id, timestamp, base folder, status,
    reversible ``step_count`` and ``file_count``, and ``trashed_count`` for
    duplicates handed off to native OS Trash.
    """
    from app.core.moves import session_summary_dicts
    from app.core.store import open_store

    store = store or open_store()
    store_path = str(store.path)
    return session_summary_dicts(store_path, history_db_path=store_path)


def can_undo_session(session_entry: dict[str, Any] | None, store: Any = None) -> bool:
    """Return True only when ``session_entry`` has an undoable status and recorded moves."""
    if not session_entry or not isinstance(session_entry, dict):
        return False
    sid = session_entry.get("session_id")
    if not sid:
        return False
    status = str(session_entry.get("status") or "").strip().lower()
    if status not in UNDOABLE_SESSION_STATUSES:
        return False
    if status == "completed" and int(session_entry.get("trashed_count") or 0) > 0:
        return True
    if int(session_entry.get("step_count") or 0) <= 0:
        return False

    target_db_path = (
        str(store.path)
        if store is not None and getattr(store, "path", None)
        else session_entry.get("history_db_path")
    )
    if isinstance(target_db_path, str) and os.path.exists(target_db_path):
        from app.core.db_conn import DB_ERRORS, get_db_connection
        from app.core.moves import _read_legacy_moves_for_session

        try:
            conn = get_db_connection(target_db_path)
            row = conn.execute(
                "SELECT 1 FROM moves WHERE session_id = ? AND state IN ('done', 'verified', 'intent') LIMIT 1",
                (str(sid),),
            ).fetchone()
            if row is not None:
                return True
            any_moves_row = conn.execute(
                "SELECT 1 FROM moves WHERE session_id = ? LIMIT 1",
                (str(sid),),
            ).fetchone()
            if any_moves_row is not None:
                return False
            legacy_moves = _read_legacy_moves_for_session(conn, str(sid))
            if legacy_moves:
                return any(
                    m.state in ("done", "verified", "intent") for m in legacy_moves
                )
            return False
        except (*DB_ERRORS, OSError, RuntimeError, ValueError):
            return True
    return True


def _has_files(folder: str) -> bool:
    return any(files for _root, _dirs, files in os.walk(folder))


def _step_count_with_legacy_fallback(conn, session_id: str) -> int:
    cnt = _count(conn, "moves", session_id)
    if cnt:
        return cnt
    cnt = _count(conn, "step_ledger", session_id)
    if cnt:
        return cnt
    return _count(conn, "transaction_ledger", session_id)


def find_abandoned_sessions(store=None) -> list:
    """Return the sessions a crash left unfinished, newest first.

    A session with recorded moves can be resumed or rolled back, and one
    without moves is listed when it is in a failed state or when files are
    still trapped in its ``.branches`` safety folder (including a partially
    rolled-back session whose branch folder was not drained).
    """
    from app.core.db_conn import get_db_connection
    from app.core.store import open_store

    store = store or open_store()
    abandoned = []
    candidate_statuses = (*UNFINISHED_STATUSES, "partially_rolled_back")

    conn = get_db_connection(str(store.path))
    placeholders = ", ".join("?" for _ in candidate_statuses)
    rows = list(
        conn.execute(
            "SELECT session_id, base_dir, status FROM sessions "
            f"WHERE status IN ({placeholders}) ORDER BY timestamp DESC",
            candidate_statuses,
        ).fetchall()
    )
    seen_sids = {sid for sid, _b, _s in rows}
    try:
        orphan_intents = conn.execute(
            "SELECT DISTINCT session_id FROM moves WHERE state = 'intent'"
        ).fetchall()
        for (sid,) in orphan_intents:
            if sid and sid not in seen_sids:
                srow = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if srow is None:
                    rows.append((sid, "", "uncommitted"))
                    seen_sids.add(sid)
    except Exception as exc:  # noqa: BLE001 - moves table may not exist in legacy database
        logger.debug("Skipping orphan moves lookup on legacy database: %s", exc)
    steps = {sid: _step_count_with_legacy_fallback(conn, sid) for sid, _b, _s in rows}

    for sid, base_dir, status in rows:
        branch_dir = os.path.join(base_dir, ".branches", sid) if base_dir else ""
        has_trapped = bool(branch_dir) and os.path.isdir(branch_dir)
        has_trapped = has_trapped and _has_files(branch_dir)
        if status == "partially_rolled_back":
            if not has_trapped:
                continue
        elif status != "failed" and not steps[sid] and not has_trapped:
            continue
        entry = {
            "session_id": sid,
            "base_dir": base_dir,
            "status": status,
            "has_trapped_files": has_trapped,
            "safety_folder": branch_dir if has_trapped else None,
        }
        if steps[sid]:
            entry.update(
                has_step_ledger=True, step_count=steps[sid], uncommitted_batch=True
            )
        abandoned.append(entry)
    return abandoned


def has_remaining_undo_moves(session_entry: dict[str, Any], store: Any = None) -> bool:
    """Return True when ``session_entry`` is completed or partially reverted and still has undoable moves."""
    status = str(session_entry.get("status", "")).lower()
    if status not in ("completed", "partially_rolled_back"):
        return False
    if int(session_entry.get("step_count") or 0) <= 0:
        return False
    sid = session_entry.get("session_id")
    if not sid:
        return False
    target_db_path = (
        str(store.path)
        if store is not None and getattr(store, "path", None)
        else session_entry.get("history_db_path")
    )
    if isinstance(target_db_path, str) and os.path.exists(target_db_path):
        try:
            from app.core.db_conn import get_db_connection
            from app.core.moves import _read_legacy_moves_for_session

            conn = get_db_connection(target_db_path)
            row = conn.execute(
                "SELECT 1 FROM moves WHERE session_id = ? AND state IN ('done', 'verified') LIMIT 1",
                (str(sid),),
            ).fetchone()
            if row is not None:
                return True
            any_moves_row = conn.execute(
                "SELECT 1 FROM moves WHERE session_id = ? LIMIT 1",
                (str(sid),),
            ).fetchone()
            if any_moves_row is not None:
                return False
            legacy_moves = _read_legacy_moves_for_session(conn, str(sid))
            if legacy_moves:
                return any(m.state in ("done", "verified") for m in legacy_moves)
            return False
        except Exception:  # noqa: BLE001 - fall back to step_count when legacy or temporary store cannot open
            return True
    return True


def mark_session_abandoned(session_id: str, store=None) -> None:
    """Record that the user chose to leave an unfinished session as it is."""
    from app.core.db_conn import get_db_connection
    from app.core.services import acquire_services
    from app.core.store import open_store

    store = store or open_store()
    with acquire_services(store) as services:

        def _write() -> None:
            conn = get_db_connection(str(store.path))
            with conn:
                services.history.state_machine.transition(
                    session_id, "abandoned", conn=conn
                )

        services.worker.execute_write(_write)


def roll_back_abandoned_session(settings, session_info: dict) -> bool:
    """Undo an unfinished session's moves and delete its session folder.

    The folder is deleted only when every move was undone, so a failed
    rollback leaves the session files and safety storage in place. Returns
    whether the rollback succeeded.
    """
    import shutil

    from app.core.pipeline import SortPipeline
    from app.core.session import AppSession

    base_dir = session_info.get("base_dir")
    session_id = session_info.get("session_id")
    if not (base_dir and session_id):
        return False
    try:
        temp_session = AppSession(settings, base_dir, session_id=session_id)
        try:
            result = SortPipeline(temp_session).undo(session_id)
        finally:
            temp_session.close()
    except Exception as e:  # noqa: BLE001 - a failed rollback must keep the session files, whatever the cause
        logger.error(f"Error during session rollback: {e}")
        return False
    if not result.ok:
        logger.error(f"Session rollback left files in place: {result.failed}")
        return False

    session_dir = session_info.get("session_dir")
    if session_dir and os.path.exists(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
    return True


def forget_abandoned_session(session_info: dict, store=None) -> None:
    """Leave an unfinished session as it is and stop offering to recover it."""
    if session_info.get("session_id"):
        mark_session_abandoned(session_info["session_id"], store=store)


def get_session_moves(session_id: str, store=None) -> list[dict[str, Any]]:
    """Return recorded move operations for ``session_id`` from the app store."""
    from app.core.history import open_session_history

    ctx = open_session_history() if store is None else open_session_history(store)
    with ctx as history_mgr:
        return history_mgr.get_session_moves(session_id)


def preview_session_rollback(
    session_id: str | None,
    *,
    selected_step_ids: Collection[int | str] | None = None,
    ignore_missing: bool = False,
    fallback_items: Sequence[dict[str, Any]] | None = None,
    store=None,
) -> dict[str, Any]:
    """Simulate a selective or full rollback without modifying files on disk."""
    from app.core.history import HistoryManager, open_session_history

    if session_id:
        try:
            ctx = (
                open_session_history() if store is None else open_session_history(store)
            )
            with ctx as history_mgr:
                preview = history_mgr.preview_rollback(
                    session_id=session_id,
                    selected_step_ids=selected_step_ids,
                    ignore_missing=ignore_missing,
                )
            if preview.get("total_operations", 0) > 0 or not fallback_items:
                return preview
        except Exception:  # fall back to preloaded items when store is unavailable
            if fallback_items is None:
                raise

    items = list(fallback_items or [])
    if selected_step_ids is not None:
        sel_set = {str(x) for x in selected_step_ids}
        steps_to_analyze = [
            item for item in items if str(item.get("step_id")) in sel_set
        ]
    else:
        steps_to_analyze = items
    return HistoryManager.simulate_rollback_steps(
        steps_to_analyze,
        session_id=session_id,
        ignore_missing=ignore_missing,
    )
