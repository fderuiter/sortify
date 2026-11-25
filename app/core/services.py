"""Managed app-store services with one writer owner per process and store path.

Consumers hold a lease, not ownership of the shared worker. The final lease
drains and stops that worker without stopping process-wide models or pools.
"""

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.db_worker import DBWorker
from app.core.store import STORE_FILENAME, Store, open_store

_condition = threading.Condition(threading.RLock())


@dataclass
class _Services:
    store: Store
    worker: DBWorker
    db: Any
    cache: Any
    history: Any
    users: int = 0
    closing: bool = False


_services: dict[str, _Services] = {}


class ServiceLease:
    """A context-managed claim on store services; close is idempotent."""

    def __init__(self, key: str, services: _Services):
        self._key = key
        self._services = services
        self._closed = False

    @property
    def store(self) -> Store:
        """Return the canonical store owned by this lease."""
        return self._services.store

    @property
    def worker(self) -> DBWorker:
        """Return the shared writer; consumers must not stop it directly."""
        return self._services.worker

    @property
    def db(self) -> Any:
        """Return the managed document database."""
        return self._services.db

    @property
    def cache(self) -> Any:
        """Return the managed directory cache."""
        return self._services.cache

    @property
    def history(self) -> Any:
        """Return the managed history manager."""
        return self._services.history

    def __enter__(self):
        """Enter an active lease."""
        if self._closed:
            raise RuntimeError("Store service lease is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Release services even when a command raises or exits."""
        self.close()

    def close(self) -> None:
        """Release this claim and drain the writer after the final consumer."""
        with _condition:
            if self._closed:
                return
            graph = self._services
            if graph.users == 1 and threading.current_thread() is graph.worker.thread:
                raise RuntimeError(
                    "The final store lease must close outside its writer"
                )
            self._closed = True
            graph.users -= 1
            if graph.users:
                return
            graph.closing = True
            _condition.notify_all()
        # Do not hold the registry lock while waiting for queued work. New
        # consumers wait for this path's old writer to finish before opening it.
        try:
            graph.worker.stop(stop_embeddings=False)
        finally:
            with _condition:
                del _services[self._key]
                _condition.notify_all()


def acquire_services(
    store: Store | None = None, *, db_path: str | Path | None = None
) -> ServiceLease:
    """Acquire services for the app store or an explicit standalone database.

    An explicit db_path preserves the quarantine CLI's custom-database option.
    Store initialization and manager construction run on the managed writer.
    """
    if store is not None and db_path is not None:
        raise ValueError("Pass a store or a custom database path, not both")
    if store is None and db_path is None:
        from app.config import get_app_dir

        path = get_app_dir() / STORE_FILENAME
    elif db_path is not None:
        path = Path(db_path)
    else:
        assert store is not None
        path = store.path
    path = Path(os.path.normcase(str(Path(path).expanduser().resolve())))
    key = str(path)
    with _condition:
        while key in _services and _services[key].closing:
            _condition.wait()
        graph = _services.get(key)
        if graph is None:
            worker = DBWorker()

            def build():
                if db_path is not None:
                    from app.core.cache import CacheManager
                    from app.core.db import MAIN_DB_MIGRATIONS, Database
                    from app.core.db_conn import get_db_connection
                    from app.core.history import HistoryManager
                    from app.core.migrations import schema_version
                    from app.core.store import STORE_MIGRATIONS, _store_initial

                    path.parent.mkdir(parents=True, exist_ok=True)
                    conn = get_db_connection(str(path))
                    with conn:
                        _store_initial(conn)
                    custom_store = Store(path.parent, path)
                    if schema_version(conn) > STORE_MIGRATIONS[-1].version:
                        has_moves = conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moves'"
                        ).fetchone()
                        if not has_moves:
                            with conn:
                                for migration in STORE_MIGRATIONS[1:]:
                                    migration.apply(conn)
                        db = Database(path, worker, migrations=MAIN_DB_MIGRATIONS)
                        cache = CacheManager(
                            str(path), worker, migrations=MAIN_DB_MIGRATIONS
                        )
                        history = HistoryManager(
                            db, cache, db_path=str(path), migrations=MAIN_DB_MIGRATIONS
                        )
                        return _Services(custom_store, worker, db, cache, history)
                    db = custom_store.database(worker)
                    cache = custom_store.cache(worker)
                    return _Services(
                        custom_store,
                        worker,
                        db,
                        cache,
                        custom_store.history(db, cache),
                    )
                canonical = store or open_store(path.parent)
                if canonical.path != path:
                    canonical = Store(canonical.app_dir, path)
                db = canonical.database(worker)
                cache = canonical.cache(worker)
                return _Services(
                    canonical, worker, db, cache, canonical.history(db, cache)
                )

            try:
                graph = worker.execute_write(build)
            except BaseException:
                worker.stop(stop_embeddings=False)
                raise
            _services[key] = graph
        if db_path is None and graph.history is None:
            raise ValueError("This database is already open as a standalone store")
        graph.users += 1
        return ServiceLease(key, graph)
