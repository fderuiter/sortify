"""Persistent SQLite cache for application analysis data and bounded memory cache utilities."""

import collections
import json
import logging
import threading
import time

try:
    import sqlite3
except Exception:
    try:
        from sqlcipher3 import dbapi2 as sqlite3
    except Exception:
        import types

        sqlite3 = types.ModuleType("sqlite3")
        sqlite3.Error = Exception
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generic,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from app.core.exceptions import CacheError

if TYPE_CHECKING:
    from app.core.db_worker import DBWorker

_MISSING = object()

K = TypeVar("K")
V = TypeVar("V")


class BoundedMemoryCache(Generic[K, V]):
    """Thread-safe bounded in-memory LRU cache with optional TTL expiration and eviction callbacks."""

    def __init__(
        self,
        max_size: int = 1000,
        ttl: float | None = None,
        on_evict: Callable[[K, V], None] | None = None,
    ):
        if max_size <= 0:
            raise ValueError("max_size must be greater than 0")
        self.max_size = max_size
        self.ttl = ttl
        self.on_evict = on_evict
        self._cache: collections.OrderedDict[K, tuple[V, float | None]] = (
            collections.OrderedDict()
        )
        self._lock = threading.RLock()

    def _is_expired(self, expire_time: float | None) -> bool:
        if expire_time is None:
            return False
        return time.monotonic() >= expire_time

    def _purge_expired(self) -> None:
        now = time.monotonic()
        for k in list(self._cache.keys()):
            _, exp = self._cache[k]
            if exp is not None and now >= exp:
                self._remove_item(k, call_on_evict=True)

    def _remove_item(self, key: K, call_on_evict: bool = True) -> None:
        if key in self._cache:
            val, _ = self._cache.pop(key)
            if call_on_evict and self.on_evict:
                try:
                    self.on_evict(key, val)
                except Exception as e:
                    logging.error(f"Error in on_evict callback for key {key}: {e}")

    def get(self, key: K, default: Any = None) -> Any:
        """Retrieve an item from the cache, updating its LRU position if valid."""
        with self._lock:
            if key not in self._cache:
                return default
            val, expire_time = self._cache[key]
            if self._is_expired(expire_time):
                self._remove_item(key, call_on_evict=True)
                return default
            self._cache.move_to_end(key)
            return val

    def set(self, key: K, value: V, ttl: float | None = None) -> None:
        """Store an item in the cache with optional entry-specific TTL."""
        with self._lock:
            ttl_val = ttl if ttl is not None else self.ttl
            expire_time = (time.monotonic() + ttl_val) if ttl_val is not None else None

            if key in self._cache:
                self._cache.pop(key)

            self._cache[key] = (value, expire_time)
            self._cache.move_to_end(key)

            while len(self._cache) > self.max_size:
                evicted_key, (evicted_val, _) = self._cache.popitem(last=False)
                if self.on_evict:
                    try:
                        self.on_evict(evicted_key, evicted_val)
                    except Exception as e:
                        logging.error(
                            f"Error in on_evict callback for key {evicted_key}: {e}"
                        )

    def put(self, key: K, value: V, ttl: float | None = None) -> None:
        """Alias for set."""
        self.set(key, value, ttl=ttl)

    def invalidate(self, key: K) -> bool:
        """Explicitly purge a specific key from the cache. Returns True if found."""
        with self._lock:
            if key in self._cache:
                self._remove_item(key, call_on_evict=True)
                return True
            return False

    def pop(self, key: K, default: Any = None) -> Any:
        """Remove and return an item from the cache."""
        with self._lock:
            if key in self._cache:
                val, expire_time = self._cache[key]
                self._remove_item(key, call_on_evict=False)
                if self._is_expired(expire_time):
                    return default
                return val
            return default

    def clear(self) -> None:
        """Clear all entries in the cache, calling on_evict for each item if defined."""
        with self._lock:
            if self.on_evict:
                for key, (val, _) in list(self._cache.items()):
                    try:
                        self.on_evict(key, val)
                    except Exception as e:
                        logging.error(f"Error in on_evict callback during clear: {e}")
            self._cache.clear()

    def keys(self) -> list[K]:
        """Return a list of non-expired cache keys."""
        with self._lock:
            self._purge_expired()
            return list(self._cache.keys())

    def values(self) -> list[V]:
        """Return a list of non-expired cache values."""
        with self._lock:
            self._purge_expired()
            res: list[V] = []
            for k in list(self._cache.keys()):
                val, exp = self._cache[k]
                if self._is_expired(exp):
                    self._remove_item(k, call_on_evict=True)
                else:
                    res.append(val)
            return res

    def items(self) -> list[tuple[K, V]]:
        """Return a list of non-expired (key, value) pairs."""
        with self._lock:
            self._purge_expired()
            res: list[tuple[K, V]] = []
            for k in list(self._cache.keys()):
                val, exp = self._cache[k]
                if self._is_expired(exp):
                    self._remove_item(k, call_on_evict=True)
                else:
                    res.append((k, val))
            return res

    def __getitem__(self, key: K) -> V:
        """Retrieve an item by key or raise KeyError."""
        val = self.get(key, _MISSING)
        if val is _MISSING:
            raise KeyError(key)
        return val

    def __setitem__(self, key: K, value: V) -> None:
        """Store an item by key."""
        self.set(key, value)

    def __delitem__(self, key: K) -> None:
        """Remove an item by key or raise KeyError."""
        if not self.invalidate(key):
            raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        """Check if non-expired key is present in cache."""
        with self._lock:
            if key not in self._cache:
                return False
            val, expire_time = self._cache[key]  # type: ignore[index]
            if self._is_expired(expire_time):
                self._remove_item(key, call_on_evict=True)  # type: ignore[arg-type]
                return False
            return True

    def __len__(self) -> int:
        """Return count of non-expired items in cache."""
        with self._lock:
            self._purge_expired()
            return len(self._cache)

    def __iter__(self) -> Iterator[V]:
        """Iterate over non-expired cache values (supporting document row iteration)."""
        with self._lock:
            self._purge_expired()
            return iter(self.values())


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

    def __init__(self, db_path: str, worker: "DBWorker | Any"):
        self.db_path = db_path
        self.worker = worker
        self._init_db()

    def _get_conn(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        from app.core.db_conn import get_db_connection

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
