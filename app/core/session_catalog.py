"""Discovery of sort sessions recorded in history databases on this machine."""

import logging

logger = logging.getLogger(__name__)


def find_all_history_sessions() -> list:
    """Scan all session directories and configuration directories for history databases."""
    from app.config import get_app_dir
    from app.core.db_conn import get_db_connection, sqlite3
    from app.core.path_utils import get_session_base_dir

    search_roots = [get_session_base_dir(), get_app_dir() / "sessions", get_app_dir()]
    history_db_paths = set()

    for root in search_roots:
        if not root.exists():
            continue
        db_file = root / "history.db"
        if db_file.exists():
            history_db_paths.add(db_file.resolve())
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir():
                    child_db = child / "history.db"
                    if child_db.exists():
                        history_db_paths.add(child_db.resolve())

    all_sessions = []
    seen_sids = set()

    for db_path in history_db_paths:
        try:
            conn = get_db_connection(str(db_path))
            with conn:
                cur = conn.execute(
                    "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
                )
                for row in cur.fetchall():
                    sid, ts, base_dir, status = row
                    if sid not in seen_sids:
                        seen_sids.add(sid)
                        step_count = 0
                        try:
                            c = conn.execute(
                                "SELECT COUNT(*) FROM step_ledger WHERE session_id = ?",
                                (sid,),
                            )
                            step_count = c.fetchone()[0]
                        except sqlite3.Error:
                            # Older history DBs have no step_ledger table.
                            step_count = 0

                        file_count = 0
                        try:
                            c = conn.execute(
                                "SELECT COUNT(*) FROM snapshot_files WHERE session_id = ?",
                                (sid,),
                            )
                            file_count = c.fetchone()[0]
                        except sqlite3.Error:
                            # Older history DBs have no snapshot_files table.
                            file_count = 0

                        all_sessions.append(
                            {
                                "session_id": sid,
                                "timestamp": ts,
                                "base_dir": base_dir,
                                "status": status,
                                "step_count": step_count,
                                "file_count": file_count,
                                "history_db_path": str(db_path),
                                "session_dir": str(db_path.parent),
                            }
                        )
        except Exception:
            # One unreadable or foreign database must not hide the others.
            logger.warning(
                "Skipping unreadable history database %s", db_path, exc_info=True
            )

    all_sessions.sort(key=lambda s: s.get("timestamp") or 0.0, reverse=True)
    return all_sessions
