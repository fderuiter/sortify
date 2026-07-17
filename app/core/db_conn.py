"""Database connection module."""

import os
import sys

from app.core.crypto import SessionCrypto
from pathlib import Path


def get_sqlite_engine():
    """Dynamically determine and return the correct SQLite engine module."""
    import importlib
    if hasattr(sys, '_MEIPASS'):
        sys.path.insert(0, sys._MEIPASS)
    # Dynamically import to hide from PyInstaller, preventing standard compiler errors
    return importlib.import_module("sqlcipher3.dbapi2")

sqlite3 = get_sqlite_engine()

_connection_cache = {}

def clear_connection_cache():
    """Clear all cached database connections."""
    global _connection_cache
    for conn in _connection_cache.values():
        conn.close()
    _connection_cache.clear()

def get_db_connection(db_path: str):
    """Create and configure a new database connection with performance parameters."""
    abs_path = os.path.abspath(db_path)
    if abs_path in _connection_cache:
        return _connection_cache[abs_path]

    conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
    
    # Apply SQLCipher encryption immediately
    crypto = SessionCrypto(Path(db_path).parent / "secret.key", Path(db_path))
    raw_key = crypto.get_raw_key()
    conn.execute(f"PRAGMA key = '{raw_key}'")
    
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
    
    _connection_cache[abs_path] = conn
    return conn

