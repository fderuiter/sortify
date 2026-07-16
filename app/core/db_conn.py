"""Database connection utility for autosorter."""

import sqlite3


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Create and configure a new database connection with performance parameters."""
    conn = sqlite3.connect(db_path)
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
    return conn
