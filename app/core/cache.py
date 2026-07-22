"""Persistent SQLite cache for application analysis data."""

import json
import logging
from pathlib import Path

from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker


class CacheManager:
    """Manages the persistence of cached extraction data to an SQLite database."""

    def __init__(self, db_path: str, worker: DBWorker):
        self.db_path = db_path
        self.worker = worker
        self._init_db()

    def _get_conn(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = get_db_connection(self.db_path)
        return conn

    def _init_db(self):
        conn = self._get_conn()
        try:
            with conn:
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
        except Exception:
            raise

    def _create_write_func(self, source_directory, corpus, locked_files, index_to_word, manual_folders):
        def _write():
            try:
                conn = self._get_conn()
                with conn:
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
        return _write

    def _save_cache_sync(
        self,
        source_directory: str,
        corpus: dict,
        locked_files: dict,
        index_to_word: dict,
        manual_folders: set = None,
    ):
        if manual_folders is None:
            manual_folders = set()
            
        _write = self._create_write_func(source_directory, corpus, locked_files, index_to_word, manual_folders)
        self.worker.execute_write(_write)

    def save_cache_async(
        self,
        source_directory: str,
        corpus: dict,
        locked_files: dict,
        index_to_word: dict,
        manual_folders: set = None,
    ):
        """Asynchronously save analysis results to the database."""
        if manual_folders is None:
            manual_folders = set()
            
        _write = self._create_write_func(source_directory, corpus, locked_files, index_to_word, manual_folders)
        self.worker.execute_write_async(_write)

    def save_cache_sync(
        self,
        source_directory: str,
        corpus: dict,
        locked_files: dict,
        index_to_word: dict,
        manual_folders: set = None,
    ):
        """Save analysis results to the database synchronously."""
        self._save_cache_sync(
            source_directory, corpus, locked_files, index_to_word, manual_folders
        )

    def load_cache(self, source_directory: str):
        """Load cached analysis results from the database for a specific directory."""
        try:
            conn = self._get_conn()
            with conn:
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
