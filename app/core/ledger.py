"""Cryptographic key lifecycle audit ledger and metadata secret redaction."""

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import get_app_dir
from app.core import db_conn
from app.core.db_conn import DB_ERRORS, get_db_connection
from app.core.migrations import Migration, migrate, schema_version, table_columns
from app.core.text_utils import sanitize_secret_patterns

logger = logging.getLogger(__name__)

_buffered_key_audits_lock = threading.RLock()
_buffered_key_audits: dict[
    str, list[tuple[str, str, str, str, str, float, dict[str, Any]]]
] = {}


def _norm_audit_path(path: Path | str) -> str:
    return os.path.normcase(str(Path(os.path.abspath(os.fspath(path)))))


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
    """Return the default key-audit database path inside the unified app store."""
    return str(get_app_dir() / "sortify.db")


def _resolve_audit_ledger_path(db_path: Path | str) -> str:
    """Resolve the audit database path for ``db_path``, keeping self-audits inside sortify.db."""
    target = Path(db_path)
    default_ledger = get_default_ledger_path()
    canonical_default = str(get_app_dir() / "sortify.db")
    if target.name == "sortify.db" and _norm_audit_path(
        default_ledger
    ) == _norm_audit_path(canonical_default):
        return str(target)
    return default_ledger


def _should_buffer_self_audit(
    db_path: Path | str,
    ledger_path: str,
    *,
    event_type: str = "KEY_EXPORT",
) -> bool:
    """Return True when ``db_path`` is its own audit ledger and is still opening or migrating."""
    if _norm_audit_path(db_path) != _norm_audit_path(ledger_path):
        return False
    if db_conn._is_db_opening(db_path):
        return True
    abs_str = str(Path(db_path).resolve())
    with db_conn._cache_lock:
        cached_conn = db_conn._connection_cache.get((abs_str, threading.get_ident()))
    if cached_conn is None:
        return event_type == "KEY_EXPORT"
    try:
        row = cached_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='key_audit_ledger'"
        ).fetchone()
        return row is None
    except DB_ERRORS:
        return True


def _buffer_self_key_audit(
    ledger_path: str,
    *,
    event_type: str,
    db_path_hash: str,
    session_id: str,
    status: str,
    metadata: dict[str, Any],
) -> None:
    norm = _norm_audit_path(ledger_path)
    entry = (
        f"audit:{uuid.uuid4().hex}",
        event_type,
        db_path_hash,
        session_id,
        status,
        time.time(),
        dict(metadata),
    )
    with _buffered_key_audits_lock:
        _buffered_key_audits.setdefault(norm, []).append(entry)


def _clear_buffered_key_audits() -> None:
    with _buffered_key_audits_lock:
        _buffered_key_audits.clear()


def _flush_buffered_key_audits(db_path: Path | str, conn: Any = None) -> int:
    """Flush buffered self-referential key audit entries into ``key_audit_ledger``."""
    norm = _norm_audit_path(db_path)
    with _buffered_key_audits_lock:
        pending = _buffered_key_audits.pop(norm, None)
    if not pending:
        return 0

    target_conn = conn if conn is not None else get_db_connection(str(db_path))
    rows = [
        (
            event_id,
            event_type,
            db_path_hash,
            session_id,
            status,
            created_at,
            json.dumps(sanitize_audit_metadata(meta), default=str),
        )
        for (
            event_id,
            event_type,
            db_path_hash,
            session_id,
            status,
            created_at,
            meta,
        ) in pending
    ]
    try:
        if target_conn.in_transaction:
            target_conn.commit()
        with target_conn:
            target_conn.executemany(
                """
                INSERT OR IGNORE INTO key_audit_ledger (
                    event_id, event_type, db_path_hash, session_id,
                    status, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)
    except (*DB_ERRORS, OSError, RuntimeError, ValueError, TypeError) as flush_err:
        logger.debug(f"Deferred key audit flush skipped for '{db_path}': {flush_err}")
        return 0


def _emit_key_lifecycle_audit(
    db_path: Path | str,
    *,
    event_type: str,
    db_path_hash: str,
    session_id: str,
    status: str,
    metadata: dict[str, Any],
) -> None:
    """Record a key export or rotation event, buffering self-audits while opening/migrating."""
    ledger_path = _resolve_audit_ledger_path(db_path)
    if _should_buffer_self_audit(db_path, ledger_path, event_type=event_type):
        _buffer_self_key_audit(
            ledger_path,
            event_type=event_type,
            db_path_hash=db_path_hash,
            session_id=session_id,
            status=status,
            metadata=metadata,
        )
        return
    KeyAuditLedger(ledger_path).log_key_audit(
        event_type=event_type,
        db_path_hash=db_path_hash,
        session_id=session_id,
        status=status,
        metadata=metadata,
    )


def _create_key_audit_ledger_schema(conn) -> None:
    """Create the key_audit_ledger table and its lookup indices."""
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


def _ledger_db_baseline(conn) -> None:
    """Create the key-audit ledger schema."""
    _create_key_audit_ledger_schema(conn)


# Ordered schema migrations for standalone key-audit ledgers. Never edit one after release.
LEDGER_DB_MIGRATIONS = (Migration(1, "baseline", _ledger_db_baseline),)


class KeyAuditLedger:
    """Persistent SQLite audit ledger for cryptographic key export and rotation events."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or get_default_ledger_path()
        self._init_db()

    def _init_db(self) -> None:
        target = Path(self.db_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = get_db_connection(self.db_path)
        if target.name == "sortify.db":
            from app.core.store import STORE_MIGRATIONS

            if schema_version(conn) < STORE_MIGRATIONS[-1].version:
                with conn:
                    conn.execute("PRAGMA temp_store = MEMORY")
                    conn.execute("PRAGMA wal_autocheckpoint = 5000")
                    migrate(conn, STORE_MIGRATIONS, label=f"Store {self.db_path}")
        else:
            if schema_version(conn) < LEDGER_DB_MIGRATIONS[-1].version:
                with conn:
                    conn.execute("PRAGMA temp_store = MEMORY")
                    conn.execute("PRAGMA wal_autocheckpoint = 5000")
                    migrate(conn, LEDGER_DB_MIGRATIONS, label=f"Ledger {self.db_path}")
            elif not table_columns(conn, "key_audit_ledger"):
                with conn:
                    _create_key_audit_ledger_schema(conn)
        _flush_buffered_key_audits(self.db_path, conn=conn)

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
        _flush_buffered_key_audits(self.db_path, conn=conn)
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
