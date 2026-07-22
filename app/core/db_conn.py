"""Database connection module."""

import os
import sqlite3
import sys
import threading

# Global connection cache and lock
_connection_cache: dict[tuple[str, int], sqlite3.Connection] = {}
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

    conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)

    # Enable Write-Ahead Logging (WAL) for simultaneous reads and writes
    conn.execute("PRAGMA journal_mode = WAL")
    # Increase the database in-memory page cache to hold vector embeddings
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
