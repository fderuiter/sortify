"""SQLite Transaction Ledger with Incremental Checkpoints.

Provides crash-resilient transactional logging and automated headless
reconciliation for file relocation operations.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from app.config import get_app_dir
from app.core.db_conn import get_db_connection
from app.core.text_utils import sanitize_secret_patterns

logger = logging.getLogger(__name__)

SENSITIVE_KEY_SUBSTRINGS = (
    "key",
    "secret",
    "password",
    "token",
    "fernet",
    "passphrase",
    "credential",
    "private",
)

EXEMPT_KEY_NAMES = {
    "db_path_hash",
    "event_id",
    "key_path",
    "key_type",
}


def sanitize_audit_metadata(metadata: Any) -> Dict[str, Any]:
    """Recursively sanitize metadata dictionary to exclude sensitive raw keys and secrets."""
    if not isinstance(metadata, dict):
        if isinstance(metadata, str):
            return {
                "details": sanitize_secret_patterns(metadata, replacement="[REDACTED]")
            }
        return {}

    sanitized: Dict[str, Any] = {}
    for k, v in metadata.items():
        k_str = str(k)
        lower_k = k_str.lower()
        if lower_k not in EXEMPT_KEY_NAMES and any(
            sub in lower_k for sub in SENSITIVE_KEY_SUBSTRINGS
        ):
            sanitized[k_str] = "[REDACTED]"
            continue

        if isinstance(v, dict):
            sanitized[k_str] = sanitize_audit_metadata(v)
        elif isinstance(v, (list, tuple, set)):
            sanitized_list = []
            for item in v:
                if isinstance(item, dict):
                    sanitized_list.append(sanitize_audit_metadata(item))
                elif isinstance(item, str):
                    sanitized_list.append(
                        sanitize_secret_patterns(item, replacement="[REDACTED]")
                    )
                else:
                    sanitized_list.append(item)
            sanitized[k_str] = sanitized_list
        elif isinstance(v, str):
            sanitized[k_str] = sanitize_secret_patterns(v, replacement="[REDACTED]")
        else:
            sanitized[k_str] = v
    return sanitized


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
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA wal_autocheckpoint = 5000")
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

            conn.execute("""
                CREATE TABLE IF NOT EXISTS key_audit_ledger (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    db_path_hash TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    metadata_json TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_key_audit_session ON key_audit_ledger (session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_key_audit_event_type ON key_audit_ledger (event_type)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_key_audit_db_hash ON key_audit_ledger (db_path_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_key_audit_created_at ON key_audit_ledger (created_at)"
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
                                base_dir,
                                file_hash,
                                (current_dest or "").replace("\\", "/"),
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
                # The move never finished. Remove the destination only when it is
                # the source itself (a hard link left by the link-then-unlink
                # fallback) or a byte-identical copy; anything else may be a file
                # that was already there, so keep both and report it.
                from app.core.resilient_file_ops import (
                    resilient_remove,
                    same_file_content,
                )

                if same_file_content(source_path, dest_path):
                    try:
                        resilient_remove(dest_path)
                    except Exception as e:
                        logger.warning(
                            f"Could not remove duplicate destination during reconciliation: {e}"
                        )
                else:
                    logger.warning(
                        f"Reconciliation conflict for {entry_id}: {dest_path} differs "
                        f"from {source_path}; keeping both files."
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
        logger.info(
            f"Headless reconciliation complete. {reconciled_count} entries reconciled."
        )
        return reconciled_count

    def log_key_audit(
        self,
        event_type: str,
        db_path_hash: str,
        session_id: str = "system",
        status: str = "SUCCESS",
        metadata: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
        created_at: Optional[float] = None,
    ) -> str:
        """Log a key management lifecycle event into key_audit_ledger."""
        import uuid

        if not event_id:
            event_id = f"audit:{uuid.uuid4().hex}"

        if created_at is None:
            created_at = time.time()

        sanitized_meta = sanitize_audit_metadata(metadata or {})
        metadata_json = json.dumps(sanitized_meta, default=str)

        try:
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    """
                    INSERT INTO key_audit_ledger (
                        event_id, event_type, db_path_hash, session_id,
                        status, created_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        db_path_hash,
                        session_id,
                        status,
                        created_at,
                        metadata_json,
                    ),
                )
        except Exception as e:
            logger.error(
                f"Failed to write key audit record for {event_type}: {e}",
                exc_info=True,
            )
        return event_id

    def get_key_audit_events(
        self,
        session_id: Optional[str] = None,
        event_type: Optional[str] = None,
        db_path_hash: Optional[str] = None,
        db_path: Optional[Any] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Query historical key management events from key_audit_ledger."""
        if not db_path_hash and db_path:
            from app.core.crypto import CryptoManager

            db_path_hash = CryptoManager.derive_db_hash(db_path)

        query = [
            "SELECT event_id, event_type, db_path_hash, session_id, status, created_at, metadata_json",
            "FROM key_audit_ledger",
        ]
        conditions = []
        params: List[Any] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        if db_path_hash:
            conditions.append("db_path_hash = ?")
            params.append(db_path_hash)
        if start_time is not None:
            conditions.append("created_at >= ?")
            params.append(start_time)
        if end_time is not None:
            conditions.append("created_at <= ?")
            params.append(end_time)

        if conditions:
            query.append("WHERE " + " AND ".join(conditions))

        query.append("ORDER BY created_at DESC")

        if limit is not None:
            query.append("LIMIT ?")
            params.append(limit)

        sql = " ".join(query)
        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            results = []
            for r in rows:
                meta_json = r[6]
                parsed_meta: Dict[str, Any] = {}
                if meta_json:
                    try:
                        parsed_meta = json.loads(meta_json)
                    except Exception:
                        pass
                results.append(
                    {
                        "event_id": r[0],
                        "event_type": r[1],
                        "db_path_hash": r[2],
                        "session_id": r[3],
                        "status": r[4],
                        "created_at": r[5],
                        "metadata_json": meta_json,
                        "metadata": parsed_meta,
                    }
                )
            return results
