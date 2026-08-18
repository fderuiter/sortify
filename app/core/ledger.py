"""SQLite Transaction Ledger with Incremental Checkpoints.

Provides crash-resilient transactional logging and automated headless
reconciliation for file relocation operations.
"""

import logging
import os
import time
from typing import Dict, List, Optional

from app.config import get_app_dir
from app.core.db_conn import get_db_connection

logger = logging.getLogger(__name__)


def get_default_ledger_path() -> str:
    """Return the default sidecar ledger database path."""
    return str(get_app_dir() / "transaction_ledger.db")


class TransactionLedger:
    """Persistent sidecar SQLite transaction ledger for tracking file moves and reconciliation."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or get_default_ledger_path()
        self._init_db()

    def _init_db(self):
        """Initialize the sidecar transaction ledger database schema in WAL mode."""
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transaction_ledger (
                    entry_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    base_dir TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    dest_path TEXT NOT NULL,
                    source_rel_path TEXT NOT NULL,
                    dest_rel_path TEXT NOT NULL,
                    current_dest TEXT,
                    file_hash TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_session ON transaction_ledger (session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_status ON transaction_ledger (status)"
            )

    def log_intent(
        self,
        session_id: str,
        base_dir: str,
        source_path: str,
        dest_path: str,
        source_rel_path: str,
        dest_rel_path: str,
        current_dest: str = "",
        file_hash: str = "",
        entry_id: Optional[str] = None,
    ) -> str:
        """Log intent to move a file (PENDING state) incrementally before physical operation."""
        if not entry_id:
            entry_id = f"{session_id}:{source_rel_path}"

        now = time.time()
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO transaction_ledger (
                    entry_id, session_id, base_dir, source_path, dest_path,
                    source_rel_path, dest_rel_path, current_dest, file_hash,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    session_id,
                    base_dir,
                    source_path,
                    dest_path,
                    source_rel_path,
                    dest_rel_path,
                    current_dest,
                    file_hash or "",
                    "PENDING",
                    now,
                    now,
                ),
            )
        return entry_id

    def update_status(self, entry_id: str, status: str):
        """Update transaction state (e.g. MOVED_PHYSICAL, COMPLETED)."""
        now = time.time()
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "UPDATE transaction_ledger SET status = ?, updated_at = ? WHERE entry_id = ?",
                (status, now, entry_id),
            )

    def get_pending_entries(self, session_id: Optional[str] = None) -> List[Dict]:
        """Retrieve incomplete transaction entries."""
        conn = get_db_connection(self.db_path)
        with conn:
            if session_id:
                cur = conn.execute(
                    "SELECT entry_id, session_id, base_dir, source_path, dest_path, "
                    "source_rel_path, dest_rel_path, current_dest, file_hash, status, created_at, updated_at "
                    "FROM transaction_ledger WHERE session_id = ? AND status != 'COMPLETED'",
                    (session_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT entry_id, session_id, base_dir, source_path, dest_path, "
                    "source_rel_path, dest_rel_path, current_dest, file_hash, status, created_at, updated_at "
                    "FROM transaction_ledger WHERE status != 'COMPLETED'"
                )
            columns = [col[0] for col in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def purge_session(self, session_id: str):
        """Purge all entries for a finalized session once batch operations succeed."""
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "DELETE FROM transaction_ledger WHERE session_id = ?",
                (session_id,),
            )

    def purge_completed(self):
        """Purge all COMPLETED entries from the ledger."""
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute("DELETE FROM transaction_ledger WHERE status = 'COMPLETED'")

    def reconcile_incomplete_transactions(self, db=None) -> int:
        """Execute automated headless reconciliation scan for incomplete moves."""
        incomplete = self.get_pending_entries()
        if not incomplete:
            return 0

        logger.info(
            f"Automated headless reconciliation scanning {len(incomplete)} incomplete transactions..."
        )
        reconciled_count = 0

        for entry in incomplete:
            entry_id = entry["entry_id"]
            base_dir = entry["base_dir"]
            source_path = entry["source_path"]
            dest_path = entry["dest_path"]
            source_rel = entry["source_rel_path"]
            dest_rel = entry["dest_rel_path"]
            current_dest = entry["current_dest"]
            file_hash = entry["file_hash"]
            status = entry["status"]

            source_exists = os.path.exists(source_path) or os.path.islink(source_path)
            dest_exists = os.path.exists(dest_path) or os.path.islink(dest_path)

            if status == "MOVED_PHYSICAL" or (dest_exists and not source_exists):
                # Physical move succeeded; roll forward main database update
                if db:
                    try:
                        if file_hash and hasattr(db, "set_user_verified_target"):
                            db.set_user_verified_target(
                                base_dir, file_hash, (current_dest or "").replace("\\", "/")
                            )
                        if hasattr(db, "update_document_path"):
                            db.update_document_path(base_dir, source_rel, dest_rel)
                    except Exception as e:
                        logger.warning(
                            f"Reconciliation DB roll-forward failed for {entry_id}: {e}"
                        )
                self.update_status(entry_id, "COMPLETED")
                reconciled_count += 1

            elif status == "PENDING" and source_exists and not dest_exists:
                # Physical move never started or aborted prior to file move; keep source state in DB
                if db:
                    try:
                        if hasattr(db, "update_document_path"):
                            db.update_document_path(base_dir, source_rel, source_rel)
                    except Exception:
                        pass
                self.update_status(entry_id, "COMPLETED")
                reconciled_count += 1

            elif dest_exists and source_exists:
                # Conflict state during interrupted move:
                if status == "MOVED_PHYSICAL":
                    if db:
                        try:
                            if file_hash and hasattr(db, "set_user_verified_target"):
                                db.set_user_verified_target(
                                    base_dir, file_hash, (current_dest or "").replace("\\", "/")
                                )
                            if hasattr(db, "update_document_path"):
                                db.update_document_path(base_dir, source_rel, dest_rel)
                        except Exception:
                            pass
                else:
                    try:
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(dest_path)
                    except Exception as e:
                        logger.warning(
                            f"Could not remove partial destination during reconciliation: {e}"
                        )
                    if db:
                        try:
                            if hasattr(db, "update_document_path"):
                                db.update_document_path(base_dir, source_rel, source_rel)
                        except Exception:
                            pass
                self.update_status(entry_id, "COMPLETED")
                reconciled_count += 1

            else:
                # Neither exists
                self.update_status(entry_id, "COMPLETED")
                reconciled_count += 1

        self.purge_completed()
        logger.info(f"Headless reconciliation complete. {reconciled_count} entries reconciled.")
        return reconciled_count
