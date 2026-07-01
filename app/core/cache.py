import sqlite3
import pathlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor

DB_PATH = pathlib.Path.home() / ".autosorter" / "cache.db"

_executor = ThreadPoolExecutor(max_workers=1)

def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS directory_cache (
            source_directory TEXT PRIMARY KEY,
            corpus TEXT,
            locked_files TEXT,
            index_to_word TEXT
        )
    """)
    conn.commit()
    return conn

def _save_cache_sync(source_directory: str, corpus: dict, locked_files: dict, index_to_word: dict):
    try:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO directory_cache (source_directory, corpus, locked_files, index_to_word)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_directory) DO UPDATE SET
                corpus=excluded.corpus,
                locked_files=excluded.locked_files,
                index_to_word=excluded.index_to_word
            """,
            (
                source_directory,
                json.dumps(corpus),
                json.dumps(locked_files),
                json.dumps({str(k): v for k, v in index_to_word.items()})
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to save cache: {e}")

def save_cache_async(source_directory: str, corpus: dict, locked_files: dict, index_to_word: dict):
    _executor.submit(_save_cache_sync, source_directory, corpus, locked_files, index_to_word)

def save_cache_sync(source_directory: str, corpus: dict, locked_files: dict, index_to_word: dict):
    _save_cache_sync(source_directory, corpus, locked_files, index_to_word)

def load_cache(source_directory: str):
    try:
        conn = _get_conn()
        cur = conn.execute("SELECT corpus, locked_files, index_to_word FROM directory_cache WHERE source_directory = ?", (source_directory,))
        row = cur.fetchone()
        conn.close()
        if row:
            corpus = json.loads(row[0])
            locked_files = json.loads(row[1])
            index_to_word = {int(k): v for k, v in json.loads(row[2]).items()}
            return corpus, locked_files, index_to_word
    except Exception as e:
        logging.error(f"Failed to load cache: {e}")
    return None, None, None

