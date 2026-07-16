import sqlite3

def get_connection(db_path):
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
