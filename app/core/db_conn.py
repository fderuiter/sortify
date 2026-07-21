"""Database connection module."""

import os
import sqlite3
import threading
from pathlib import Path

_local = threading.local()

def clear_connection_cache():
    """Clear all cached database connections."""
    if not hasattr(_local, "connection_cache"):
        return
    for conn in _local.connection_cache.values():
        conn.close()
    _local.connection_cache.clear()

def get_db_connection(db_path: str):
    """Create and configure a new database connection with performance parameters."""
    if not hasattr(_local, "connection_cache"):
        _local.connection_cache = {}
        
    abs_path = os.path.abspath(db_path)
    if abs_path in _local.connection_cache:
        return _local.connection_cache[abs_path]

    conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
    
    # Enable Write-Ahead Logging (WAL) for simultaneous reads and writes
    conn.execute("PRAGMA journal_mode = WAL")
    # Increase the database in-memory page cache to hold vector embeddings
    conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
    # Enforce optimized disk page allocations
    conn.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap
    # Ensure database size remains stable under rapid writes
    conn.execute(
        "PRAGMA journal_size_limit = 67108864"
    )  # 64MB limit for WAL/rollback logs
    # Set synchronous mode to NORMAL for WAL
    conn.execute("PRAGMA synchronous = NORMAL")
    
    _local.connection_cache[abs_path] = conn
    return conn

