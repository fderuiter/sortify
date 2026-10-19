"""Sortify's one app store (ADR 0008).

All state lives in one encrypted SQLite file under the app directory, with one
list of numbered migrations. The per-session databases that older versions left
in the session folder are copied in once, the first time the store opens.
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.core.cache import CacheManager, _cache_db_baseline
from app.core.db import Database, _main_db_baseline
from app.core.db_conn import get_db_connection
from app.core.exceptions import CryptoError
from app.core.history import HistoryManager, _history_db_baseline
from app.core.migrations import Migration, migrate
from app.core.moves import import_step_ledger, migrate_add_moves

STORE_FILENAME = "sortify.db"

# Per-session files older versions wrote, in import order.
LEGACY_HISTORY_DB = "history.db"
LEGACY_MAIN_DB = "autosorter.db"

# Database errors from either driver; sqlcipher3's classes don't subclass sqlite3's.
try:
    from sqlcipher3 import dbapi2 as _sqlcipher

    _DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, _sqlcipher.Error)
except ImportError:
    _DB_ERRORS = (sqlite3.Error,)


def _store_initial(conn) -> None:
    """Create the documents, history and directory-cache tables in one file."""
    _main_db_baseline(conn)
    _history_db_baseline(conn)
    _cache_db_baseline(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS legacy_imports (
            source_path TEXT PRIMARY KEY,
            imported_at REAL NOT NULL,
            row_count INTEGER NOT NULL
        )
    """)


# Ordered schema migrations for the app store. Never edit one after release.
STORE_MIGRATIONS = (
    Migration(1, "initial", _store_initial),
    Migration(2, "moves", migrate_add_moves),
)


@dataclass(frozen=True)
class Store:
    """The app store file and factories for the managers that read and write it."""

    app_dir: Path
    path: Path

    def database(self, worker) -> Database:
        """Return a Database on the store."""
        return Database(self.path, worker, migrations=STORE_MIGRATIONS)

    def cache(self, worker) -> CacheManager:
        """Return a CacheManager on the store."""
        return CacheManager(str(self.path), worker, migrations=STORE_MIGRATIONS)

    def history(self, db: Database, cache_manager: CacheManager) -> HistoryManager:
        """Return a HistoryManager on the store."""
        return HistoryManager(
            db, cache_manager, db_path=str(self.path), migrations=STORE_MIGRATIONS
        )


def open_store(
    app_dir: Path | str | None = None, legacy_session_root: Path | None = None
) -> Store:
    """Open (creating or migrating) the app store, then import old session databases.

    ``legacy_session_root`` is the folder whose subfolders hold per-session
    databases; it defaults to the session folder older versions used.
    """
    if app_dir is None:
        from app.config import get_app_dir

        app_dir = get_app_dir()
    app_dir = Path(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    store = Store(app_dir=app_dir, path=app_dir / STORE_FILENAME)

    conn = get_db_connection(str(store.path))
    migrate(conn, STORE_MIGRATIONS, label=f"Store {store.path}")

    if legacy_session_root is None:
        from app.core.path_utils import get_session_base_dir

        legacy_session_root = get_session_base_dir()
    import_legacy_sessions(store, Path(legacy_session_root))
    return store


def import_legacy_sessions(store: Store, session_root: Path) -> int:
    """Copy every not-yet-imported old database into the store.

    Old databases sit in each subfolder of ``session_root`` and in the app
    directory itself. Newer files go first, so when two of them indexed the same
    document, the newer row is the one kept. A database that fails to import is
    logged and left in place, and the next open tries it again. Returns the
    number of databases imported.
    """
    folders = [store.app_dir]
    if session_root.is_dir():
        folders += [d for d in session_root.iterdir() if d.is_dir()]

    sources = [
        (folder / name, copy)
        for folder in folders
        for name, copy in (
            (LEGACY_HISTORY_DB, _copy_history),
            (LEGACY_MAIN_DB, _copy_main),
        )
        if (folder / name).is_file()
    ]
    sources.sort(key=lambda source: _mtime(source[0]), reverse=True)
    return sum(_import_one(store, source, copy) for source, copy in sources)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _import_one(store: Store, source: Path, copy: Callable) -> bool:
    from app.core.path_utils import resolve_db_crypto

    source_key = str(source.resolve())
    conn = get_db_connection(str(store.path))
    if conn.in_transaction:
        conn.commit()
    already = conn.execute(
        "SELECT 1 FROM legacy_imports WHERE source_path = ?", (source_key,)
    ).fetchone()
    if already:
        return False

    try:
        legacy = get_db_connection(source_key)
        # Old versions encrypted document text, including the copies in history
        # snapshots, with the key of the main database next to it.
        main_db = source.parent / LEGACY_MAIN_DB
        crypto = (
            resolve_db_crypto(main_db) if main_db.is_file() else None,
            resolve_db_crypto(store.path),
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            row_count = copy(legacy, conn, *crypto)
            conn.execute(
                "INSERT INTO legacy_imports (source_path, imported_at, row_count) VALUES (?, ?, ?)",
                (source_key, time.time(), row_count),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    except (*_DB_ERRORS, OSError, CryptoError, ValueError) as e:
        logging.warning(f"Could not import {source} into the app store: {e}")
        return False
    logging.info(f"Imported {row_count} rows from {source} into the app store.")
    return True


def _columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _copy_table(
    src,
    dst,
    table: str,
    *,
    skip: tuple[str, ...] = (),
    transform: Callable[[dict], dict | None] | None = None,
) -> int:
    """Copy the columns ``table`` shares in both databases; existing keys win."""
    src_columns = set(_columns(src, table))
    if not src_columns:
        return 0
    columns = [c for c in _columns(dst, table) if c in src_columns and c not in skip]
    column_sql = ", ".join(f'"{c}"' for c in columns)
    rows = src.execute(f'SELECT {column_sql} FROM "{table}" ORDER BY rowid').fetchall()

    values = []
    for row in rows:
        record = dict(zip(columns, row))
        if transform is not None:
            record = transform(record)
            if record is None:
                continue
        values.append(tuple(record[c] for c in columns))
    if values:
        dst.executemany(
            f'INSERT OR IGNORE INTO "{table}" ({column_sql}) '
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
    return len(values)


def _reencrypt(column: str, old_crypto, new_crypto) -> Callable[[dict], dict | None]:
    """Re-encrypt ``column`` from the session key to the store key.

    A row whose text the session key can no longer read is left out rather than
    copied as unreadable bytes, so the next scan extracts that file again.
    """

    def transform(record: dict) -> dict | None:
        cipher = record.get(column)
        if cipher is None:
            return record
        if old_crypto is None:
            return None
        try:
            record[column] = new_crypto.encrypt_text(old_crypto.decrypt_text(cipher))
        except CryptoError:
            return None
        return record

    return transform


def _copy_history(src, dst, old_crypto, new_crypto) -> int:
    count = _copy_table(src, dst, "sessions")
    count += _copy_table(src, dst, "snapshot_files")
    count += _copy_table(src, dst, "snapshot_cache")
    count += _copy_table(
        src,
        dst,
        "snapshot_documents",
        transform=_reencrypt("extracted_text", old_crypto, new_crypto),
    )
    # step_id is a store-wide sequence; the copied steps get new ids in their old order.
    count += _copy_table(src, dst, "step_ledger", skip=("step_id",))
    # The move ledger (ADR 0006): copy it where the old file has one, and build
    # it from the copied steps for the sessions it lacks.
    count += _copy_table(src, dst, "moves", skip=("id",))
    count += import_step_ledger(dst)
    return count


def _copy_main(src, dst, old_crypto, new_crypto) -> int:
    count = _copy_table(
        src,
        dst,
        "documents",
        transform=_reencrypt("extracted_text", old_crypto, new_crypto),
    )
    count += _copy_table(src, dst, "transaction_ledger", skip=("id",))
    count += _copy_table(src, dst, "quarantine_records")
    # Vectors, TF-IDF tables, model metadata and classification caches are
    # derived from the documents and rebuilt on the next analysis.
    return count
