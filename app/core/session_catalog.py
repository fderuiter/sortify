"""The sort sessions recorded in the app store (ADR 0008)."""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Statuses of a session whose moves never finished.
UNFINISHED_STATUSES = ("active", "uncommitted", "failed")

ROLLBACK_JOURNAL = "rollback_journal.json"


def _count(conn, table: str, session_id: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
    ).fetchone()
    return row[0] if row else 0


def list_sessions(store=None) -> list:
    """Return every session in the store, newest first.

    Each entry carries the session's id, timestamp, base folder, status and
    how many steps and snapshot files it recorded.
    """
    from app.core.db_conn import get_db_connection
    from app.core.store import open_store

    store = store or open_store()
    conn = get_db_connection(str(store.path))
    with conn:
        rows = conn.execute(
            "SELECT session_id, timestamp, base_dir, status FROM sessions "
            "ORDER BY timestamp DESC"
        ).fetchall()
        return [
            {
                "session_id": sid,
                "timestamp": ts,
                "base_dir": base_dir,
                "status": status,
                "step_count": _count(conn, "step_ledger", sid),
                "file_count": _count(conn, "snapshot_files", sid),
                "history_db_path": str(store.path),
            }
            for sid, ts, base_dir, status in rows
        ]


def _has_files(folder: str) -> bool:
    return any(files for _root, _dirs, files in os.walk(folder))


def _journal_entry(journal_path: Path) -> dict | None:
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            jdata = json.load(f)
    except (OSError, ValueError):
        logger.warning("Skipping unreadable rollback journal %s", journal_path)
        return None
    return {
        "session_id": jdata.get("session_id"),
        "safety_session_id": jdata.get("safety_session_id"),
        "base_dir": jdata.get("base_dir"),
        "journal_path": str(journal_path),
        "is_rollback_recovery": True,
        "status": "interrupted_rollback",
    }


def _rollback_journals(store, session_root: Path | None) -> list[Path]:
    """Return the store's rollback journal, then any that older versions left behind."""
    journals = [store.app_dir / ROLLBACK_JOURNAL]
    if session_root is None:
        from app.core.path_utils import get_session_base_dir

        session_root = get_session_base_dir()
    try:
        journals += [d / ROLLBACK_JOURNAL for d in session_root.iterdir() if d.is_dir()]
    except OSError:
        logger.warning("Could not list session folders in %s", session_root)
    return [j for j in journals if j.is_file()]


def find_abandoned_sessions(store=None, session_root: Path | None = None) -> list:
    """Return the sessions a crash left unfinished, newest first.

    An interrupted rollback, found by its journal, comes first. Then come
    sessions whose moves never finished: one with recorded steps can be resumed
    or rolled back, and one without steps is listed only when files are still
    in its safety folder.
    """
    from app.core.db_conn import get_db_connection
    from app.core.store import open_store

    store = store or open_store()
    abandoned = []

    for journal in _rollback_journals(store, session_root):
        entry = _journal_entry(journal)
        if entry:
            if journal.parent != store.app_dir:
                # An older version's session folder, removed once recovered.
                entry["session_dir"] = str(journal.parent)
            abandoned.append(entry)

    conn = get_db_connection(str(store.path))
    with conn:
        placeholders = ", ".join("?" for _ in UNFINISHED_STATUSES)
        rows = conn.execute(
            "SELECT session_id, base_dir, status FROM sessions "
            f"WHERE status IN ({placeholders}) ORDER BY timestamp DESC",
            UNFINISHED_STATUSES,
        ).fetchall()
        steps = {sid: _count(conn, "step_ledger", sid) for sid, _b, _s in rows}

    for sid, base_dir, status in rows:
        branch_dir = os.path.join(base_dir, ".branches", sid) if base_dir else ""
        has_trapped = bool(branch_dir) and os.path.isdir(branch_dir)
        has_trapped = has_trapped and _has_files(branch_dir)
        if not steps[sid] and not has_trapped:
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
    from app.core.store import open_store

    store = store or open_store()
    conn = get_db_connection(str(store.path))
    with conn:
        conn.execute(
            "UPDATE sessions SET status = 'abandoned' WHERE session_id = ?",
            (session_id,),
        )
