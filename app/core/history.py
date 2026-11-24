"""Session history, lifecycle state machine, and reverse-replay undo (ADR 0006)."""

import contextlib
import csv
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Collection, Generator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.db_conn import DB_ERRORS, get_db_connection
from app.core.exceptions import InvalidStateTransitionError
from app.core.lifecycle import LifecycleTelemetryMixin
from app.core.migrations import Migration, add_missing_columns, migrate
from app.core.moves import (
    FAILED,
    INTENT,
    Move,
    MoveLedger,
    migrate_add_moves,
    migrate_import_legacy_moves,
)
from app.core.path_utils import safe_relpath

# Valid session lifecycle statuses
ACTIVE = "active"
COMPLETED = "completed"
ROLLED_BACK = "rolled_back"
PARTIALLY_ROLLED_BACK = "partially_rolled_back"
FAILED_STATUS = "failed"
ABANDONED = "abandoned"
UNCOMMITTED = "uncommitted"

ALL_SESSION_STATUSES: set[str] = {
    ACTIVE,
    COMPLETED,
    ROLLED_BACK,
    PARTIALLY_ROLLED_BACK,
    FAILED_STATUS,
    ABANDONED,
    UNCOMMITTED,
}

# Explicit transition matrix: current_status -> set of allowed target statuses
TRANSITION_MATRIX: dict[str, set[str]] = {
    ACTIVE: {
        ACTIVE,
        COMPLETED,
        ROLLED_BACK,
        PARTIALLY_ROLLED_BACK,
        FAILED_STATUS,
        ABANDONED,
    },
    COMPLETED: {
        COMPLETED,
        ROLLED_BACK,
        PARTIALLY_ROLLED_BACK,
        FAILED_STATUS,
        ABANDONED,
    },
    PARTIALLY_ROLLED_BACK: {
        PARTIALLY_ROLLED_BACK,
        ROLLED_BACK,
        FAILED_STATUS,
        ABANDONED,
    },
    FAILED_STATUS: {
        FAILED_STATUS,
        ROLLED_BACK,
        PARTIALLY_ROLLED_BACK,
        ABANDONED,
        ACTIVE,
        COMPLETED,
    },
    ABANDONED: {
        ABANDONED,
        ROLLED_BACK,
        PARTIALLY_ROLLED_BACK,
        FAILED_STATUS,
    },
    ROLLED_BACK: {
        # Terminal state: only idempotent self-transition is permitted
        ROLLED_BACK,
    },
    UNCOMMITTED: {
        UNCOMMITTED,
        COMPLETED,
        ROLLED_BACK,
        PARTIALLY_ROLLED_BACK,
        FAILED_STATUS,
        ABANDONED,
    },
}


def _ensure_lifecycle_audit_schema(conn) -> None:
    """Ensure the session lifecycle audit ledger exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_lifecycle_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            previous_status TEXT,
            new_status TEXT NOT NULL,
            timestamp REAL NOT NULL,
            error_details TEXT,
            metadata TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_lifecycle_audit_sid ON session_lifecycle_audit (session_id)"
    )


class SessionStateMachine(LifecycleTelemetryMixin):
    """Enforces lifecycle transition rules and records structured audit and telemetry events."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def add_transition_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a callback to receive structured telemetry events on status change."""
        self._listeners.append(callback)

    def remove_transition_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Unregister a telemetry event callback."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    @staticmethod
    def validate_transition(current_status: Optional[str], new_status: str) -> None:
        """Validate whether a status transition is permitted by the explicit transition matrix.

        Raises InvalidStateTransitionError if the transition is disallowed.
        Executes within microseconds (<5ms constraint).
        """
        curr = (current_status or ACTIVE).lower()
        target = new_status.lower()

        allowed = TRANSITION_MATRIX.get(curr)
        if allowed is None or target not in allowed:
            raise InvalidStateTransitionError(
                f"Invalid session lifecycle transition from '{curr}' to '{target}'. "
                f"Allowed transitions from '{curr}': {sorted(allowed) if allowed else []}"
            )

    def transition(
        self,
        session_id: str,
        new_status: str,
        conn=None,
        *,
        error_details: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """Atomically validate and execute a session status transition in the database.

        Records audit logs and structured telemetry events.
        Returns (previous_status, new_status).
        """
        if conn is None:
            if not self.db_path:
                raise ValueError(
                    "db_path or an active SQLite connection must be provided to transition session status."
                )
            managed_conn = get_db_connection(self.db_path)
            with managed_conn:
                return self._execute_transition(
                    managed_conn,
                    session_id,
                    new_status,
                    error_details=error_details,
                    metadata=metadata,
                )
        else:
            return self._execute_transition(
                conn,
                session_id,
                new_status,
                error_details=error_details,
                metadata=metadata,
            )

    def _execute_transition(
        self,
        conn,
        session_id: str,
        new_status: str,
        *,
        error_details: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        cur = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            previous_status = UNCOMMITTED
            self.validate_transition(previous_status, new_status)
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (session_id, time.time(), "", new_status),
            )
        else:
            previous_status = row[0] or ACTIVE
            self.validate_transition(previous_status, new_status)
            conn.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (new_status, session_id),
            )

        now = time.time()
        meta_json = json.dumps(metadata) if metadata else None

        # Write durable audit ledger record
        try:
            _ensure_lifecycle_audit_schema(conn)
            conn.execute(
                """
                INSERT INTO session_lifecycle_audit (session_id, previous_status, new_status, timestamp, error_details, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    previous_status,
                    new_status,
                    now,
                    error_details,
                    meta_json,
                ),
            )
        except DB_ERRORS as audit_exc:
            logging.warning(
                "Failed to write session lifecycle audit ledger: %s", audit_exc
            )

        # Structured audit log
        logging.info(
            "AUDIT: Session %s status transitioned from '%s' to '%s'%s",
            session_id,
            previous_status,
            new_status,
            f" [error: {error_details}]" if error_details else "",
        )

        # Structured telemetry event
        telemetry_event = {
            "event_type": "SESSION_STATUS_CHANGED",
            "session_id": session_id,
            "previous_status": previous_status,
            "new_status": new_status,
            "timestamp": now,
            "error_details": error_details,
            "metadata": metadata or {},
        }
        logging.debug("TELEMETRY: %s", telemetry_event)

        for listener in list(self._listeners):
            try:
                listener(telemetry_event)
            except Exception as listener_exc:
                logging.warning(
                    "Error executing session telemetry listener: %s", listener_exc
                )

        return previous_status, new_status

    def get_audit_events(
        self, session_id: Optional[str] = None, conn=None
    ) -> list[dict[str, Any]]:
        """Retrieve historical lifecycle audit events for a session."""
        if conn is None:
            if not self.db_path:
                return []
            conn = get_db_connection(self.db_path)

        try:
            _ensure_lifecycle_audit_schema(conn)
            if session_id:
                cur = conn.execute(
                    """
                    SELECT session_id, previous_status, new_status, timestamp, error_details, metadata
                    FROM session_lifecycle_audit WHERE session_id = ?
                    ORDER BY id ASC
                    """,
                    (session_id,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT session_id, previous_status, new_status, timestamp, error_details, metadata
                    FROM session_lifecycle_audit
                    ORDER BY id ASC
                    """
                )

            events = []
            for row in cur.fetchall():
                meta = {}
                if row[5]:
                    try:
                        meta = json.loads(row[5])
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                events.append(
                    {
                        "session_id": row[0],
                        "previous_status": row[1],
                        "new_status": row[2],
                        "timestamp": row[3],
                        "error_details": row[4],
                        "metadata": meta,
                    }
                )
            return events
        except DB_ERRORS as e:
            logging.warning("Failed to query session lifecycle audit events: %s", e)
            return []


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

    if not base_dir:
        return
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


def _purge_duplicate_staging(base_dir: str, session_id: str) -> None:
    """Delete staged duplicate files belonging to a pruned session."""
    from app.core.mover import DUPLICATE_STAGING_DIR, duplicate_staging_dir
    from app.core.resilient_file_ops import resilient_rmtree

    if not base_dir:
        return
    stage_dir = duplicate_staging_dir(base_dir, session_id)
    if not os.path.isdir(stage_dir) or os.path.islink(stage_dir):
        return
    try:
        resilient_rmtree(stage_dir)
        root = os.path.join(base_dir, DUPLICATE_STAGING_DIR)
        if os.path.isdir(root) and not os.listdir(root):
            os.rmdir(root)
    except OSError as err:
        logging.warning(f"Could not purge duplicate staging in {stage_dir}: {err}")


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
        if getattr(db_conn, "in_transaction", False) or not hasattr(
            db_conn, "__enter__"
        ):
            db_conn.execute(
                "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                (rel_safe, base_dir, rel_current, rel_current),
            )
        else:
            with db_conn:
                db_conn.execute(
                    "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                    (rel_safe, base_dir, rel_current, rel_current),
                )


def _normalize_restored_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_same_restored_path(actual: str, expected: str) -> bool:
    return _normalize_restored_path(actual) == _normalize_restored_path(expected)


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


def _restore_trapped_branch_files(
    base_dir: str,
    session_id: str,
    docs_conn,
    failures: list[Exception],
) -> int:
    """Restore any files parked in ``<base_dir>/.branches/<session_id>`` back into ``base_dir``."""
    from app.core.resilient_file_ops import resilient_move, resilient_rmtree

    branch_dir = os.path.join(base_dir, ".branches", session_id)
    if not os.path.isdir(branch_dir) or os.path.islink(branch_dir):
        return 0

    restored_count = 0
    for root, _dirs, files in os.walk(branch_dir):
        for fname in files:
            trapped_src = os.path.join(root, fname)
            rel_path = os.path.relpath(trapped_src, branch_dir)
            restore_dst = os.path.join(base_dir, rel_path)
            try:
                _move_aside_blocking_parents(restore_dst, base_dir, docs_conn)
                os.makedirs(os.path.dirname(restore_dst), exist_ok=True)
                resilient_move(trapped_src, restore_dst)
                restored_count += 1
            except Exception as err:
                logging.error(
                    f"Failed to restore trapped branch file {trapped_src}: {err}"
                )
                failures.append(err)

    if not failures:
        try:
            resilient_rmtree(branch_dir)
            branches_root = os.path.join(base_dir, ".branches")
            if os.path.isdir(branches_root) and not os.listdir(branches_root):
                os.rmdir(branches_root)
        except OSError as err:
            logging.warning(f"Could not clean up safety branch {branch_dir}: {err}")
    return restored_count


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
    Migration(3, "legacy_moves", migrate_import_legacy_moves),
)


class HistoryManager:
    """Manages sort session registration, canonical move queries, and replay-based undo."""

    def __init__(
        self, db, cache_manager, db_path=None, migrations=HISTORY_DB_MIGRATIONS
    ):
        self.db = db
        self.cache_manager = cache_manager
        self.db_path = db_path or str(Path(db.db_path).parent / "history.db")
        self.migrations = migrations
        self._init_db()
        self.state_machine = SessionStateMachine(self.db_path)
        # The move ledger (ADR 0006) lives in this database and writes through its worker.
        self.moves = MoveLedger(self.db_path, getattr(db, "worker", None))

    def _init_db(self):
        conn = get_db_connection(self.db_path)
        with conn:
            migrate(conn, self.migrations, label=f"History {self.db_path}")

    def _transition_session_status(
        self,
        session_id: str,
        new_status: str,
        conn=None,
        *,
        error_details: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """Atomically validate and execute a session status transition."""
        return self.state_machine.transition(
            session_id,
            new_status,
            conn=conn,
            error_details=error_details,
            metadata=metadata,
        )

    def _start_session_internal(
        self, base_dir: str, session_id: Optional[str] = None
    ) -> str:
        if not base_dir or not os.path.isdir(base_dir):
            raise OSError(f"Base directory does not exist: {base_dir}")

        sid = str(session_id) if session_id else str(uuid.uuid4())
        timestamp = time.time()

        policies = None
        try:
            from app.config import AppSettings

            policies = getattr(AppSettings(), "RETENTION_POLICIES", None)
        except Exception:
            policies = None

        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (sid, timestamp, base_dir, ACTIVE),
            )
            self._prune_snapshots(conn, policies=policies)
        return sid

    def start_session(self, base_dir: str, session_id: Optional[str] = None) -> str:
        """Register a new sort session in ``sessions`` without walking ``base_dir``."""

        def _write() -> str:
            return self._start_session_internal(base_dir, session_id=session_id)

        return self.db.worker.execute_write(_write)

    def complete_session(
        self, session_id: str, metadata: Optional[dict[str, Any]] = None
    ) -> None:
        """Mark ``session_id`` as ``completed`` while keeping its ``moves`` ledger intact."""
        if not session_id:
            return

        def _write() -> None:
            conn = get_db_connection(self.db_path)
            with conn:
                self._transition_session_status(
                    session_id, COMPLETED, conn=conn, metadata=metadata
                )

        self.db.worker.execute_write(_write)

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

            if base_dir:
                branch_dir = os.path.join(base_dir, ".branches", sid)
                if os.path.exists(branch_dir) and os.listdir(branch_dir):
                    continue

            if policies:
                retained_by_policy = False
                try:
                    from app.core.policy_engine import PolicyEngine

                    cur_moves = conn.execute(
                        "SELECT src, dst FROM moves WHERE session_id = ?",
                        (sid,),
                    ).fetchall()
                    file_paths: list[str] = []
                    for src_p, dst_p in cur_moves:
                        if src_p:
                            file_paths.append(src_p)
                        if dst_p:
                            file_paths.append(dst_p)
                    if not file_paths and base_dir:
                        file_paths = [base_dir]

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
                        f"Error evaluating policies during session pruning: {ex}"
                    )
                if retained_by_policy:
                    continue

            for legacy_table in (
                "snapshot_files",
                "snapshot_cache",
                "snapshot_documents",
                "step_ledger",
            ):
                try:
                    conn.execute(
                        f"DELETE FROM {legacy_table} WHERE session_id = ?", (sid,)
                    )
                except DB_ERRORS:
                    pass
            conn.execute("DELETE FROM moves WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            _purge_overwrite_backups(base_dir, sid)
            _purge_duplicate_staging(base_dir, sid)

    def get_sessions(self) -> list[dict[str, Any]]:
        """Retrieve a list of all historical sessions, newest first, with canonical move counts."""
        from app.core.moves import query_session_summaries

        rows = query_session_summaries(self.db_path)
        return [
            {
                "session_id": sid,
                "timestamp": ts,
                "base_dir": b_dir,
                "status": status,
                "step_count": step_count,
                "file_count": file_count,
            }
            for sid, ts, b_dir, status, step_count, file_count in rows
        ]

    def get_session_moves(self, session_id: str) -> list[dict[str, Any]]:
        """Retrieve itemized recorded file moves for ``session_id`` from canonical ``moves``."""
        moves = self.moves.list_moves(session_id)
        active_moves = [m for m in moves if m.state not in (INTENT, FAILED)]
        items = []
        for m in active_moves:
            sz = m.size
            if sz is None and m.dst and os.path.exists(m.dst):
                try:
                    sz = os.path.getsize(m.dst)
                except OSError:
                    sz = None
            items.append(
                {
                    "step_id": m.seq,
                    "source_path": m.src,
                    "target_path": m.dst,
                    "size": sz,
                    "status": m.state,
                }
            )
        return items

    def export_audit_log(
        self,
        output_path: str,
        session_id: Any = None,
        format: Any = None,
        base_dir: Optional[str] = None,
    ) -> dict[str, Any]:
        """Export canonical session and move records into standard CSV or JSON audit log formats."""
        sessions_map: dict[str, dict[str, Any]] = {}
        steps: list[dict[str, Any]] = []

        if self.db_path:
            db_p = str(Path(self.db_path).resolve())
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
                        cur_moves = conn.execute(
                            """
                            SELECT seq, session_id, src, dst, src_sha256, state,
                                   created_at, updated_at
                            FROM moves
                            WHERE session_id = ?
                            ORDER BY seq ASC
                            """,
                            (session_id,),
                        )
                    else:
                        cur_moves = conn.execute(
                            """
                            SELECT seq, session_id, src, dst, src_sha256, state,
                                   created_at, updated_at
                            FROM moves
                            ORDER BY created_at ASC, session_id ASC, seq ASC
                            """
                        )
                    for (
                        seq,
                        sid,
                        src,
                        dst,
                        src_sha256,
                        state,
                        created_at,
                        updated_at,
                    ) in cur_moves.fetchall():
                        ts = (
                            updated_at
                            if updated_at is not None
                            else (created_at or 0.0)
                        )
                        ts_iso = ""
                        if ts:
                            try:
                                ts_iso = datetime.fromtimestamp(
                                    ts, tz=timezone.utc
                                ).isoformat()
                            except Exception:
                                pass
                        if state in ("done", "verified"):
                            audit_status = "completed"
                        elif state == INTENT:
                            audit_status = "pending"
                        else:
                            audit_status = "failed"

                        src_str = src or ""
                        dst_str = dst or ""
                        steps.append(
                            {
                                "step_id": int(seq),
                                "session_id": sid,
                                "source_path": src_str,
                                "target_path": dst_str,
                                "original_path": src_str,
                                "original_filename": os.path.basename(src_str),
                                "is_cross_volume": False,
                                "is_collision_renamed": os.path.basename(src_str)
                                != os.path.basename(dst_str),
                                "file_hash": src_sha256 or "",
                                "timestamp": ts,
                                "timestamp_iso": ts_iso,
                                "status": audit_status,
                            }
                        )
            except DB_ERRORS:
                pass

        sessions = sorted(
            sessions_map.values(), key=lambda s: s.get("timestamp") or 0.0, reverse=True
        )
        steps.sort(key=lambda s: (s.get("timestamp") or 0.0, s.get("step_id") or 0))

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

        from app.core.audit_reporter import (
            _open_secure_export_file,
            _validate_and_resolve_export_path,
        )

        abs_output_path = _validate_and_resolve_export_path(
            output_path, base_dir=base_dir
        )

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
            with _open_secure_export_file(abs_output_path, "w", newline="") as f:
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
            with _open_secure_export_file(abs_output_path, "w") as f:
                json.dump(export_payload, f, indent=2)

        return {
            "status": "success",
            "session_id": session_id,
            "output_path": abs_output_path,
            "format": fmt,
            "count": len(steps),
        }

    def unwind_session(
        self,
        session_id: str,
        db=None,
        selected_step_ids: Optional[Collection[int | str]] = None,
    ) -> int:
        """Unwind completed relocation steps in reverse order using the canonical move ledger."""
        return self._unwind_session_internal(
            session_id, db=db, selected_step_ids=selected_step_ids
        )

    def _unwind_session_internal(
        self,
        session_id: str,
        db=None,
        selected_step_ids: Optional[Collection[int | str]] = None,
    ) -> int:
        if db and hasattr(db, "invalidate_cache"):
            db.invalidate_cache()
        moves = self.moves.list_moves(session_id)
        if selected_step_ids is not None:
            sel_set = {str(x) for x in selected_step_ids}
            moves_to_unwind = [m for m in moves if str(m.seq) in sel_set]
            active_moves = [m for m in moves if m.state not in (INTENT, FAILED)]
            is_partial = len(moves_to_unwind) < len(active_moves) or not active_moves
            if not moves_to_unwind:
                self._transition_session_status(session_id, PARTIALLY_ROLLED_BACK)
                return 0
        else:
            moves_to_unwind = moves
            is_partial = False

        return self._replay_moves_backwards(
            session_id, moves_to_unwind, db=db, is_partial=is_partial
        )

    def _replay_moves_backwards(
        self,
        session_id: str,
        moves: list[Move],
        db=None,
        is_partial: bool = False,
    ) -> int:
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

        session_source_dirs: list[str] = []
        extra_prune_roots: list[str] = []
        try:
            for ev in self.state_machine.get_audit_events(session_id, conn=conn):
                meta = ev.get("metadata") if isinstance(ev, dict) else None
                if isinstance(meta, dict):
                    meta_dest = meta.get("dest_dir")
                    if isinstance(meta_dest, str) and meta_dest.strip():
                        extra_prune_roots.append(meta_dest)
                    for ext_r in meta.get("external_roots") or []:
                        if isinstance(ext_r, str) and ext_r.strip():
                            extra_prune_roots.append(ext_r)
                    for src_r in meta.get("source_dirs") or []:
                        if isinstance(src_r, str) and src_r.strip():
                            session_source_dirs.append(src_r)
                            extra_prune_roots.append(src_r)
        except Exception:
            pass
        try:
            from app.config import AppSettings

            cfg = AppSettings()
            if cfg.DEST_DIR and cfg.DEST_DIR.strip():
                extra_prune_roots.append(
                    os.path.abspath(os.path.expanduser(cfg.DEST_DIR.strip()))
                )
            for ext_r in cfg.ALLOWED_EXTERNAL_ROOTS or []:
                if isinstance(ext_r, str) and ext_r.strip():
                    extra_prune_roots.append(
                        os.path.abspath(os.path.expanduser(ext_r.strip()))
                    )
        except Exception:
            pass

        from app.core.mover import is_subpath_or_equal

        claimed = {m.dst for m in moves if m.state not in (INTENT, FAILED)}
        docs_conn = get_db_connection(self.db.db_path)
        dirs_to_prune = set()
        restored = 0
        conflicts = []
        failures: list[Exception] = []
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
                    if base_dir and is_subpath_or_equal(move.src, base_dir):
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
                            doc_base = base_dir
                            if not is_subpath_or_equal(restored_to, base_dir):
                                for cand_src in session_source_dirs:
                                    if is_subpath_or_equal(restored_to, cand_src):
                                        doc_base = cand_src
                                        break
                            old_rel = (
                                safe_relpath(moved, doc_base).replace("\\", "/")
                                if is_subpath_or_equal(moved, doc_base)
                                else os.path.abspath(moved).replace("\\", "/")
                            )
                            new_rel = (
                                safe_relpath(restored_to, doc_base).replace("\\", "/")
                                if is_subpath_or_equal(restored_to, doc_base)
                                else os.path.abspath(restored_to).replace("\\", "/")
                            )
                            try:
                                db.update_document_path(
                                    doc_base,
                                    old_rel,
                                    new_rel,
                                    clear_verified_target=True,
                                )
                            except TypeError:
                                db.update_document_path(doc_base, old_rel, new_rel)
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

        if base_dir and not is_partial:
            restored += _restore_trapped_branch_files(
                base_dir, session_id, docs_conn, failures
            )

        prune_roots: list[str] = []
        if base_dir and os.path.exists(base_dir):
            _remove_staging_leftovers(base_dir)
            prune_roots.append(base_dir)
        for extra_root in extra_prune_roots:
            if (
                extra_root
                and os.path.exists(extra_root)
                and extra_root not in prune_roots
            ):
                prune_roots.append(extra_root)
        if prune_roots:
            _prune_emptied_dirs(dirs_to_prune, prune_roots)

        if failures:
            try:
                self._transition_session_status(
                    session_id,
                    FAILED_STATUS,
                    error_details=str(failures[0]),
                    metadata={"failures_count": len(failures)},
                )
            except Exception as st_err:
                logging.warning(
                    f"Could not transition session {session_id} to failed: {st_err}"
                )
            raise PermissionError(
                f"Recovery incomplete: {len(failures)} move(s) failed to undo. "
                f"Error: {failures[0]}"
            )
        if conflicts:
            try:
                self._transition_session_status(
                    session_id,
                    FAILED_STATUS,
                    error_details=f"Rollback incomplete: file hash mismatch for {len(conflicts)} file(s)",
                    metadata={"conflicts_count": len(conflicts)},
                )
            except Exception as st_err:
                logging.warning(
                    f"Could not transition session {session_id} to failed: {st_err}"
                )
            raise ValueError(
                f"Rollback incomplete: file hash mismatch for {len(conflicts)} file(s) "
                f"that changed since the sort, left in place (e.g. {conflicts[0]})."
            )

        status_to_set = PARTIALLY_ROLLED_BACK if is_partial else ROLLED_BACK
        unwound_seqs = [m.seq for m in moves]

        with conn:
            self._transition_session_status(session_id, status_to_set, conn=conn)
            if is_partial and unwound_seqs:
                placeholders = ",".join("?" for _ in unwound_seqs)
                conn.execute(
                    f"DELETE FROM moves WHERE session_id = ? AND seq IN ({placeholders})",
                    [session_id] + unwound_seqs,
                )

        if db and hasattr(db, "invalidate_cache"):
            db.invalidate_cache()
        try:
            from app.core.shared_registry import SharedModelRegistry

            jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
            if jev_engine and hasattr(jev_engine, "clear_cache"):
                jev_engine.clear_cache()
        except Exception:
            pass
        return restored

    def rollback(
        self,
        session_id: str,
        ignore_missing: bool = False,
        selected_step_ids: Optional[Collection[int | str]] = None,
    ):
        """Undo a session solely by replaying its recorded moves backwards (ADR 0006)."""
        if hasattr(self.db, "invalidate_cache"):
            self.db.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            row = conn.execute(
                "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if not row:
                raise ValueError("Session not found")

            try:
                return self._unwind_session_internal(
                    session_id, db=self.db, selected_step_ids=selected_step_ids
                )
            except Exception as exc:
                try:
                    self._transition_session_status(
                        session_id, FAILED_STATUS, error_details=str(exc)
                    )
                except Exception:
                    pass
                raise

        return self.db.worker.execute_write(_write)

    def flush_staged_duplicates(
        self,
        session_id: Optional[str] = None,
        *,
        base_dir: Optional[str] = None,
        max_age_days: Optional[float] = None,
        trashed_only: bool = False,
        trash_fn: Optional[Any] = None,
        audit_ledger: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Export staged duplicates in ``.sortify_duplicates/<session>/`` to native OS Trash.

        Parameters
        ----------
        session_id : str | None, optional
            Specific session to flush; flushes all sessions under ``base_dir`` when omitted.
        base_dir : str | None, optional
            Workspace root containing ``.sortify_duplicates``; inferred from ``session_id`` when omitted.
        max_age_days : float | None, optional
            Only export staged files older than ``max_age_days`` days when set.
        trashed_only : bool, optional
            When True, only export staged files inside the ``_trash`` subfolder.
        trash_fn : Callable[[str], Any] | None, optional
            Injectable ``send2trash`` callable for testing or custom Trash backends.
        audit_ledger : KeyAuditLedger | None, optional
            Optional audit ledger instance; defaults to ``KeyAuditLedger()``.

        Returns
        -------
        dict[str, Any]
            Summary containing ``exported``, ``kept_staged``, and ``errors``.
        """
        from app.core.ledger import KeyAuditLedger
        from app.core.mover import (
            DUPLICATE_STAGING_DIR,
            _prune_emptied_dirs,
            duplicate_staging_dir,
        )
        from app.core.resilient_file_ops import send_to_os_trash

        target_sessions: list[tuple[str, str]] = []
        if session_id:
            resolved_base = base_dir
            if not resolved_base:
                conn = get_db_connection(self.db_path)
                with conn:
                    row = conn.execute(
                        "SELECT base_dir FROM sessions WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                resolved_base = row[0] if row else None
            if resolved_base:
                target_sessions.append((session_id, resolved_base))
        elif base_dir:
            dup_root = os.path.join(base_dir, DUPLICATE_STAGING_DIR)
            if os.path.isdir(dup_root) and not os.path.islink(dup_root):
                for entry_name in sorted(os.listdir(dup_root)):
                    cand = os.path.join(dup_root, entry_name)
                    if os.path.isdir(cand) and not os.path.islink(cand):
                        target_sessions.append((entry_name, base_dir))

        ledger = audit_ledger or KeyAuditLedger()
        exported_paths: list[str] = []
        kept_staged_paths: list[str] = []
        errors: dict[str, str] = {}
        now = time.time()

        for sid, b_dir in target_sessions:
            stage_root = duplicate_staging_dir(b_dir, sid)
            scan_root = (
                os.path.join(stage_root, "_trash") if trashed_only else stage_root
            )
            if not os.path.isdir(scan_root) or os.path.islink(scan_root):
                continue

            move_by_dst: dict[str, Move] = {}
            for m in self.moves.list_moves(sid):
                if m.dst:
                    move_by_dst[os.path.normcase(os.path.abspath(m.dst))] = m

            flushed_seqs: list[int] = []
            dirs_to_prune: set[str] = set()

            for root, _dirs, files in os.walk(scan_root):
                for fname in sorted(files):
                    fpath = os.path.normpath(os.path.abspath(os.path.join(root, fname)))
                    if max_age_days is not None:
                        try:
                            age_days = (now - os.stat(fpath).st_mtime) / 86400.0
                            if age_days < max_age_days:
                                kept_staged_paths.append(fpath)
                                continue
                        except OSError:
                            continue

                    matched_move = move_by_dst.get(os.path.normcase(fpath))
                    orig_src = matched_move.src if matched_move else ""
                    sha_val = (matched_move.src_sha256 or "") if matched_move else ""

                    ok, err_reason = send_to_os_trash(fpath, trash_fn=trash_fn)
                    if ok:
                        exported_paths.append(fpath)
                        dirs_to_prune.add(os.path.dirname(fpath))
                        if matched_move is not None:
                            flushed_seqs.append(matched_move.seq)
                        logging.info(
                            "Irreversible OS Trash handoff: exported staged duplicate '%s' (original source: '%s', session: %s) to native OS Trash.",
                            fpath,
                            orig_src or "<unknown>",
                            sid,
                        )
                        ledger.log_trash_handoff(
                            session_id=sid,
                            staged_path=fpath,
                            original_source=orig_src,
                            sha256=sha_val,
                            status="EXPORTED_TO_OS_TRASH",
                            db_path=self.db_path,
                        )
                    else:
                        kept_staged_paths.append(fpath)
                        errors[fpath] = err_reason or "OS Trash unavailable"
                        ledger.log_trash_handoff(
                            session_id=sid,
                            staged_path=fpath,
                            original_source=orig_src,
                            sha256=sha_val,
                            status="KEPT_STAGED_FALLBACK",
                            reason=err_reason,
                            db_path=self.db_path,
                        )

            if flushed_seqs:

                def _delete_flushed_rows(
                    target_sid: str = sid, seqs: list[int] = list(flushed_seqs)
                ) -> None:
                    conn = get_db_connection(self.db_path)
                    placeholders = ",".join("?" for _ in seqs)
                    with conn:
                        conn.execute(
                            f"DELETE FROM moves WHERE session_id = ? AND seq IN ({placeholders})",
                            [target_sid, *seqs],
                        )

                self.db.worker.execute_write(_delete_flushed_rows)

            if dirs_to_prune:
                _prune_emptied_dirs(dirs_to_prune, b_dir)
                dup_parent = os.path.join(b_dir, DUPLICATE_STAGING_DIR)
                if os.path.isdir(dup_parent) and not os.listdir(dup_parent):
                    with contextlib.suppress(OSError):
                        os.rmdir(dup_parent)

        return {
            "exported": exported_paths,
            "kept_staged": kept_staged_paths,
            "errors": errors,
        }

    @staticmethod
    def simulate_rollback_steps(
        steps_to_analyze: Sequence[dict[str, Any]],
        *,
        session_id: Optional[str] = None,
        ignore_missing: bool = False,
    ) -> dict[str, Any]:
        """Simulate pre-flight rollback outcomes for a sequence of move step records."""
        from app.core.mover import get_safe_path
        from app.core.resilient_file_ops import same_file_content

        analyzed_steps = []
        clean_count = 0
        identical_count = 0
        collision_count = 0
        missing_count = 0

        for step in steps_to_analyze:
            step_id = step.get("step_id")
            source_path = step.get("source_path") or step.get("original_path") or ""
            target_path = step.get("target_path") or ""
            size = step.get("size")

            target_exists = bool(target_path and os.path.lexists(target_path))
            source_exists = bool(source_path and os.path.lexists(source_path))

            if not target_exists:
                st = "MISSING_TARGET"
                is_coll = False
                restoration_path = source_path
                det = "Sorted target file is missing on disk"
                missing_count += 1
            elif source_exists:
                if same_file_content(source_path, target_path):
                    st = "IDENTICAL_EXISTS"
                    is_coll = False
                    restoration_path = source_path
                    det = "Identical copy exists at original path; sorted file will be removed"
                    identical_count += 1
                else:
                    st = "COLLISION"
                    is_coll = True
                    dest_dir = os.path.dirname(source_path) or "."
                    orig_filename = os.path.basename(source_path)
                    restoration_path = os.path.normpath(
                        get_safe_path(dest_dir, orig_filename, target_path)
                    )
                    det = f"Original path is occupied by a different file; will restore as {os.path.basename(restoration_path)}"
                    collision_count += 1
            else:
                st = "CLEAN"
                is_coll = False
                restoration_path = source_path
                det = "Ready for clean restoration"
                clean_count += 1

            if size is None and target_exists:
                try:
                    size = os.path.getsize(target_path)
                except OSError:
                    size = None

            analyzed_steps.append(
                {
                    "step_id": step_id,
                    "source_path": source_path,
                    "target_path": target_path,
                    "restoration_path": restoration_path,
                    "status": st,
                    "collision": is_coll,
                    "size": size,
                    "detail": det,
                }
            )

        total = len(analyzed_steps)
        return {
            "session_id": session_id,
            "total_operations": total,
            "summary": {
                "total": total,
                "clean": clean_count,
                "identical_exists": identical_count,
                "collision": collision_count,
                "missing_target": missing_count,
            },
            "has_collisions": collision_count > 0,
            "has_missing": missing_count > 0,
            "ignore_missing": ignore_missing,
            "steps": analyzed_steps,
        }

    def preview_rollback(
        self,
        session_id: str,
        selected_step_ids: Optional[Collection[int | str]] = None,
        ignore_missing: bool = False,
    ) -> dict[str, Any]:
        """Simulate pre-flight session rollback without making any disk modifications."""
        all_moves = self.get_session_moves(session_id)
        if selected_step_ids is not None:
            sel_set = {str(x) for x in selected_step_ids}
            steps_to_analyze = [
                m for m in all_moves if str(m.get("step_id")) in sel_set
            ]
        else:
            steps_to_analyze = list(all_moves)

        return self.simulate_rollback_steps(
            steps_to_analyze,
            session_id=session_id,
            ignore_missing=ignore_missing,
        )


@contextmanager
def open_session_history(store=None) -> Generator[HistoryManager]:
    """Yield shared history services without taking ownership of their writer."""
    from app.core.services import acquire_services

    with acquire_services(store) as services:
        yield services.history
