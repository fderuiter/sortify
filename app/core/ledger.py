"""Cryptographic key lifecycle audit ledger and metadata secret redaction."""

import json
import logging
import time
import uuid
from typing import Any

from app.config import get_app_dir
from app.core.db_conn import get_db_connection
from app.core.migrations import Migration, migrate
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


def sanitize_audit_metadata(metadata: Any) -> dict[str, Any]:
    """Recursively sanitize metadata dictionary to exclude sensitive raw keys and secrets."""
    if not isinstance(metadata, dict):
        if isinstance(metadata, str):
            return {
                "details": sanitize_secret_patterns(metadata, replacement="[REDACTED]")
            }
        return {}

    sanitized: dict[str, Any] = {}
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
    """Return the default sidecar key-audit database path."""
    return str(get_app_dir() / "transaction_ledger.db")


def _ledger_db_baseline(conn) -> None:
    """Create the transaction and key-audit ledger schema."""
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


# Ordered schema migrations for the sidecar ledger. Never edit one after release.
LEDGER_DB_MIGRATIONS = (Migration(1, "baseline", _ledger_db_baseline),)


class KeyAuditLedger:
    """Persistent SQLite audit ledger for cryptographic key export and rotation events."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_default_ledger_path()
        self._init_db()

    def _init_db(self) -> None:
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA wal_autocheckpoint = 5000")
            migrate(conn, LEDGER_DB_MIGRATIONS, label=f"Ledger {self.db_path}")

    def log_key_audit(
        self,
        event_type: str,
        db_path_hash: str,
        session_id: str = "system",
        status: str = "SUCCESS",
        metadata: dict[str, Any] | None = None,
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> str:
        """Log a key management lifecycle event into key_audit_ledger."""
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

    def log_trash_handoff(
        self,
        session_id: str,
        staged_path: str,
        *,
        original_source: str = "",
        sha256: str = "",
        status: str = "EXPORTED_TO_OS_TRASH",
        reason: str | None = None,
        db_path: str | None = None,
    ) -> str:
        """Record an irreversible staged-duplicate handoff (or headless fallback) to OS Trash."""
        db_hash = "duplicate_trash"
        if db_path:
            try:
                from app.core.crypto import CryptoManager

                db_hash = CryptoManager.derive_db_hash(db_path)
            except (ImportError, OSError, ValueError, TypeError):
                db_hash = "duplicate_trash"

        meta: dict[str, Any] = {
            "staged_path": staged_path,
            "original_source": original_source,
            "sha256": sha256,
            "irreversible": status == "EXPORTED_TO_OS_TRASH",
        }
        if reason:
            meta["reason"] = reason

        return self.log_key_audit(
            event_type="OS_TRASH_HANDOFF",
            db_path_hash=db_hash,
            session_id=session_id or "nosession",
            status=status,
            metadata=meta,
        )

    def get_key_audit_events(
        self,
        session_id: str | None = None,
        event_type: str | None = None,
        db_path_hash: str | None = None,
        db_path: Any | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query historical key management events from key_audit_ledger."""
        if not db_path_hash and db_path:
            from app.core.crypto import CryptoManager

            db_path_hash = CryptoManager.derive_db_hash(db_path)

        query = [
            "SELECT event_id, event_type, db_path_hash, session_id, status, created_at, metadata_json",
            "FROM key_audit_ledger",
        ]
        conditions = []
        params: list[Any] = []

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
                parsed_meta: dict[str, Any] = {}
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
