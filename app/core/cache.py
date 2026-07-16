"""Persistent SQLite cache for application analysis data."""

import json
import logging
from contextlib import closing

from app.config import get_app_dir
from app.core.db_worker import get_connection, worker

DB_PATH = get_app_dir() / "cache.db"


def init_cache_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(get_connection(DB_PATH)) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS directory_cache (
                    source_directory TEXT PRIMARY KEY,
                    corpus TEXT,
                    locked_files TEXT,
                    index_to_word TEXT,
                    manual_folders TEXT
                )
            """)
            try:
                conn.execute("ALTER TABLE directory_cache ADD COLUMN manual_folders TEXT")
            except Exception:
                pass
    except Exception as e:
        logging.error(f"Failed to init cache db: {e}")


def _get_conn():
    return get_connection(DB_PATH)


def _save_cache_sync(
    source_directory: str,
    corpus: dict,
    locked_files: dict,
    index_to_word: dict,
    manual_folders: set = None,
):
    if manual_folders is None:
        manual_folders = set()
    def _write():
        try:
            with closing(_get_conn()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO directory_cache (source_directory, corpus, locked_files, index_to_word, manual_folders)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source_directory) DO UPDATE SET
                        corpus=excluded.corpus,
                        locked_files=excluded.locked_files,
                        index_to_word=excluded.index_to_word,
                        manual_folders=excluded.manual_folders
                    """,
                    (
                        source_directory,
                        json.dumps(corpus),
                        json.dumps(locked_files),
                        json.dumps({str(k): v for k, v in index_to_word.items()}),
                        json.dumps(list(manual_folders)),
                    ),
                )
        except Exception as e:
            logging.error(f"Failed to save cache: {e}")
            raise
    worker.execute_write(_write)


def save_cache_async(
    source_directory: str,
    corpus: dict,
    locked_files: dict,
    index_to_word: dict,
    manual_folders: set = None,
):
    """Save the current directory cache asynchronously to avoid blocking."""
    if manual_folders is None:
        manual_folders = set()
    def _write():
        try:
            with closing(_get_conn()) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO directory_cache (source_directory, corpus, locked_files, index_to_word, manual_folders)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source_directory) DO UPDATE SET
                        corpus=excluded.corpus,
                        locked_files=excluded.locked_files,
                        index_to_word=excluded.index_to_word,
                        manual_folders=excluded.manual_folders
                    """,
                    (
                        source_directory,
                        json.dumps(corpus),
                        json.dumps(locked_files),
                        json.dumps({str(k): v for k, v in index_to_word.items()}),
                        json.dumps(list(manual_folders)),
                    ),
                )
        except Exception as e:
            logging.error(f"Failed to save cache async: {e}")
    worker.execute_write_async(_write)


def save_cache_sync(
    source_directory: str,
    corpus: dict,
    locked_files: dict,
    index_to_word: dict,
    manual_folders: set = None,
):
    """Save the current directory cache synchronously."""
    _save_cache_sync(
        source_directory, corpus, locked_files, index_to_word, manual_folders
    )


def load_cache(source_directory: str):
    """Load the cache for a given source directory from SQLite database."""
    try:
        with closing(_get_conn()) as conn, conn:
            cur = conn.execute(
                "SELECT corpus, locked_files, index_to_word, manual_folders FROM directory_cache WHERE source_directory = ?",
                (source_directory,),
            )
            row = cur.fetchone()
            
        if row:
            corpus = json.loads(row[0])
            locked_files = json.loads(row[1])
            index_to_word = {int(k): v for k, v in json.loads(row[2]).items()}
            manual_folders_raw = row[3]
            manual_folders = (
                set(json.loads(manual_folders_raw)) if manual_folders_raw else set()
            )
            return corpus, locked_files, index_to_word, manual_folders
    except Exception as e:
        logging.error(f"Failed to load cache: {e}")
    return None, None, None, None
