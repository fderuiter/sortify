"""Persistent SQLite cache for application analysis data."""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.exceptions import CacheError


class SparseMatrixLRUCache:
    """In-memory LRU cache for precomputed sparse matrix rows bounded by total documents (default 500)."""

    def __init__(self, max_documents: int = 500):
        self.max_documents = max_documents
        self._cache: Dict[str, List[Tuple[str, str, float]]] = {}
        self._doc_counts: Dict[str, int] = {}
        self._access_order: List[str] = []
        self._lock = threading.Lock()

    def get(self, base_dir: str) -> Optional[List[Tuple[str, str, float]]]:
        """Serve precomputed matrix rows from in-memory cache in under 5ms."""
        with self._lock:
            if base_dir in self._cache:
                if base_dir in self._access_order:
                    self._access_order.remove(base_dir)
                self._access_order.append(base_dir)
                return self._cache[base_dir]
            return None

    def put(self, base_dir: str, rows: List[Tuple[str, str, float]]):
        """Store matrix rows in LRU cache if document capacity allows, evicting LRU items as needed."""
        with self._lock:
            distinct_docs = len({r[0] for r in rows}) if rows else 0
            if distinct_docs > self.max_documents:
                # Exceeds max documents threshold; fall back to uncached behavior
                self._invalidate_unlocked(base_dir)
                return

            current_total = sum(self._doc_counts.values()) - self._doc_counts.get(base_dir, 0)
            while self._access_order and (current_total + distinct_docs > self.max_documents):
                lru_key = self._access_order.pop(0)
                if lru_key != base_dir:
                    self._cache.pop(lru_key, None)
                    evicted_count = self._doc_counts.pop(lru_key, 0)
                    current_total -= evicted_count

            self._cache[base_dir] = rows
            self._doc_counts[base_dir] = distinct_docs
            if base_dir in self._access_order:
                self._access_order.remove(base_dir)
            self._access_order.append(base_dir)

    def invalidate(self, base_dir: Optional[str] = None):
        """Invalidate cache entries for a given base_dir or clear entire cache if base_dir is None."""
        with self._lock:
            self._invalidate_unlocked(base_dir)

    def _invalidate_unlocked(self, base_dir: Optional[str] = None):
        if base_dir is None:
            self._cache.clear()
            self._doc_counts.clear()
            self._access_order.clear()
        else:
            self._cache.pop(base_dir, None)
            self._doc_counts.pop(base_dir, None)
            if base_dir in self._access_order:
                self._access_order.remove(base_dir)

    def doc_count(self, base_dir: Optional[str] = None) -> int:
        """Return total cached document count, or count for specific base_dir."""
        with self._lock:
            if base_dir is not None:
                return self._doc_counts.get(base_dir, 0)
            return sum(self._doc_counts.values())


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
                    conn.execute(
                        "ALTER TABLE directory_cache ADD COLUMN manual_folders TEXT"
                    )
                except Exception as alter_err:
                    logging.debug(f"Column manual_folders alter execution skipped or failed: {alter_err}")
        except Exception as e:
            logging.error(f"Failed to initialize directory cache database: {e}", exc_info=True)
            if not isinstance(e, CacheError):
                raise CacheError(f"Failed to initialize cache database: {e}") from e
            raise

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
        except (sqlite3.Error, json.JSONDecodeError) as e:
            logging.error(
                f"Query or JSON parsing error loading cache for directory '{source_directory}': {e}",
                exc_info=True,
            )
        except Exception as e:
            logging.error(
                f"Failed to load cache for directory '{source_directory}': {e}",
                exc_info=True,
            )
        return None, None, None, None
