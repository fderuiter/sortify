"""History management module for snapshotting and rollback."""

import logging
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

from app.core.db_conn import get_db_connection
from app.core.migrations import Migration, add_missing_columns, migrate
from app.core.moves import FAILED, INTENT, Move, MoveLedger, migrate_add_moves
from app.core.path_utils import is_junction_path, safe_relpath

try:
    import pylnk3
except ImportError:
    pylnk3 = None


# Session status of the safety snapshot older versions took before an undo.
UNDO_SNAPSHOT_STATUS = "undo_snapshot"


def _robust_move(src, dst):
    """Safely move a file, retrying on temporary Windows sharing violations and file locks."""
    from app.core.resilient_file_ops import resilient_move

    resilient_move(src, dst)


def _remove_staging_leftovers(base_dir: str) -> None:
    """Delete transient ``.tmp_stage_*`` files left by interrupted moves, and nothing else."""
    from app.core.resilient_file_ops import resilient_remove

    for root, _dirs, files in os.walk(base_dir):
        for name in files:
            if name.startswith(".tmp_stage_"):
                try:
                    resilient_remove(os.path.join(root, name))
                except OSError as exc:
                    logging.warning(f"Could not remove staging file {name}: {exc}")


def _purge_overwrite_backups(base_dir: str, session_id: str) -> None:
    """Delete the files a pruned session's overwrites displaced (ADR 0005 point 3)."""
    from app.core.mover import OVERWRITE_BACKUP_DIR, overwrite_backup_dir
    from app.core.resilient_file_ops import resilient_rmtree

    backup_dir = overwrite_backup_dir(base_dir, session_id)
    if not os.path.isdir(backup_dir) or os.path.islink(backup_dir):
        return
    try:
        resilient_rmtree(backup_dir)
        root = os.path.join(base_dir, OVERWRITE_BACKUP_DIR)
        if not os.listdir(root):
            os.rmdir(root)
    except OSError as err:
        logging.warning(f"Could not purge overwrite backups in {backup_dir}: {err}")


def _move_aside_blocking_parents(path: str, base_dir: str, db_conn) -> None:
    """Move any file standing where a parent folder of ``path`` must go.

    The user may have saved a file named like a folder that a sort emptied and
    removed; undo moves it to a free name instead of failing to recreate the folder.
    """
    from app.core.mover import get_safe_path

    parts = safe_relpath(path, base_dir).replace("\\", "/").split("/")
    current = base_dir
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if not os.path.exists(current) or os.path.isdir(current):
            continue
        safe_current = get_safe_path(
            os.path.dirname(current), os.path.basename(current)
        )
        _robust_move(current, safe_current)
        rel_current = safe_relpath(current, base_dir).replace("\\", "/")
        rel_safe = safe_relpath(safe_current, base_dir).replace("\\", "/")
        with db_conn:
            db_conn.execute(
                "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                (rel_safe, base_dir, rel_current, rel_current),
            )


def _is_same_restored_path(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.abspath(actual)) == os.path.normcase(
        os.path.abspath(expected)
    )


def _holds_bytes(path: str, sha256: str | None) -> bool:
    """Return whether ``path`` exists and, when a hash is known, has those bytes."""
    if not os.path.lexists(path):
        return False
    if not sha256 or os.path.islink(path) or not os.path.isfile(path):
        return True
    from app.core.extractor import get_file_hash

    try:
        return get_file_hash(path) == sha256
    except OSError:
        return False


def _find_moved_file(move: Move, claimed: set[str]) -> str | None:
    """Return where ``move`` left its file, or None if it never moved or is gone.

    A ``done`` or ``verified`` row names the path. An ``intent`` row was cut off
    by a crash: the file is still at ``src`` if the move never ran, at ``dst``
    if it did, or at a suffixed name next to ``dst`` if the move had to pick
    another name. ``claimed`` holds paths other rows account for.
    """
    if move.state == FAILED:
        return None
    if move.state != INTENT:
        return move.dst if os.path.lexists(move.dst) else None
    if os.path.lexists(move.src):
        return None
    if move.dst not in claimed and _holds_bytes(move.dst, move.src_sha256):
        return move.dst
    if not move.src_sha256:
        return None
    folder = os.path.dirname(move.dst)
    stem = os.path.splitext(os.path.basename(move.dst))[0]
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        candidate = os.path.join(folder, name)
        if (
            name.startswith(stem)
            and candidate not in claimed
            and _holds_bytes(candidate, move.src_sha256)
        ):
            return candidate
    return None


def _history_db_baseline(conn) -> None:
    """Create the history schema, or complete one left by the old upgrade code."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            timestamp REAL,
            base_dir TEXT,
            status TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_files (
            session_id TEXT,
            original_rel_path TEXT,
            inode INTEGER,
            size INTEGER,
            mtime REAL,
            is_symlink INTEGER DEFAULT 0,
            symlink_target TEXT,
            link_type TEXT,
            arguments TEXT,
            description TEXT,
            icon_file TEXT,
            icon_index INTEGER,
            work_dir TEXT,
            window_mode INTEGER,
            file_hash TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )
    """)
    add_missing_columns(
        conn,
        "snapshot_files",
        {
            "is_symlink": "INTEGER DEFAULT 0",
            "symlink_target": "TEXT",
            "file_hash": "TEXT",
            "link_type": "TEXT",
            "arguments": "TEXT",
            "description": "TEXT",
            "icon_file": "TEXT",
            "icon_index": "INTEGER",
            "work_dir": "TEXT",
            "window_mode": "INTEGER",
        },
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_cache (
            session_id TEXT PRIMARY KEY,
            corpus TEXT,
            locked_files TEXT,
            index_to_word TEXT,
            manual_folders TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_documents (
            session_id TEXT,
            filepath TEXT,
            file_hash TEXT,
            extracted_text TEXT,
            user_verified_target_path TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )
    """)
    add_missing_columns(
        conn, "snapshot_documents", {"user_verified_target_path": "TEXT"}
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS step_ledger (
            step_id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            source_path TEXT,
            target_path TEXT,
            original_path TEXT,
            original_filename TEXT,
            is_cross_volume INTEGER DEFAULT 0,
            is_collision_renamed INTEGER DEFAULT 0,
            file_hash TEXT,
            timestamp REAL,
            status TEXT DEFAULT 'completed',
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_step_ledger_session ON step_ledger(session_id)"
    )


# Ordered schema migrations for history.db. Never edit one after release.
HISTORY_DB_MIGRATIONS = (
    Migration(1, "baseline", _history_db_baseline),
    Migration(2, "moves", migrate_add_moves),
)


class HistoryManager:
    """Manages full directory snapshots and rollback functionality."""

    def __init__(
        self, db, cache_manager, db_path=None, migrations=HISTORY_DB_MIGRATIONS
    ):
        from pathlib import Path

        self.db = db
        self.cache_manager = cache_manager
        self.db_path = db_path or str(Path(db.db_path).parent / "history.db")
        self.migrations = migrations
        self._init_db()
        # The move ledger (ADR 0006) lives in this database and writes through its worker.
        self.moves = MoveLedger(self.db_path, getattr(db, "worker", None))

    def _init_db(self):
        conn = get_db_connection(self.db_path)
        with conn:
            migrate(conn, self.migrations, label=f"History {self.db_path}")

    def _create_snapshot_internal(self, base_dir: str) -> str:
        session_id = str(uuid.uuid4())
        timestamp = time.time()

        from app.core.link_manager import LinkManager
        from app.core.scanner import get_files_recursively

        files = get_files_recursively(base_dir)

        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (session_id, timestamp, base_dir, "active"),
            )

            # 1. Snapshot Files
            file_records = []
            for rel_path in files:
                abs_path = os.path.join(base_dir, rel_path)
                try:
                    st = os.lstat(abs_path)
                    is_symlink = 1 if os.path.islink(abs_path) else 0
                    is_junc = 1 if is_junction_path(abs_path) else 0
                    is_lnk = 1 if abs_path.lower().endswith(".lnk") else 0
                    is_link_entity = bool(is_symlink or is_junc or is_lnk)

                    link_type = None
                    link_target = None
                    arguments = None
                    description = None
                    icon_file = None
                    icon_index = 0
                    work_dir = None
                    window_mode = None

                    if is_junc:
                        link_type = "junction"
                        try:
                            link_target = os.readlink(abs_path)
                        except OSError:
                            info = LinkManager.get_link_info(abs_path)
                            if info:
                                link_target = info.get("target")
                    elif is_symlink:
                        link_type = "symlink"
                        try:
                            link_target = os.readlink(abs_path)
                        except OSError:
                            pass
                    elif is_lnk:
                        link_type = "lnk"
                        if pylnk3:
                            try:
                                parsed = pylnk3.parse(abs_path)
                                link_target = parsed.path
                                arguments = parsed.arguments
                                description = parsed.description
                                icon_file = parsed.icon
                                icon_index = getattr(parsed, "icon_index", 0)
                                work_dir = parsed.work_dir
                                window_mode = parsed.window_mode
                            except Exception:
                                info = LinkManager.get_link_info(abs_path)
                                if info:
                                    link_target = info.get("target")
                        else:
                            info = LinkManager.get_link_info(abs_path)
                            if info:
                                link_target = info.get("target")

                    file_hash = None
                    if not is_link_entity and st.st_size > 0:
                        try:
                            from app.core.extractor import get_file_hash

                            file_hash = get_file_hash(abs_path)
                        except Exception:
                            pass

                    file_records.append(
                        (
                            session_id,
                            rel_path.replace("\\", "/"),
                            st.st_ino,
                            st.st_size,
                            st.st_mtime,
                            is_symlink,
                            is_symlink
                            and (
                                link_target or os.readlink(abs_path)
                                if is_symlink
                                else None
                            )
                            or link_target,
                            link_type,
                            arguments,
                            description,
                            icon_file,
                            icon_index,
                            work_dir,
                            window_mode,
                            file_hash,
                        )
                    )
                except OSError:
                    continue
            if file_records:
                conn.executemany(
                    """
                    INSERT INTO snapshot_files (
                        session_id, original_rel_path, inode, size, mtime, 
                        is_symlink, symlink_target, link_type, arguments, 
                        description, icon_file, icon_index, work_dir, 
                        window_mode, file_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    file_records,
                )

            # 2. Snapshot Cache
            cache_conn = self.cache_manager._get_conn()
            with cache_conn:
                cur = cache_conn.execute(
                    "SELECT corpus, locked_files, index_to_word, manual_folders FROM directory_cache WHERE source_directory = ?",
                    (base_dir,),
                )
                row = cur.fetchone()
            if row:
                conn.execute(
                    "INSERT INTO snapshot_cache (session_id, corpus, locked_files, index_to_word, manual_folders) VALUES (?, ?, ?, ?, ?)",
                    (session_id, row[0], row[1], row[2], row[3]),
                )

            # 3. Snapshot DB
            docs = []
            db_conn = get_db_connection(self.db.db_path)
            with db_conn:
                try:
                    cur = db_conn.execute(
                        "SELECT filepath, file_hash, extracted_text, user_verified_target_path FROM documents WHERE base_dir = ?",
                        (base_dir,),
                    )
                    for r in cur.fetchall():
                        docs.append(
                            (
                                session_id,
                                r[0].replace("\\", "/"),
                                r[1],
                                r[2],
                                r[3] if len(r) > 3 else None,
                            )
                        )
                except Exception:
                    cur = db_conn.execute(
                        "SELECT filepath, file_hash, extracted_text FROM documents WHERE base_dir = ?",
                        (base_dir,),
                    )
                    for r in cur.fetchall():
                        docs.append(
                            (session_id, r[0].replace("\\", "/"), r[1], r[2], None)
                        )
            if docs:
                try:
                    conn.executemany(
                        "INSERT INTO snapshot_documents (session_id, filepath, file_hash, extracted_text, user_verified_target_path) VALUES (?, ?, ?, ?, ?)",
                        docs,
                    )
                except Exception:
                    conn.executemany(
                        "INSERT INTO snapshot_documents (session_id, filepath, file_hash, extracted_text) VALUES (?, ?, ?, ?)",
                        [d[:4] for d in docs],
                    )

            # Prune expired snapshots based on configured age retention limit
            policies = None
            try:
                from app.config import AppSettings

                app_settings = AppSettings()
                policies = getattr(app_settings, "UNIFIED_POLICIES", None) or getattr(
                    app_settings, "POLICIES", None
                )
            except Exception:
                pass
            self._prune_snapshots(conn, policies=policies)

        return session_id

    def create_snapshot(self, base_dir: str) -> str:
        """Create a complete snapshot of the directory tree and its metadata."""

        def _write():
            return self._create_snapshot_internal(base_dir)

        return self.db.worker.execute_write(_write)

    def _prune_snapshots(self, conn, retention_days=None, limit=None, policies=None):
        if retention_days is None:
            try:
                from app.config import AppSettings

                app_settings = AppSettings()
                retention_days = getattr(app_settings, "SNAPSHOT_RETENTION_DAYS", 30)
            except Exception:
                retention_days = 30

        cutoff_timestamp = time.time() - (float(retention_days) * 86400.0)
        cur = conn.execute(
            "SELECT session_id, base_dir, status FROM sessions WHERE timestamp IS NOT NULL AND timestamp < ?",
            (cutoff_timestamp,),
        )
        old_sessions = cur.fetchall()
        for sid, base_dir, status in old_sessions:
            if status and str(status).lower() in (
                "active",
                "retain",
                "retained",
                "archive",
                "archived",
            ):
                continue

            # Never prune divergent history branches that contain unmerged user data
            branch_dir = os.path.join(base_dir, ".branches", sid)
            if os.path.exists(branch_dir) and os.listdir(branch_dir):
                continue

            if policies:
                retained_by_policy = False
                try:
                    from app.core.policy_engine import PolicyEngine

                    cur_files = conn.execute(
                        "SELECT original_rel_path FROM snapshot_files WHERE session_id = ?",
                        (sid,),
                    ).fetchall()
                    file_paths = [r[0] for r in cur_files] or [base_dir]

                    for rule in policies:
                        act = (
                            rule.get("action", "").lower()
                            if isinstance(rule, dict)
                            else getattr(rule, "action", "").lower()
                        )
                        if act in ("retain", "archive"):
                            expr = (
                                rule.get("expression", "")
                                if isinstance(rule, dict)
                                else getattr(rule, "expression", "")
                            )
                            if not expr:
                                continue
                            for fp in file_paths:
                                if PolicyEngine.match_policy(rule, fp, "", ""):
                                    retained_by_policy = True
                                    break
                        if retained_by_policy:
                            break
                except Exception as ex:
                    logging.warning(
                        f"Error evaluating policies during snapshot pruning: {ex}"
                    )
                if retained_by_policy:
                    continue

            conn.execute("DELETE FROM snapshot_files WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_cache WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_documents WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM step_ledger WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM moves WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            _purge_overwrite_backups(base_dir, sid)

    def get_sessions(self) -> List[Dict[str, Any]]:
        """Retrieve a list of all historical sessions, ordered by time."""
        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute(
                "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
            )
            sessions = []
            for r in cur.fetchall():
                sid, ts, b_dir, status = r
                step_count = 0
                try:
                    c = conn.execute(
                        "SELECT COUNT(*) FROM step_ledger WHERE session_id = ?",
                        (sid,),
                    )
                    step_count = c.fetchone()[0]
                except Exception:
                    pass

                file_count = 0
                try:
                    c = conn.execute(
                        "SELECT COUNT(*) FROM snapshot_files WHERE session_id = ?",
                        (sid,),
                    )
                    file_count = c.fetchone()[0]
                except Exception:
                    pass

                sessions.append(
                    {
                        "session_id": sid,
                        "timestamp": ts,
                        "base_dir": b_dir,
                        "status": status,
                        "step_count": step_count,
                        "file_count": file_count,
                    }
                )
            return sessions

    def log_step(
        self,
        session_id: str,
        source_path: str,
        target_path: str,
        original_path: str = None,
        original_filename: str = None,
        is_cross_volume: bool = False,
        is_collision_renamed: bool = False,
        file_hash: str = None,
    ) -> int:
        """Log a single file relocation step in real time into the persistent history database."""
        if not session_id:
            return 0
        norm_src = os.path.normpath(source_path)
        norm_dst = os.path.normpath(target_path)
        orig_path = os.path.normpath(original_path or source_path)
        orig_name = original_filename or os.path.basename(orig_path)
        ts = time.time()

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO step_ledger (
                    session_id, source_path, target_path, original_path,
                    original_filename, is_cross_volume, is_collision_renamed,
                    file_hash, timestamp, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
                """,
                (
                    session_id,
                    norm_src,
                    norm_dst,
                    orig_path,
                    orig_name,
                    1 if is_cross_volume else 0,
                    1 if is_collision_renamed else 0,
                    file_hash,
                    ts,
                ),
            )
            return cursor.lastrowid

    def get_step_ledger(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve all recorded relocation steps for a session in chronological order."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                SELECT step_id, session_id, source_path, target_path, original_path,
                       original_filename, is_cross_volume, is_collision_renamed,
                       file_hash, timestamp, status
                FROM step_ledger
                WHERE session_id = ?
                ORDER BY step_id ASC
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "step_id": r[0],
                    "session_id": r[1],
                    "source_path": r[2],
                    "target_path": r[3],
                    "original_path": r[4],
                    "original_filename": r[5],
                    "is_cross_volume": bool(r[6]),
                    "is_collision_renamed": bool(r[7]),
                    "file_hash": r[8],
                    "timestamp": r[9],
                    "status": r[10],
                }
                for r in rows
            ]

    def clear_step_ledger(self, session_id: str):
        """Clean up temporary ledger records upon successful completion of a batch move operation."""
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute("DELETE FROM step_ledger WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE sessions SET status = 'completed' WHERE session_id = ?",
                (session_id,),
            )

    def export_audit_log(
        self,
        output_path: str,
        session_id: Any = None,
        format: Any = None,
    ) -> Dict[str, Any]:
        """Export session history records into standard CSV or JSON audit log formats."""
        import csv
        import json
        from datetime import datetime, timezone

        db_paths: List[str] = []
        if self.db_path:
            p_abs = str(Path(self.db_path).resolve())
            if os.path.exists(p_abs):
                db_paths.append(p_abs)

        if not db_paths and self.db_path:
            db_paths.append(str(Path(self.db_path).resolve()))

        sessions_map: Dict[str, Dict[str, Any]] = {}
        steps: List[Dict[str, Any]] = []
        seen_step_keys = set()

        for db_p in db_paths:
            try:
                conn = get_db_connection(db_p)
                with conn:
                    if session_id:
                        cur_sess = conn.execute(
                            "SELECT session_id, timestamp, base_dir, status FROM sessions WHERE session_id = ?",
                            (session_id,),
                        )
                    else:
                        cur_sess = conn.execute(
                            "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
                        )
                    for row in cur_sess.fetchall():
                        sid = row[0]
                        if sid and sid not in sessions_map:
                            sessions_map[sid] = {
                                "session_id": sid,
                                "timestamp": row[1],
                                "base_dir": row[2],
                                "status": row[3],
                            }

                    if session_id:
                        cur_steps = conn.execute(
                            """
                            SELECT step_id, session_id, source_path, target_path, original_path,
                                   original_filename, is_cross_volume, is_collision_renamed,
                                   file_hash, timestamp, status
                            FROM step_ledger
                            WHERE session_id = ?
                            ORDER BY step_id ASC
                            """,
                            (session_id,),
                        )
                    else:
                        cur_steps = conn.execute(
                            """
                            SELECT step_id, session_id, source_path, target_path, original_path,
                                   original_filename, is_cross_volume, is_collision_renamed,
                                   file_hash, timestamp, status
                            FROM step_ledger
                            ORDER BY session_id ASC, step_id ASC
                            """
                        )
                    for r in cur_steps.fetchall():
                        step_key = (r[1], r[0], r[2], r[3])
                        if step_key in seen_step_keys:
                            continue
                        seen_step_keys.add(step_key)

                        ts = r[9]
                        ts_iso = ""
                        if ts:
                            try:
                                ts_iso = datetime.fromtimestamp(
                                    ts, tz=timezone.utc
                                ).isoformat()
                            except Exception:
                                pass
                        steps.append(
                            {
                                "step_id": r[0],
                                "session_id": r[1],
                                "source_path": r[2] or "",
                                "target_path": r[3] or "",
                                "original_path": r[4] or "",
                                "original_filename": r[5] or "",
                                "is_cross_volume": bool(r[6]),
                                "is_collision_renamed": bool(r[7]),
                                "file_hash": r[8] or "",
                                "timestamp": r[9] or 0.0,
                                "timestamp_iso": ts_iso,
                                "status": r[10] or "completed",
                            }
                        )
            except Exception:
                pass

        sessions = sorted(
            sessions_map.values(), key=lambda s: s.get("timestamp") or 0.0, reverse=True
        )

        steps.sort(key=lambda s: (s.get("timestamp") or 0.0, s.get("step_id") or 0))

        if not steps:
            try:
                from app.core.ledger import TransactionLedger

                tx_ledger = TransactionLedger()
                pending = tx_ledger.get_pending_entries(session_id=session_id)
                for idx, entry in enumerate(pending, 1):
                    ts = entry.get("updated_at") or entry.get("created_at") or 0.0
                    ts_iso = ""
                    if ts:
                        try:
                            ts_iso = datetime.fromtimestamp(
                                ts, tz=timezone.utc
                            ).isoformat()
                        except Exception:
                            pass
                    steps.append(
                        {
                            "step_id": idx,
                            "session_id": entry.get("session_id")
                            or session_id
                            or "unknown",
                            "source_path": entry.get("source_path") or "",
                            "target_path": entry.get("dest_path") or "",
                            "original_path": entry.get("source_path") or "",
                            "original_filename": os.path.basename(
                                entry.get("source_path") or ""
                            ),
                            "is_cross_volume": False,
                            "is_collision_renamed": False,
                            "file_hash": entry.get("file_hash") or "",
                            "timestamp": ts,
                            "timestamp_iso": ts_iso,
                            "status": entry.get("status") or "pending",
                        }
                    )
            except Exception:
                pass

        if session_id and not sessions and not steps:
            raise ValueError(f"Session '{session_id}' not found in history database.")

        fmt = (format or "").lower().strip()
        if not fmt:
            if output_path.lower().endswith(".csv"):
                fmt = "csv"
            else:
                fmt = "json"

        if fmt not in ("csv", "json"):
            raise ValueError(
                f"Unsupported export format '{fmt}'. Supported formats are 'csv' and 'json'."
            )

        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if fmt == "csv":
            fieldnames = [
                "session_id",
                "step_id",
                "timestamp",
                "timestamp_iso",
                "source_path",
                "target_path",
                "original_path",
                "original_filename",
                "file_hash",
                "status",
            ]
            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for step in steps:
                    writer.writerow(step)
        else:
            export_payload = {
                "export_timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "sessions": sessions,
                "total_steps": len(steps),
                "steps": steps,
            }
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(export_payload, f, indent=2)

        return {
            "status": "success",
            "session_id": session_id,
            "output_path": output_path,
            "format": fmt,
            "count": len(steps),
        }

    def unwind_session(self, session_id: str, db=None) -> int:
        """Undo a session by replaying its ``moves`` rows backwards (ADR 0006)."""
        return self._unwind_session_internal(session_id, db=db)

    def _unwind_session_internal(self, session_id: str, db=None) -> int:
        if db:
            db.invalidate_cache()
        moves = self.moves.list_moves(session_id)
        if not moves:
            raise ValueError(
                f"Session {session_id} has no recorded moves, so there is nothing to undo."
            )
        return self._replay_moves_backwards(session_id, moves, db=db)

    def _replay_moves_backwards(self, session_id: str, moves: list[Move], db=None):
        """Undo a session by walking its ``moves`` rows from last to first (ADR 0006).

        Each moved file goes back to ``src``, and any file its move displaced
        goes back to ``dst``. A file that is gone is skipped, and one that changed
        since the sort is left in place and reported as a conflict. If something
        else now occupies ``src``, the file is restored next to it under a
        suffixed name, so nothing is overwritten. Returns the number of files
        moved back.
        """
        from app.core.mover import _prune_emptied_dirs
        from app.core.resilient_file_ops import (
            resilient_move,
            resilient_remove,
            same_file_content,
        )

        conn = get_db_connection(self.db_path)
        with conn:
            row = conn.execute(
                "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        base_dir = row[0] if row else None

        claimed = {m.dst for m in moves if m.state not in (INTENT, FAILED)}
        docs_conn = get_db_connection(self.db.db_path)
        dirs_to_prune = set()
        restored = 0
        conflicts = []
        failures = []
        for move in reversed(moves):
            # The sort may have created the destination folder even if the move failed.
            dirs_to_prune.add(os.path.dirname(move.dst))
            try:
                moved = _find_moved_file(move, claimed)
                # Rows the mover wrote carry the size of the bytes it hashed; rows
                # imported from step_ledger may hold a text hash instead (#844).
                if (
                    moved
                    and move.size is not None
                    and not _holds_bytes(moved, move.src_sha256)
                ):
                    logging.error(
                        f"Undo conflict: {moved} changed since the sort; left it in place."
                    )
                    conflicts.append(moved)
                    moved = None
                if moved:
                    claimed.add(moved)
                    dirs_to_prune.add(os.path.dirname(moved))
                    if base_dir:
                        _move_aside_blocking_parents(move.src, base_dir, docs_conn)
                    os.makedirs(os.path.dirname(move.src), exist_ok=True)
                    restored_to = move.src
                    if os.path.lexists(move.src) and same_file_content(move.src, moved):
                        # An identical copy is already back in place.
                        resilient_remove(moved)
                    else:
                        restored_to = resilient_move(moved, move.src)
                        if not _is_same_restored_path(restored_to, move.src):
                            logging.warning(
                                f"Undo conflict: {move.src} is occupied by a different file; "
                                f"restored {moved} to {restored_to} instead."
                            )
                    restored += 1
                    if db and base_dir:
                        try:
                            db.update_document_path(
                                base_dir,
                                safe_relpath(moved, base_dir).replace("\\", "/"),
                                safe_relpath(restored_to, base_dir).replace("\\", "/"),
                            )
                        except Exception as err:
                            logging.warning(
                                f"Could not update the document path for {restored_to}: {err}"
                            )
                if move.displaced and os.path.lexists(move.displaced):
                    dirs_to_prune.add(os.path.dirname(move.displaced))
                    put_back = resilient_move(move.displaced, move.dst)
                    if not _is_same_restored_path(put_back, move.dst):
                        logging.warning(
                            f"Undo conflict: {move.dst} is occupied; restored the file "
                            f"an overwrite displaced to {put_back} instead."
                        )
            except Exception as err:
                logging.error(f"Failed to undo move {session_id}#{move.seq}: {err}")
                failures.append(err)

        if base_dir and os.path.exists(base_dir):
            _remove_staging_leftovers(base_dir)
            _prune_emptied_dirs(dirs_to_prune, base_dir)

        if failures:
            raise PermissionError(
                f"Recovery incomplete: {len(failures)} move(s) failed to undo. "
                f"Error: {failures[0]}"
            )
        if conflicts:
            raise ValueError(
                f"Rollback incomplete: file hash mismatch for {len(conflicts)} file(s) "
                f"that changed since the sort, left in place (e.g. {conflicts[0]})."
            )

        with conn:
            conn.execute(
                "UPDATE sessions SET status = 'rolled_back' WHERE session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM step_ledger WHERE session_id = ?", (session_id,))

        if db:
            db.invalidate_cache()
        return restored

    def rollback(self, session_id: str, ignore_missing: bool = False):
        """Undo a session by replaying its ``moves`` rows backwards (ADR 0006).

        Files that are gone are always skipped; ``ignore_missing`` is kept for
        callers and has no effect.
        """
        self.db.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                row = conn.execute(
                    "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
            if not row:
                raise ValueError("Session not found")
            return self._unwind_session_internal(session_id, db=self.db)

        return self.db.worker.execute_write(_write)


@contextmanager
def open_session_history(store=None) -> Iterator[HistoryManager]:
    """Yield a HistoryManager on the app store, with its own DBWorker.

    The DBWorker opened here is stopped on exit.
    """
    from app.core.db_worker import DBWorker
    from app.core.store import open_store

    store = store or open_store()
    db_worker = DBWorker()
    try:
        db = store.database(db_worker)
        yield store.history(db, store.cache(db_worker))
    finally:
        db_worker.stop(stop_embeddings=False)
