"""The move ledger: one append-only ``moves`` table per database (ADR 0006).

Each move a sort makes is one row, numbered per session by ``seq``. The mover
records an ``intent`` row and waits for it to commit before it touches the
filesystem, then moves the row to ``done`` (with the final destination and any
file it displaced, ADR 0005), ``verified`` once the destination's hash matches
the source's, or ``failed``. Writes go through the database's single writer
(ADR 0003) and do no file I/O.
"""

import os
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from app.core.db_conn import get_db_connection

INTENT = "intent"
DONE = "done"
VERIFIED = "verified"
FAILED = "failed"
MOVE_STATES = (INTENT, DONE, VERIFIED, FAILED)

# The states each state may follow. An intent row stays an intent while its
# displacement is recorded.
_ALLOWED_FROM = {
    INTENT: (INTENT,),
    DONE: (INTENT,),
    VERIFIED: (DONE,),
    FAILED: (INTENT,),
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
                CHECK (state IN ('intent', 'done', 'verified', 'failed')),
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


def import_transaction_ledger(conn) -> int:
    """Copy ``transaction_ledger`` rows into ``moves`` as ``done``, for sessions with no moves yet.

    Older sorts cleared their ``step_ledger`` once they finished and kept only
    these rows, with paths relative to the session's folder. Returns the number
    of rows copied. Runs inside the caller's transaction.
    """
    columns = {
        row[1] for row in conn.execute('PRAGMA table_info("transaction_ledger")')
    }
    if not {"session_id", "step_number", "base_dir", "source_path"} <= columns:
        return 0
    rows = conn.execute(
        """
        SELECT session_id, step_number, base_dir, source_path, destination_path,
               file_hash, timestamp
        FROM transaction_ledger
        WHERE session_id NOT IN (SELECT DISTINCT session_id FROM moves)
        ORDER BY session_id, step_number, id
        """
    ).fetchall()
    seqs: dict[str, int] = {}
    values = []
    for session_id, _step, base_dir, src, dst, file_hash, timestamp in rows:
        seqs[session_id] = seqs.get(session_id, 0) + 1
        values.append(
            (
                session_id,
                seqs[session_id],
                os.path.normpath(os.path.join(base_dir, src)),
                os.path.normpath(os.path.join(base_dir, dst)),
                file_hash,
                timestamp,
                timestamp,
            )
        )
    conn.executemany(
        """
        INSERT INTO moves (
            session_id, seq, src, dst, src_sha256, size, displaced, state,
            error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 'done', NULL, ?, ?)
        """,
        values,
    )
    return len(values)


def migrate_add_moves(conn) -> None:
    """Migration: add the ``moves`` table and import the old step ledger into it."""
    create_moves_table(conn)
    import_step_ledger(conn)


class _InlineWriter:
    """Runs writes on the calling thread, for a database with no DBWorker."""

    def execute_write(self, func, *args, **kwargs):
        return func(*args, **kwargs)


class MoveLedger:
    """Records a session's moves in the ``moves`` table of one database.

    ``writer`` is the database's single writer (a ``DBWorker``); without one,
    writes run on the calling thread.
    """

    def __init__(self, db_path: str, writer: _Writer | None = None):
        self.db_path = db_path
        self._writer = writer or _InlineWriter()

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

    def list_moves(self, session_id: str) -> list[Move]:
        """Return the session's moves in the order they were recorded."""
        conn = get_db_connection(self.db_path)
        with conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM moves WHERE session_id = ? ORDER BY seq",
                (str(session_id),),
            ).fetchall()
        return [Move(*row) for row in rows]

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
