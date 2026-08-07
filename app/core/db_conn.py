"""Database connection module."""

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

try:
    from sqlcipher3 import dbapi2 as sqlite3

    HAS_SQLCIPHER = True
except ImportError:
    import sqlite3

    HAS_SQLCIPHER = False

# Global connection cache and lock
_connection_cache = {}
_cache_lock = threading.Lock()


def clear_connection_cache():
    """Clear all cached database connections."""
    global _connection_cache
    import gc

    with _cache_lock:
        for k, conn in _connection_cache.items():
            try:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                conn.close()
            except Exception:
                pass
        _connection_cache.clear()
    gc.collect()


def clear_dead_thread_connections():
    """Close and remove cached connections for threads that are no longer active to prevent file locking on Windows."""
    global _connection_cache
    active_thread_ids = {t.ident for t in threading.enumerate()}
    with _cache_lock:
        for key in list(_connection_cache.keys()):
            _, thread_id = key
            if thread_id not in active_thread_ids:
                conn = _connection_cache.pop(key, None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass


def get_db_connection(db_path: str):
    """Create and configure a new database connection with performance parameters."""
    global _connection_cache
    abs_path = os.path.abspath(db_path)
    thread_id = threading.get_ident()
    cache_key = (abs_path, thread_id)

    # Automatically clean up connections for dead threads to prevent resource and lock leaks on Windows
    clear_dead_thread_connections()

    with _cache_lock:
        if cache_key in _connection_cache:
            return _connection_cache[cache_key]

    from app.core.path_utils import resolve_db_crypto

    crypto = resolve_db_crypto(db_path)
    raw_key = crypto.get_raw_key()

    if not HAS_SQLCIPHER:
        raise RuntimeError(
            "SQLCipher library is missing. Standard SQLite fallback connections are blocked."
        )

    from contextlib import closing

    conn = None
    try:
        conn = sqlite3.connect(abs_path, timeout=30.0, check_same_thread=False)
        if raw_key:
            with closing(conn.cursor()) as cursor:
                cursor.execute(f"PRAGMA key = '{raw_key}'")

        with closing(conn.cursor()) as cursor:
            cursor.execute("PRAGMA cipher_version;")
            version = cursor.fetchone()
            if not version or not version[0]:
                raise RuntimeError(
                    "SQLCipher is not active on this connection context."
                )

        # Test database validity to catch unencrypted legacy databases or bad keys
        with closing(conn.cursor()) as cursor:
            cursor.execute("PRAGMA user_version;")

        # Enable Write-Ahead Logging (WAL) for simultaneous reads and writes
        with closing(conn.cursor()) as cursor:
            cursor.execute("PRAGMA journal_mode = WAL")
            # Increase the database in-memory page cache to hold text features and clustering data
            cursor.execute("PRAGMA cache_size = -64000")  # 64MB cache

            # Disable mmap_size on Windows to prevent OS-level file locking issues with multiple connections
            if sys.platform != "win32":
                # Enforce optimized disk page allocations
                cursor.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap

            # Ensure database size remains stable under rapid writes
            cursor.execute(
                "PRAGMA journal_size_limit = 67108864"
            )  # 64MB limit for WAL/rollback logs
            # Set synchronous mode to NORMAL for WAL
            cursor.execute("PRAGMA synchronous = NORMAL")

    except Exception as e:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        # Clear all local variables to break traceback-held reference cycles on Windows GHA
        conn = None
        cursor = None
        version = None
        crypto = None
        raw_key = None
        cache_key = None
        abs_path = None

        import sqlite3 as std_sqlite3

        # Safe extraction of module and class names to prevent AttributeError when __module__ is None
        module_name = getattr(type(e), "__module__", "") or ""
        module_name = str(module_name).lower()
        class_name = getattr(type(e), "__name__", "") or ""
        err_msg_lower = str(e).lower()

        # Broad detection of SQLite/SQLCipher database and connection errors
        is_sqlite_or_db_err = (
            isinstance(e, (sqlite3.Error, std_sqlite3.Error))
            or "sqlite" in module_name
            or "sqlcipher" in module_name
            or class_name == "Error"
            or any(
                term in class_name
                for term in (
                    "DatabaseError",
                    "OperationalError",
                    "IntegrityError",
                    "InternalError",
                    "ProgrammingError",
                    "NotSupportedError",
                )
            )
            or any(
                msg in err_msg_lower
                for msg in (
                    "not a database",
                    "encrypted",
                    "disk i/o error",
                    "malformed",
                    "authentication",
                    "password",
                    "passphrase",
                    "mac",
                    "bad decrypt",
                    "mismatch",
                )
            )
        )

        if is_sqlite_or_db_err and not isinstance(
            e, (RuntimeError, SystemExit, KeyboardInterrupt)
        ):
            logger.error(
                "Database decryption failed: The database is encrypted or is not a valid database. "
                "This indicates a locked OS keyring, mismatched cryptographic keys, or decryption failure. "
                f"Database path: '{db_path}'. Error detail: {e}"
            )
            raise sqlite3.DatabaseError(
                f"Failed to decrypt database at '{db_path}'. Please ensure your OS keyring is unlocked and configured correctly."
            ) from e
        else:
            raise

    with _cache_lock:
        _connection_cache[cache_key] = conn

    return conn
