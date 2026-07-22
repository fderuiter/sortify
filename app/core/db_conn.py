"""Database connection module."""

import os
import sqlite3
import threading
from pathlib import Path

_connection_cache = {}
_cache_lock = threading.Lock()

def clear_connection_cache():
    """Clear all cached database connections."""
    global _connection_cache
    with _cache_lock:
        for conn in _connection_cache.values():
            conn.close()
        _connection_cache.clear()

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

