"""The move ledger: one append-only ``moves`` table per database (ADR 0006).

Each move a sort makes is one row, numbered per session by ``seq``. The mover
records an ``intent`` row and waits for it to commit before it touches the
filesystem, then moves the row to ``done`` (with the final destination and any
file it displaced, ADR 0005), ``verified`` once the destination's hash matches
the source's, or ``failed``. Writes go through the database's single writer
(ADR 0003) and do no file I/O.
"""

import hashlib
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from app.core.db_conn import DB_ERRORS, get_db_connection
from app.core.lifecycle import LifecycleTelemetryMixin

logger = logging.getLogger(__name__)

INTENT = "intent"
DONE = "done"
VERIFIED = "verified"
FAILED = "failed"
EXPORTED_TO_TRASH = "exported_to_trash"
MOVE_STATES = (INTENT, DONE, VERIFIED, FAILED, EXPORTED_TO_TRASH)

STATUS_PENDING = "PENDING"
STATUS_ABORTED = "ABORTED"
STATUS_FAILED_CONFLICT = "FAILED_CONFLICT"
STATUS_FAILED_MISSING = "FAILED_MISSING"
STATUS_FAILED = "FAILED"

FAILURE_REASONS = {
    STATUS_ABORTED: (
        "Move aborted prior to physical file move; source file preserved at origin."
    ),
    STATUS_FAILED_CONFLICT: (
        "Destination file exists and content differs from source; both files preserved."
    ),
    STATUS_FAILED_MISSING: "Neither source nor destination file exists.",
}

_STRUCTURED_FAILURE_PREFIXES = (
    STATUS_ABORTED,
    STATUS_FAILED_CONFLICT,
    STATUS_FAILED_MISSING,
)

# The states each state may follow. An intent row stays an intent while its
# displacement is recorded.
_ALLOWED_FROM = {
    INTENT: (INTENT,),
    DONE: (INTENT,),
    VERIFIED: (DONE,),
    FAILED: (INTENT,),
    EXPORTED_TO_TRASH: (DONE, VERIFIED),
}

# Batch completion commits a move's post-chunk terminal state in one write, so a
# verified move transitions directly from ``intent`` (or ``done``) to ``verified``.
_BATCH_ALLOWED_FROM = {
    INTENT: (INTENT,),
    DONE: (INTENT, DONE),
    VERIFIED: (INTENT, DONE, VERIFIED),
    FAILED: (INTENT, FAILED),
    EXPORTED_TO_TRASH: (DONE, VERIFIED, EXPORTED_TO_TRASH),
}


class _Writer(Protocol):
    def execute_write(self, func: Callable, *args, **kwargs): ...


@dataclass(frozen=True)
class Move:
    """One row of the move ledger."""

    session_id: str
    seq: int
    src: str
    dst: str
    src_sha256: str | None
    size: int | None
    displaced: str | None
    state: str
    error: str | None
    created_at: float | None
    updated_at: float | None


_COLUMNS = (
    "session_id, seq, src, dst, src_sha256, size, displaced, state, error, "
    "created_at, updated_at"
)


def format_move_failure(status_code: str, reason: str | None = None) -> str:
    """Encode a structured reconciliation or move failure code and reason for ``moves.error``."""
    resolved_reason = reason if reason is not None else FAILURE_REASONS.get(status_code)
    if status_code in _STRUCTURED_FAILURE_PREFIXES:
        return f"{status_code}: {resolved_reason}" if resolved_reason else status_code
    return resolved_reason or status_code


def decode_move_failure(error: str | None) -> tuple[str, str | None]:
    """Decode ``moves.error`` into a CLI-compatible ``(status, failure_reason)`` pair."""
    if not error:
        return STATUS_FAILED, None
    for code in _STRUCTURED_FAILURE_PREFIXES:
        prefix = f"{code}:"
        if error.startswith(prefix):
            reason = error[len(prefix) :].strip() or FAILURE_REASONS.get(code)
            return code, reason
        if error == code:
            return code, FAILURE_REASONS.get(code)
    return STATUS_FAILED, error


def create_moves_table(conn) -> None:
    """Create the ``moves`` table. For use inside a migration only."""
    conn.execute("""
        CREATE TABLE moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            src_sha256 TEXT,
            size INTEGER,
            displaced TEXT,
            state TEXT NOT NULL
                CHECK (state IN ('intent', 'done', 'verified', 'failed', 'exported_to_trash')),
            error TEXT,
            created_at REAL,
            updated_at REAL,
            UNIQUE (session_id, seq)
        )
    """)
    conn.execute("CREATE INDEX idx_moves_session ON moves(session_id)")


def import_step_ledger(conn) -> int:
    """Copy ``step_ledger`` rows into ``moves`` as ``done``, for sessions with no moves yet.

    Each session's steps keep their order. Returns the number of rows copied.
    Runs inside the caller's transaction.
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info("step_ledger")')}
    if not columns:
        return 0
    cursor = conn.execute(
        """
        INSERT INTO moves (
            session_id, seq, src, dst, src_sha256, size, displaced, state,
            error, created_at, updated_at
        )
        SELECT session_id,
               ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY step_id),
               COALESCE(source_path, original_path),
               target_path,
               file_hash,
               NULL, NULL, 'done', NULL, timestamp, timestamp
        FROM step_ledger
        WHERE session_id IS NOT NULL
          AND COALESCE(source_path, original_path) IS NOT NULL
          AND target_path IS NOT NULL
          AND session_id NOT IN (SELECT DISTINCT session_id FROM moves)
        ORDER BY session_id, step_id
        """
    )
    return max(cursor.rowcount, 0)


def _resolve_legacy_path(base_dir: str | None, path_value: str) -> str:
    if base_dir and not os.path.isabs(path_value):
        return os.path.normpath(os.path.join(base_dir, path_value))
    return path_value


def import_sidecar_ledger(src_conn, dst_conn) -> int:
    """Copy sidecar ``transaction_ledger`` rows from ``src_conn`` into ``dst_conn.moves``.

    Only sessions that do not already have rows in ``dst_conn.moves`` are imported.
    Returns the number of rows inserted.
    """
    src_columns = {
        row[1] for row in src_conn.execute('PRAGMA table_info("transaction_ledger")')
    }
    if not {"session_id", "source_path", "dest_path", "status"} <= src_columns:
        return 0
    dst_moves_cols = {row[1] for row in dst_conn.execute('PRAGMA table_info("moves")')}
    if not dst_moves_cols:
        return 0

    existing_sessions = {
        row[0] for row in dst_conn.execute("SELECT DISTINCT session_id FROM moves")
    }
    has_base_dir = "base_dir" in src_columns
    has_current_dest = "current_dest" in src_columns
    has_hash = "file_hash" in src_columns
    has_created = "created_at" in src_columns
    has_updated = "updated_at" in src_columns

    dst_expr = "COALESCE(current_dest, dest_path)" if has_current_dest else "dest_path"
    select_sql = (
        "SELECT session_id, "
        f"{'base_dir' if has_base_dir else 'NULL'}, "
        f"source_path, {dst_expr}, "
        f"{'file_hash' if has_hash else 'NULL'}, "
        "status, "
        f"{'created_at' if has_created else 'NULL'}, "
        f"{'updated_at' if has_updated else 'NULL'} "
        "FROM transaction_ledger "
        "WHERE session_id IS NOT NULL AND source_path IS NOT NULL AND dest_path IS NOT NULL "
        f"ORDER BY session_id, {'created_at, ' if has_created else ''}rowid"
    )
    rows = src_conn.execute(select_sql).fetchall()
    if not rows:
        return 0

    seq_by_session: dict[str, int] = {}
    session_meta: dict[str, tuple[float, str, list[str]]] = {}
    to_insert: list[tuple[Any, ...]] = []

    for sid_raw, base_dir, src, dst, fhash, status_raw, created_at, updated_at in rows:
        sid = str(sid_raw)
        if sid in existing_sessions:
            continue
        seq = seq_by_session.get(sid, 0) + 1
        seq_by_session[sid] = seq

        status_upper = str(status_raw or "").upper()
        if status_upper == STATUS_PENDING:
            state = INTENT
            error = None
        elif status_upper in ("MOVED_PHYSICAL", "COMPLETED", "RECONCILED_SUCCESS"):
            state = DONE
            error = None
        elif status_upper in _STRUCTURED_FAILURE_PREFIXES:
            state = FAILED
            error = format_move_failure(status_upper)
        else:
            state = FAILED
            error = str(status_raw) if status_raw else None

        ts = float(created_at if created_at is not None else time.time())
        uts = float(updated_at if updated_at is not None else ts)
        resolved_src = _resolve_legacy_path(base_dir, str(src))
        resolved_dst = _resolve_legacy_path(base_dir, str(dst))
        to_insert.append(
            (sid, seq, resolved_src, resolved_dst, fhash or None, state, error, ts, uts)
        )
        if sid not in session_meta:
            session_meta[sid] = (ts, str(base_dir or ""), [state])
        else:
            session_meta[sid][2].append(state)

    if not to_insert:
        return 0

    dst_conn.executemany(
        """
        INSERT INTO moves (
            session_id, seq, src, dst, src_sha256, size, displaced, state,
            error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
        """,
        to_insert,
    )

    has_sessions = bool(
        {row[1] for row in dst_conn.execute('PRAGMA table_info("sessions")')}
    )
    if has_sessions:
        for sid, (ts, bdir, states) in session_meta.items():
            if INTENT in states:
                sess_status = "active"
            elif all(st == FAILED for st in states):
                sess_status = "failed"
            else:
                sess_status = "completed"
            dst_conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, timestamp, base_dir, status) "
                "VALUES (?, ?, ?, ?)",
                (sid, ts, bdir, sess_status),
            )
    return len(to_insert)


def import_transaction_ledger(conn) -> int:
    """Copy legacy ``transaction_ledger`` rows in ``conn`` into ``moves``.

    Supports both the main-database ``transaction_ledger`` schema (with
    ``step_number`` and ``destination_path``) and the sidecar schema (with
    ``dest_path`` and ``status``), importing only sessions that have no rows in
    ``moves`` yet.
    """
    columns = {
        row[1] for row in conn.execute('PRAGMA table_info("transaction_ledger")')
    }
    if not columns:
        return 0

    if "dest_path" in columns and "status" in columns:
        return import_sidecar_ledger(conn, conn)

    if not {"session_id", "source_path", "destination_path"} <= columns:
        return 0

    existing_sessions = {
        row[0] for row in conn.execute("SELECT DISTINCT session_id FROM moves")
    }
    has_step = "step_number" in columns
    has_id = "id" in columns
    has_base_dir = "base_dir" in columns
    has_hash = "file_hash" in columns
    has_ts = "timestamp" in columns
    has_item_type = "item_type" in columns

    order_parts = ["session_id"]
    if has_step:
        order_parts.append("step_number")
    if has_id:
        order_parts.append("id")
    else:
        order_parts.append("rowid")

    where_type = "AND COALESCE(item_type, 'file') = 'file' " if has_item_type else ""
    rows = conn.execute(
        f"SELECT session_id, "
        f"{'base_dir' if has_base_dir else 'NULL'}, "
        f"source_path, destination_path, "
        f"{'file_hash' if has_hash else 'NULL'}, "
        f"{'timestamp' if has_ts else 'NULL'} "
        f"FROM transaction_ledger "
        f"WHERE session_id IS NOT NULL "
        f"  AND source_path IS NOT NULL "
        f"  AND destination_path IS NOT NULL "
        f"  {where_type}"
        f"ORDER BY {', '.join(order_parts)}"
    ).fetchall()
    if not rows:
        return 0

    seq_by_session: dict[str, int] = {}
    session_meta: dict[str, tuple[float, str]] = {}
    to_insert: list[tuple[Any, ...]] = []

    for sid_raw, base_dir, src_rel, dst_rel, fhash, ts_raw in rows:
        sid = str(sid_raw)
        if sid in existing_sessions:
            continue
        seq = seq_by_session.get(sid, 0) + 1
        seq_by_session[sid] = seq
        ts = float(ts_raw if ts_raw is not None else time.time())
        resolved_src = _resolve_legacy_path(base_dir, str(src_rel))
        resolved_dst = _resolve_legacy_path(base_dir, str(dst_rel))
        to_insert.append((sid, seq, resolved_src, resolved_dst, fhash or None, ts, ts))
        if sid not in session_meta:
            session_meta[sid] = (ts, str(base_dir or ""))

    if not to_insert:
        return 0

    conn.executemany(
        """
        INSERT INTO moves (
            session_id, seq, src, dst, src_sha256, size, displaced, state,
            error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 'done', NULL, ?, ?)
        """,
        to_insert,
    )

    has_sessions = bool(
        {row[1] for row in conn.execute('PRAGMA table_info("sessions")')}
    )
    if has_sessions:
        for sid, (ts, bdir) in session_meta.items():
            conn.execute(
                "INSERT OR IGNORE INTO sessions (session_id, timestamp, base_dir, status) "
                "VALUES (?, ?, ?, 'completed')",
                (sid, ts, bdir),
            )
    return len(to_insert)


def migrate_add_moves(conn) -> None:
    """Migration: add the ``moves`` table and import the old step ledger into it."""
    create_moves_table(conn)
    import_step_ledger(conn)


def migrate_import_legacy_moves(conn) -> None:
    """Migration: import remaining historical ``step_ledger`` and ``transaction_ledger`` rows into ``moves``."""
    import_step_ledger(conn)
    import_transaction_ledger(conn)


def migrate_moves_exported_to_trash(conn) -> None:
    """Migration 4: widen ``moves.state`` CHECK constraint to include ``exported_to_trash``."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='moves'"
    ).fetchone()
    if not row:
        create_moves_table(conn)
        return
    ddl = str(row[0] or "")
    if "exported_to_trash" in ddl:
        return
    conn.execute("DROP INDEX IF EXISTS idx_moves_session")
    conn.execute("ALTER TABLE moves RENAME TO _moves_v3_old")
    create_moves_table(conn)
    conn.execute(
        """
        INSERT INTO moves (
            id, session_id, seq, src, dst, src_sha256, size, displaced,
            state, error, created_at, updated_at
        )
        SELECT id, session_id, seq, src, dst, src_sha256, size, displaced,
               state, error, created_at, updated_at
        FROM _moves_v3_old
        ORDER BY id
        """
    )
    conn.execute("DROP TABLE _moves_v3_old")


class SessionSummaryRow(NamedTuple):
    """Named summary tuple for one session's reversible and trashed move counts."""

    session_id: str
    timestamp: float | None
    base_dir: str | None
    status: str | None
    step_count: int
    file_count: int
    trashed_count: int


def query_session_summaries(db_path: str) -> list[SessionSummaryRow]:
    """Return ``SessionSummaryRow`` items newest first, separating reversible steps from OS Trash exports."""
    conn = get_db_connection(db_path)
    query = (
        "SELECT "
        "s.session_id, s.timestamp, s.base_dir, s.status, "
        "COALESCE(SUM(CASE WHEN m.state IN ('intent', 'done', 'verified', 'failed') THEN 1 ELSE 0 END), 0) AS step_count, "
        "COUNT(DISTINCT CASE WHEN m.state IN ('intent', 'done', 'verified', 'failed') THEN m.src END) AS file_count, "
        "COALESCE(SUM(CASE WHEN m.state = 'exported_to_trash' THEN 1 ELSE 0 END), 0) AS trashed_count "
        "FROM sessions s "
        "LEFT JOIN moves m ON s.session_id = m.session_id "
        "GROUP BY s.session_id "
        "ORDER BY s.timestamp DESC"
    )
    try:
        return [SessionSummaryRow(*r) for r in conn.execute(query).fetchall()]
    except DB_ERRORS:
        for legacy_query in (
            (
                "SELECT s.session_id, s.timestamp, s.base_dir, s.status, "
                "COUNT(m.step_id) AS step_count, "
                "COUNT(DISTINCT COALESCE(m.source_path, m.original_path)) AS file_count, "
                "0 AS trashed_count "
                "FROM sessions s "
                "LEFT JOIN step_ledger m ON s.session_id = m.session_id "
                "GROUP BY s.session_id ORDER BY s.timestamp DESC"
            ),
            (
                "SELECT s.session_id, s.timestamp, s.base_dir, s.status, "
                "COUNT(m.rowid) AS step_count, "
                "COUNT(DISTINCT m.source_path) AS file_count, "
                "0 AS trashed_count "
                "FROM sessions s "
                "LEFT JOIN transaction_ledger m ON s.session_id = m.session_id "
                "GROUP BY s.session_id ORDER BY s.timestamp DESC"
            ),
        ):
            try:
                return [
                    SessionSummaryRow(*r) for r in conn.execute(legacy_query).fetchall()
                ]
            except DB_ERRORS:
                continue
        fallback = (
            "SELECT s.session_id, s.timestamp, s.base_dir, s.status, "
            "0 AS step_count, 0 AS file_count, 0 AS trashed_count "
            "FROM sessions s ORDER BY s.timestamp DESC"
        )
        return [SessionSummaryRow(*r) for r in conn.execute(fallback).fetchall()]


def session_summary_dicts(
    db_path: str, *, history_db_path: str | None = None
) -> list[dict[str, Any]]:
    """Build standard session summary dicts from ``query_session_summaries``."""
    summaries: list[dict[str, Any]] = []
    for row in query_session_summaries(db_path):
        entry: dict[str, Any] = {
            "session_id": row.session_id,
            "timestamp": row.timestamp,
            "base_dir": row.base_dir,
            "status": row.status,
            "step_count": row.step_count,
            "file_count": row.file_count,
            "trashed_count": row.trashed_count,
        }
        if history_db_path is not None:
            entry["history_db_path"] = history_db_path
        summaries.append(entry)
    return summaries


def _read_legacy_moves_for_session(conn: Any, session_id: str) -> list[Move]:
    """Read legacy ``step_ledger`` or ``transaction_ledger`` rows without mutating ``conn``."""
    sid = str(session_id)
    step_cols = {row[1] for row in conn.execute('PRAGMA table_info("step_ledger")')}
    if step_cols and "session_id" in step_cols and "target_path" in step_cols:
        src_expr = (
            "COALESCE(source_path, original_path)"
            if "original_path" in step_cols
            else "source_path"
        )
        hash_expr = "file_hash" if "file_hash" in step_cols else "NULL"
        ts_expr = "timestamp" if "timestamp" in step_cols else "NULL"
        order_col = "step_id" if "step_id" in step_cols else "rowid"
        rows = conn.execute(
            f"SELECT {src_expr}, target_path, {hash_expr}, {ts_expr} "
            f"FROM step_ledger WHERE session_id = ? "
            f"AND {src_expr} IS NOT NULL AND target_path IS NOT NULL "
            f"ORDER BY {order_col} ASC",
            (sid,),
        ).fetchall()
        if rows:
            return [
                Move(
                    session_id=sid,
                    seq=idx,
                    src=str(src),
                    dst=str(dst),
                    src_sha256=fhash or None,
                    size=None,
                    displaced=None,
                    state=DONE,
                    error=None,
                    created_at=float(ts) if ts is not None else None,
                    updated_at=float(ts) if ts is not None else None,
                )
                for idx, (src, dst, fhash, ts) in enumerate(rows, start=1)
            ]

    tx_cols = {
        row[1] for row in conn.execute('PRAGMA table_info("transaction_ledger")')
    }
    if not tx_cols or "session_id" not in tx_cols or "source_path" not in tx_cols:
        return []

    if "dest_path" in tx_cols:
        dst_expr = (
            "COALESCE(current_dest, dest_path)"
            if "current_dest" in tx_cols
            else "dest_path"
        )
        bdir_expr = "base_dir" if "base_dir" in tx_cols else "NULL"
        hash_expr = "file_hash" if "file_hash" in tx_cols else "NULL"
        status_expr = "status" if "status" in tx_cols else "'COMPLETED'"
        created_expr = "created_at" if "created_at" in tx_cols else "NULL"
        updated_expr = "updated_at" if "updated_at" in tx_cols else "NULL"
        rows = conn.execute(
            f"SELECT {bdir_expr}, source_path, {dst_expr}, {hash_expr}, "
            f"{status_expr}, {created_expr}, {updated_expr} "
            f"FROM transaction_ledger WHERE session_id = ? "
            f"AND source_path IS NOT NULL AND dest_path IS NOT NULL "
            f"ORDER BY rowid ASC",
            (sid,),
        ).fetchall()
        legacy_moves: list[Move] = []
        for idx, (bdir, src, dst, fhash, status_raw, c_at, u_at) in enumerate(
            rows, start=1
        ):
            st_upper = str(status_raw or "").upper()
            if st_upper in (STATUS_PENDING, "INTENT"):
                st = INTENT
                err = None
            elif st_upper in ("MOVED_PHYSICAL", "COMPLETED", "RECONCILED_SUCCESS"):
                st = DONE
                err = None
            elif st_upper in _STRUCTURED_FAILURE_PREFIXES:
                st = FAILED
                err = format_move_failure(st_upper)
            else:
                st = FAILED
                err = str(status_raw) if status_raw else None
            legacy_moves.append(
                Move(
                    session_id=sid,
                    seq=idx,
                    src=_resolve_legacy_path(bdir, str(src)),
                    dst=_resolve_legacy_path(bdir, str(dst)),
                    src_sha256=fhash or None,
                    size=None,
                    displaced=None,
                    state=st,
                    error=err,
                    created_at=float(c_at) if c_at is not None else None,
                    updated_at=float(u_at) if u_at is not None else None,
                )
            )
        return legacy_moves

    if "destination_path" in tx_cols:
        bdir_expr = "base_dir" if "base_dir" in tx_cols else "NULL"
        hash_expr = "file_hash" if "file_hash" in tx_cols else "NULL"
        ts_expr = "timestamp" if "timestamp" in tx_cols else "NULL"
        order_col = "step_number" if "step_number" in tx_cols else "rowid"
        rows = conn.execute(
            f"SELECT {bdir_expr}, source_path, destination_path, {hash_expr}, {ts_expr} "
            f"FROM transaction_ledger WHERE session_id = ? "
            f"AND source_path IS NOT NULL AND destination_path IS NOT NULL "
            f"ORDER BY {order_col} ASC, rowid ASC",
            (sid,),
        ).fetchall()
        return [
            Move(
                session_id=sid,
                seq=idx,
                src=_resolve_legacy_path(bdir, str(src)),
                dst=_resolve_legacy_path(bdir, str(dst)),
                src_sha256=fhash or None,
                size=None,
                displaced=None,
                state=DONE,
                error=None,
                created_at=float(ts) if ts is not None else None,
                updated_at=float(ts) if ts is not None else None,
            )
            for idx, (bdir, src, dst, fhash, ts) in enumerate(rows, start=1)
        ]

    return []


class _InlineWriter:
    """Runs writes on the calling thread, for a database with no DBWorker."""

    def execute_write(self, func, *args, **kwargs):
        return func(*args, **kwargs)


class MoveLedger(LifecycleTelemetryMixin):
    """Records a session's moves in the ``moves`` table of one database.

    ``writer`` is the database's single writer (a ``DBWorker``); without one,
    writes run on the calling thread.
    """

    def __init__(
        self,
        db_path: str,
        writer: _Writer | None = None,
        *,
        auto_init: bool = False,
    ):
        super().__init__()
        self.db_path = db_path
        self._writer = writer or _InlineWriter()
        if auto_init:
            self._writer.execute_write(self._ensure_schema)

    def _ensure_schema(self) -> None:
        conn = get_db_connection(self.db_path)
        with conn:
            has_moves = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moves'"
            ).fetchone()
            if not has_moves:
                create_moves_table(conn)
            else:
                migrate_moves_exported_to_trash(conn)
            migrate_import_legacy_moves(conn)

    def record_intent(
        self,
        session_id: str,
        src: str,
        dst: str,
        src_sha256: str | None = None,
        size: int | None = None,
    ) -> int:
        """Record a move about to happen and return its ``seq``; committed on return."""
        if not session_id:
            raise ValueError("A move needs a session id.")
        return self._writer.execute_write(
            self._insert_intent, str(session_id), src, dst, src_sha256 or None, size
        )

    def record_intents(
        self,
        session_id: str,
        intents: Sequence[tuple[str, str, str | None, int | None]],
    ) -> list[int]:
        """Record a batch of pre-move ``intent`` rows in one commit and return their ``seq`` list."""
        if not session_id:
            raise ValueError("A move needs a session id.")
        items = list(intents)
        if not items:
            return []
        if "record_intent" in self.__dict__:
            return [
                self.record_intent(session_id, src, dst, src_sha256=sha, size=sz)
                for src, dst, sha, sz in items
            ]
        return self._writer.execute_write(self._insert_intents, str(session_id), items)

    def complete_moves(
        self,
        session_id: str,
        completions: Sequence[dict[str, Any]],
    ) -> None:
        """Apply a batch of post-move state transitions (``done``, ``verified``, ``failed``) in one commit."""
        items = list(completions)
        if not items:
            return
        if (
            "mark_done" in self.__dict__
            or "mark_verified" in self.__dict__
            or "mark_failed" in self.__dict__
            or "mark_exported_to_trash" in self.__dict__
        ):
            for item in items:
                sid = str(item.get("session_id") or session_id)
                seq = int(item["seq"])
                st = str(item["state"])
                if st == VERIFIED:
                    self.mark_done(
                        sid, seq, item.get("dst") or "", displaced=item.get("displaced")
                    )
                    self.mark_verified(sid, seq)
                elif st == DONE:
                    self.mark_done(
                        sid, seq, item.get("dst") or "", displaced=item.get("displaced")
                    )
                elif st == FAILED:
                    self.mark_failed(sid, seq, error=item.get("error"))
                elif st == EXPORTED_TO_TRASH:
                    self.mark_exported_to_trash(sid, seq)
            return
        self._writer.execute_write(self._complete_batch, str(session_id), items)

    def mark_done(
        self, session_id: str, seq: int, dst: str, displaced: str | None = None
    ) -> None:
        """Record that the move finished at ``dst``, having moved ``displaced`` aside."""
        self._change_state(session_id, seq, DONE, dst=dst, displaced=displaced)

    def record_displacement(self, session_id: str, seq: int, displaced: str) -> None:
        """Record, before it happens, where the file at the move's ``dst`` is moved aside to."""
        self._change_state(session_id, seq, INTENT, displaced=displaced)

    def mark_verified(self, session_id: str, seq: int) -> None:
        """Record that the file at the move's destination has the source's hash."""
        self._change_state(session_id, seq, VERIFIED)

    def mark_failed(self, session_id: str, seq: int, error: str | None = None) -> None:
        """Record that the move did not happen."""
        self._change_state(session_id, seq, FAILED, error=error)

    def mark_exported_to_trash(self, session_id: str, seq: int) -> None:
        """Record that a verified staged duplicate was handed off to native OS Trash."""
        self._change_state(session_id, seq, EXPORTED_TO_TRASH)

    def list_moves(self, session_id: str) -> list[Move]:
        """Return the session's moves in the order they were recorded, with read-only legacy fallback."""
        conn = get_db_connection(self.db_path)
        with conn:
            try:
                rows = conn.execute(
                    f"SELECT {_COLUMNS} FROM moves WHERE session_id = ? ORDER BY seq",
                    (str(session_id),),
                ).fetchall()
            except DB_ERRORS:
                rows = []
            if rows:
                return [Move(*row) for row in rows]
            try:
                return _read_legacy_moves_for_session(conn, session_id)
            except DB_ERRORS:
                return []

    def get_pending_entries(
        self,
        session_id: str | None = None,
        include_failed: bool = True,
        failed_only: bool = False,
        aborted_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve incomplete (``intent``) and/or ``failed`` move entries."""
        conn = get_db_connection(self.db_path)
        has_moves = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moves'"
            ).fetchone()
        )
        rows: list[Any] = []
        if has_moves:
            has_sessions = bool(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
                ).fetchone()
            )
            join_sql = (
                "LEFT JOIN sessions s ON m.session_id = s.session_id"
                if has_sessions
                else ""
            )
            bdir_col = "COALESCE(s.base_dir, '')" if has_sessions else "''"

            if aborted_only or failed_only:
                state_clause = "m.state = 'failed'"
            elif include_failed:
                state_clause = "m.state IN ('intent', 'failed')"
            else:
                state_clause = "m.state = 'intent'"

            params: list[Any] = []
            session_clause = ""
            if session_id is not None:
                session_clause = " AND m.session_id = ?"
                params.append(str(session_id))

            rows = conn.execute(
                f"""
                SELECT m.session_id, m.seq, m.src, m.dst, m.src_sha256,
                       m.displaced, m.state, m.error, m.created_at, m.updated_at,
                       {bdir_col} AS base_dir
                FROM moves m
                {join_sql}
                WHERE {state_clause}{session_clause}
                ORDER BY m.created_at ASC, m.id ASC
                """,
                params,
            ).fetchall()

        if not rows:
            tx_cols = {
                r[1] for r in conn.execute('PRAGMA table_info("transaction_ledger")')
            }
            if tx_cols and "session_id" in tx_cols and "source_path" in tx_cols:
                dst_col = (
                    "COALESCE(current_dest, dest_path)"
                    if "current_dest" in tx_cols and "dest_path" in tx_cols
                    else ("dest_path" if "dest_path" in tx_cols else "destination_path")
                )
                bdir_expr = "COALESCE(base_dir, '')" if "base_dir" in tx_cols else "''"
                hash_expr = "file_hash" if "file_hash" in tx_cols else "NULL"
                status_expr = "status" if "status" in tx_cols else "'PENDING'"
                created_expr = "created_at" if "created_at" in tx_cols else "NULL"
                updated_expr = "updated_at" if "updated_at" in tx_cols else "NULL"
                tx_params: list[Any] = []
                tx_where = ""
                if session_id is not None:
                    tx_where = " WHERE session_id = ?"
                    tx_params.append(str(session_id))
                tx_rows = conn.execute(
                    f"""
                    SELECT session_id, rowid, source_path, {dst_col}, {hash_expr},
                           {status_expr}, {created_expr}, {updated_expr}, {bdir_expr}
                    FROM transaction_ledger{tx_where}
                    ORDER BY rowid ASC
                    """,
                    tx_params,
                ).fetchall()
                for sid, rid, src, dst, fhash, st_raw, c_at, u_at, bdir in tx_rows:
                    st_up = str(st_raw or "").upper()
                    if st_up in (STATUS_PENDING, "INTENT"):
                        mapped_state = INTENT
                        mapped_err = None
                    elif st_up in ("MOVED_PHYSICAL", "COMPLETED", "RECONCILED_SUCCESS"):
                        continue
                    elif st_up in _STRUCTURED_FAILURE_PREFIXES:
                        mapped_state = FAILED
                        mapped_err = format_move_failure(st_up)
                    else:
                        mapped_state = FAILED
                        mapped_err = str(st_raw) if st_raw else None
                    if (aborted_only or failed_only) and mapped_state != FAILED:
                        continue
                    if not include_failed and mapped_state != INTENT:
                        continue
                    rows.append(
                        (
                            sid,
                            int(rid),
                            _resolve_legacy_path(bdir, str(src or "")),
                            _resolve_legacy_path(bdir, str(dst or "")),
                            fhash,
                            None,
                            mapped_state,
                            mapped_err,
                            c_at,
                            u_at,
                            bdir or "",
                        )
                    )

        results: list[dict[str, Any]] = []
        for (
            sid,
            seq,
            src,
            dst,
            src_sha256,
            displaced,
            state,
            error,
            created_at,
            updated_at,
            base_dir,
        ) in rows:
            if state == INTENT:
                status = STATUS_PENDING
                reason = None
            else:
                status, reason = decode_move_failure(error)
                if aborted_only and status != STATUS_ABORTED:
                    continue

            src_rel = src
            dst_rel = dst
            if base_dir:
                try:
                    src_rel = os.path.relpath(src, base_dir)
                    dst_rel = os.path.relpath(dst, base_dir)
                except ValueError:
                    pass

            results.append(
                {
                    "entry_id": f"{sid}:{seq}",
                    "session_id": sid,
                    "seq": int(seq),
                    "base_dir": base_dir,
                    "source_path": src,
                    "dest_path": dst,
                    "source_rel_path": src_rel,
                    "dest_rel_path": dst_rel,
                    "displaced": displaced,
                    "file_hash": src_sha256 or "",
                    "status": status,
                    "failure_reason": reason,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }
            )
        return results

    def reconcile_incomplete_transactions(self, db: Any = None) -> int:
        """Resolve in-flight ``intent`` rows in ``moves`` by inspecting filesystem paths and hashes."""
        from app.core.resilient_file_ops import (
            resilient_move,
            resilient_remove,
            same_file_content,
        )

        def _sha256_file(filepath: str) -> str:
            digest = hashlib.sha256()
            with open(filepath, "rb") as fh:
                while chunk := fh.read(65536):
                    digest.update(chunk)
            return digest.hexdigest()

        incomplete = self.get_pending_entries(include_failed=False)
        if not incomplete:
            return 0

        self.emit_telemetry(
            "reconciliation_start",
            {"total_incomplete": len(incomplete), "timestamp": time.time()},
        )

        logger.info(
            "Automated headless reconciliation scanning %d incomplete move(s)...",
            len(incomplete),
        )
        reconciled_count = 0
        batch_size = 50
        pending_completions: list[dict[str, Any]] = []
        pending_db_updates: list[dict[str, Any]] = []

        def _flush_reconcile_batch() -> None:
            if not pending_completions and not pending_db_updates:
                return
            completions_to_commit = list(pending_completions)
            db_updates_to_commit = list(pending_db_updates)
            pending_completions.clear()
            pending_db_updates.clear()

            can_coalesce = (
                db is not None
                and hasattr(db, "execute_batch_updates")
                and "execute_batch_updates" not in getattr(db, "__dict__", {})
                and hasattr(db, "db_path")
                and os.path.abspath(str(db.db_path)) == os.path.abspath(self.db_path)
                and "mark_done" not in self.__dict__
                and "mark_verified" not in self.__dict__
                and "mark_failed" not in self.__dict__
            )
            if can_coalesce:
                try:
                    db.execute_batch_updates(
                        db_updates_to_commit,
                        extra_conn_ops=lambda conn: self._apply_completions_on_conn(
                            conn, "", completions_to_commit
                        ),
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - fall back to independent commits if DB sync fails
                    logger.warning(
                        "Coalesced reconciliation batch failed (%s); retrying ledger update separately.",
                        exc,
                    )

            if db and db_updates_to_commit:
                if hasattr(db, "execute_batch_updates"):
                    try:
                        db.execute_batch_updates(db_updates_to_commit)
                    except Exception as exc:  # noqa: BLE001 - best-effort DB sync on crash recovery
                        logger.warning(
                            "Reconciliation batch DB roll-forward failed: %s", exc
                        )
                else:
                    for upd in db_updates_to_commit:
                        try:
                            if upd["type"] == "verified_target" and hasattr(
                                db, "set_user_verified_target"
                            ):
                                db.set_user_verified_target(*upd["args"])
                            elif upd["type"] == "document_path" and hasattr(
                                db, "update_document_path"
                            ):
                                if upd.get("clear_verified_target"):
                                    try:
                                        db.update_document_path(
                                            *upd["args"], clear_verified_target=True
                                        )
                                    except TypeError:
                                        db.update_document_path(*upd["args"])
                                else:
                                    db.update_document_path(*upd["args"])
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Reconciliation DB update skipped: %s", exc)

            if completions_to_commit:
                self.complete_moves("", completions_to_commit)

        for entry in incomplete:
            entry_id = entry["entry_id"]
            sid = entry["session_id"]
            seq = entry["seq"]
            base_dir = entry["base_dir"]
            src = entry["source_path"]
            dst = entry["dest_path"]
            src_rel = entry["source_rel_path"]
            dst_rel = entry["dest_rel_path"]
            displaced = entry.get("displaced")
            expected_hash = entry.get("file_hash") or None

            src_exists = os.path.exists(src) or os.path.islink(src)
            dst_exists = os.path.exists(dst) or os.path.islink(dst)

            if dst_exists and not src_exists:
                hash_ok = True
                verified = False
                if expected_hash and not os.path.islink(dst) and os.path.isfile(dst):
                    try:
                        actual_hash = _sha256_file(dst)
                    except OSError:
                        actual_hash = None
                    if actual_hash == expected_hash:
                        verified = True
                    else:
                        hash_ok = False

                if not hash_ok:
                    pending_completions.append(
                        {
                            "session_id": sid,
                            "seq": seq,
                            "state": FAILED,
                            "error": format_move_failure(
                                STATUS_FAILED_CONFLICT,
                                "Destination file content does not match recorded source hash; file preserved.",
                            ),
                        }
                    )
                else:
                    if db and base_dir:
                        raw_folder = (
                            os.path.dirname(dst_rel).replace("\\", "/").strip("/")
                        )
                        target_folder = (
                            raw_folder if raw_folder and raw_folder != "." else None
                        )
                        if expected_hash:
                            pending_db_updates.append(
                                {
                                    "type": "verified_target",
                                    "args": (base_dir, expected_hash, target_folder),
                                }
                            )
                        pending_db_updates.append(
                            {
                                "type": "document_path",
                                "args": (base_dir, src_rel, dst_rel),
                            }
                        )
                    pending_completions.append(
                        {
                            "session_id": sid,
                            "seq": seq,
                            "state": VERIFIED if verified else DONE,
                            "dst": dst,
                            "displaced": displaced,
                        }
                    )
                reconciled_count += 1

            elif src_exists and not dst_exists:
                if displaced and (
                    os.path.exists(displaced) or os.path.islink(displaced)
                ):
                    try:
                        resilient_move(displaced, dst)
                    except OSError as exc:
                        logger.warning(
                            "Could not restore displaced file %s -> %s during reconciliation: %s",
                            displaced,
                            dst,
                            exc,
                        )
                if db and base_dir:
                    pending_db_updates.append(
                        {
                            "type": "document_path",
                            "args": (base_dir, src_rel, src_rel),
                            "clear_verified_target": True,
                        }
                    )
                pending_completions.append(
                    {
                        "session_id": sid,
                        "seq": seq,
                        "state": FAILED,
                        "error": format_move_failure(STATUS_ABORTED),
                    }
                )
                reconciled_count += 1

            elif src_exists and dst_exists:
                if same_file_content(src, dst):
                    try:
                        if os.path.samefile(src, dst):
                            os.unlink(dst)
                        else:
                            resilient_remove(dst)
                        if db and base_dir:
                            pending_db_updates.append(
                                {
                                    "type": "document_path",
                                    "args": (base_dir, src_rel, src_rel),
                                    "clear_verified_target": True,
                                }
                            )
                        pending_completions.append(
                            {
                                "session_id": sid,
                                "seq": seq,
                                "state": FAILED,
                                "error": format_move_failure(
                                    STATUS_ABORTED,
                                    "Incomplete move left duplicate destination; duplicate removed and source preserved.",
                                ),
                            }
                        )
                    except OSError as exc:
                        logger.warning(
                            "Could not remove duplicate destination during reconciliation: %s",
                            exc,
                        )
                        pending_completions.append(
                            {
                                "session_id": sid,
                                "seq": seq,
                                "state": FAILED,
                                "error": format_move_failure(
                                    STATUS_FAILED_CONFLICT,
                                    f"Reconciliation conflict for {entry_id}: duplicate destination could not be removed: {exc}",
                                ),
                            }
                        )
                else:
                    logger.warning(
                        "Reconciliation conflict for %s: %s differs from %s; keeping both files.",
                        entry_id,
                        dst,
                        src,
                    )
                    if db and base_dir:
                        pending_db_updates.append(
                            {
                                "type": "document_path",
                                "args": (base_dir, src_rel, src_rel),
                                "clear_verified_target": True,
                            }
                        )
                    pending_completions.append(
                        {
                            "session_id": sid,
                            "seq": seq,
                            "state": FAILED,
                            "error": format_move_failure(STATUS_FAILED_CONFLICT),
                        }
                    )
                reconciled_count += 1

            else:
                pending_completions.append(
                    {
                        "session_id": sid,
                        "seq": seq,
                        "state": FAILED,
                        "error": format_move_failure(STATUS_FAILED_MISSING),
                    }
                )
                reconciled_count += 1

            if len(pending_completions) >= batch_size:
                _flush_reconcile_batch()

            self.emit_telemetry(
                "reconciliation_progress",
                {
                    "entry_id": entry_id,
                    "reconciled_count": reconciled_count,
                    "total_incomplete": len(incomplete),
                    "timestamp": time.time(),
                },
            )

        _flush_reconcile_batch()

        self.emit_telemetry(
            "reconciliation_finish",
            {
                "reconciled_count": reconciled_count,
                "total_incomplete": len(incomplete),
                "timestamp": time.time(),
            },
        )
        logger.info(
            "Headless reconciliation complete. %d entries reconciled.", reconciled_count
        )
        return reconciled_count

    def purge_session(self, session_id: str) -> int:
        """Delete all move records for ``session_id``."""
        return self._writer.execute_write(
            self._delete_where, "session_id = ?", (str(session_id),)
        )

    def purge_completed(self, session_id: str | None = None) -> int:
        """Delete completed (``done``, ``verified``, and ``exported_to_trash``) move records."""
        if session_id is not None:
            return self._writer.execute_write(
                self._delete_where,
                "state IN ('done', 'verified', 'exported_to_trash') AND session_id = ?",
                (str(session_id),),
            )
        return self._writer.execute_write(
            self._delete_where,
            "state IN ('done', 'verified', 'exported_to_trash')",
            (),
        )

    def purge_failed(
        self, session_id: str | None = None, status_filter: str | None = None
    ) -> int:
        """Delete ``failed`` move records, optionally restricted by session or failure code."""
        clauses = ["state = 'failed'"]
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(str(session_id))
        if status_filter == STATUS_ABORTED:
            clauses.append("(error = ? OR error LIKE ?)")
            params.extend([STATUS_ABORTED, f"{STATUS_ABORTED}:%"])
        elif status_filter:
            clauses.append("(error = ? OR error LIKE ?)")
            params.extend([status_filter, f"{status_filter}:%"])
        return self._writer.execute_write(
            self._delete_where, " AND ".join(clauses), tuple(params)
        )

    def _delete_where(self, where_sql: str, params: tuple[Any, ...]) -> int:
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(f"DELETE FROM moves WHERE {where_sql}", params)
        return max(cursor.rowcount, 0)

    def _insert_intent(self, session_id, src, dst, src_sha256, size) -> int:
        now = time.time()
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO moves (
                    session_id, seq, src, dst, src_sha256, size, state,
                    created_at, updated_at
                )
                SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, 'intent', ?, ?
                FROM moves WHERE session_id = ?
                """,
                (session_id, src, dst, src_sha256, size, now, now, session_id),
            )
            row = conn.execute(
                "SELECT seq FROM moves WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return int(row[0])

    def _insert_intents(
        self,
        session_id: str,
        items: list[tuple[str, str, str | None, int | None]],
    ) -> list[int]:
        now = time.time()
        conn = get_db_connection(self.db_path)
        with conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM moves WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            base_seq = int(row[0]) if row else 0
            seqs: list[int] = []
            rows_to_insert: list[tuple[Any, ...]] = []
            for offset, (src, dst, src_sha256, size) in enumerate(items, start=1):
                seq = base_seq + offset
                seqs.append(seq)
                rows_to_insert.append(
                    (session_id, seq, src, dst, src_sha256 or None, size, now, now)
                )
            conn.executemany(
                """
                INSERT INTO moves (
                    session_id, seq, src, dst, src_sha256, size, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'intent', ?, ?)
                """,
                rows_to_insert,
            )
        return seqs

    def _complete_batch(
        self, session_id: str, completions: list[dict[str, Any]]
    ) -> None:
        conn = get_db_connection(self.db_path)
        with conn:
            self._apply_completions_on_conn(conn, session_id, completions)

    def _apply_completions_on_conn(
        self,
        conn: Any,
        session_id: str,
        completions: Sequence[dict[str, Any]],
    ) -> None:
        now = time.time()
        for item in completions:
            sid = str(item.get("session_id") or session_id)
            seq = int(item["seq"])
            state = str(item["state"])
            dst = item.get("dst")
            displaced = item.get("displaced")
            error = item.get("error")
            allowed = _BATCH_ALLOWED_FROM[state]
            cursor = conn.execute(
                f"""
                UPDATE moves
                SET state = ?, dst = COALESCE(?, dst),
                    displaced = COALESCE(?, displaced),
                    error = COALESCE(?, error), updated_at = ?
                WHERE session_id = ? AND seq = ?
                  AND state IN ({", ".join("?" for _ in allowed)})
                """,
                (state, dst, displaced, error, now, sid, seq, *allowed),
            )
            if cursor.rowcount != 1 and item.get("required", True):
                raise ValueError(
                    f"Move {sid}#{seq} cannot become {state!r}: "
                    f"it is missing or not in {allowed}."
                )

    def _change_state(self, session_id, seq, state, **fields) -> None:
        # Raised here, not on the writer, where an exception rolls back its open session.
        changed = self._writer.execute_write(
            self._transition, str(session_id), int(seq), state, **fields
        )
        if not changed:
            raise ValueError(
                f"Move {session_id}#{seq} cannot become {state!r}: "
                f"it is missing or not in {_ALLOWED_FROM[state]}."
            )

    def _transition(
        self, session_id, seq, state, dst=None, displaced=None, error=None
    ) -> bool:
        allowed = _ALLOWED_FROM[state]
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                f"""
                UPDATE moves
                SET state = ?, dst = COALESCE(?, dst),
                    displaced = COALESCE(?, displaced),
                    error = COALESCE(?, error), updated_at = ?
                WHERE session_id = ? AND seq = ?
                  AND state IN ({", ".join("?" for _ in allowed)})
                """,
                (state, dst, displaced, error, time.time(), session_id, seq, *allowed),
            )
        return cursor.rowcount == 1
