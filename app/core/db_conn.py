"""Database connection module."""

import os
import sys
import threading

try:
    from sqlcipher3 import dbapi2 as sqlite3
except ImportError:
    import sqlite3

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


def get_db_connection(db_path: str):
    """Create and configure a new database connection with performance parameters."""
    global _connection_cache
    abs_path = os.path.abspath(db_path)
    thread_id = threading.get_ident()
    cache_key = (abs_path, thread_id)

    with _cache_lock:
        if cache_key in _connection_cache:
            return _connection_cache[cache_key]

    from app.core.path_utils import resolve_db_crypto

    crypto = resolve_db_crypto(db_path)
    raw_key = crypto.get_raw_key()

    def _open_conn(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        if raw_key:
            conn.execute(f"PRAGMA key = '{raw_key}'")

        cursor = conn.cursor()
        cursor.execute("PRAGMA cipher_version;")
        version = cursor.fetchone()
        if not version or not version[0]:
            raise RuntimeError("SQLCipher is not active on this connection context.")

        # Test database validity to catch unencrypted legacy databases or bad keys
        try:
            conn.execute("PRAGMA user_version;")
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    try:
        conn = _open_conn(db_path)
    except sqlite3.DatabaseError as e:
        err_msg = str(e).lower()
        if "file is not a database" in err_msg or "file is encrypted" in err_msg:
            # Legacy unencrypted database or invalid key. Delete files and recreate.
            for ext in ["", "-wal", "-shm"]:
                target_file = f"{db_path}{ext}"
                if os.path.exists(target_file):
                    os.remove(target_file)
            conn = _open_conn(db_path)
        else:
            raise

    # Enable Write-Ahead Logging (WAL) for simultaneous reads and writes
    conn.execute("PRAGMA journal_mode = WAL")
    # Increase the database in-memory page cache to hold text features and clustering data
    conn.execute("PRAGMA cache_size = -64000")  # 64MB cache

    # Disable mmap_size on Windows to prevent OS-level file locking issues with multiple connections
    if sys.platform != "win32":
        # Enforce optimized disk page allocations
        conn.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap

    # Ensure database size remains stable under rapid writes
    conn.execute(
        "PRAGMA journal_size_limit = 67108864"
    )  # 64MB limit for WAL/rollback logs
    # Set synchronous mode to NORMAL for WAL
    conn.execute("PRAGMA synchronous = NORMAL")

    with _cache_lock:
        _connection_cache[cache_key] = conn

    return conn
