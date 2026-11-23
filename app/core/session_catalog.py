"""The sort sessions recorded in the app store (ADR 0008)."""

import logging
import os
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Statuses of a session whose moves never finished.
UNFINISHED_STATUSES = ("active", "uncommitted", "failed", "partially_rolled_back")


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

    Each entry carries the session's id, timestamp, base folder, status and
    how many moves and distinct moved files it recorded in ``moves``.
    """
    from app.core.moves import query_session_summaries
    from app.core.store import open_store

    store = store or open_store()
    rows = query_session_summaries(str(store.path))
    return [
        {
            "session_id": sid,
            "timestamp": ts,
            "base_dir": base_dir,
            "status": status,
            "step_count": step_count,
            "file_count": file_count,
            "history_db_path": str(store.path),
        }
        for sid, ts, base_dir, status, step_count, file_count in rows
    ]


def _has_files(folder: str) -> bool:
    return any(files for _root, _dirs, files in os.walk(folder))


def find_abandoned_sessions(store=None, session_root: Path | None = None) -> list:
    """Return the sessions a crash left unfinished, newest first.

    A session with recorded moves can be resumed or rolled back, and one
    without moves is listed when it is in a failed/partially rolled-back state
    or when files are still in its ``.branches`` safety folder.
    """
    from app.core.db_conn import get_db_connection
    from app.core.store import open_store

    store = store or open_store()
    abandoned = []

    conn = get_db_connection(str(store.path))
    with conn:
        placeholders = ", ".join("?" for _ in UNFINISHED_STATUSES)
        rows = conn.execute(
            "SELECT session_id, base_dir, status FROM sessions "
            f"WHERE status IN ({placeholders}) ORDER BY timestamp DESC",
            UNFINISHED_STATUSES,
        ).fetchall()
        steps = {sid: _count(conn, "moves", sid) for sid, _b, _s in rows}

    for sid, base_dir, status in rows:
        branch_dir = os.path.join(base_dir, ".branches", sid) if base_dir else ""
        has_trapped = bool(branch_dir) and os.path.isdir(branch_dir)
        has_trapped = has_trapped and _has_files(branch_dir)
        if (
            status not in ("failed", "partially_rolled_back")
            and not steps[sid]
            and not has_trapped
        ):
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


def mark_session_abandoned(session_id: str, store=None) -> None:
    """Record that the user chose to leave an unfinished session as it is."""
    from app.core.db_conn import get_db_connection
    from app.core.history import SessionStateMachine
    from app.core.store import open_store

    store = store or open_store()
    conn = get_db_connection(str(store.path))
    with conn:
        sm = SessionStateMachine(str(store.path))
        sm.transition(session_id, "abandoned", conn=conn)


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
            result = SortPipeline(temp_session).undo(session_id, ignore_missing=True)
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
        except Exception:  # noqa: BLE001 - fall back to preloaded items when store is unavailable
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
