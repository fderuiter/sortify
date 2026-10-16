"""Background worker queue for serialized database write operations."""

import logging
import queue
import threading
import time
from contextlib import contextmanager

from app.core.db_conn import clear_connection_cache, get_db_connection

logger = logging.getLogger(__name__)


class DBWorker:
    """A worker that sequentially executes database write operations and manages background enrichment tasks."""

    def __init__(self):
        self.q = queue.Queue()
        self._stopped = False
        self._lock = threading.Lock()
        self._listeners = []
        self._active_session = None
        from app.core.shared_registry import ContextPropagatingThread

        self.thread = ContextPropagatingThread(
            target=self._run, daemon=True, name="DBWorkerThread"
        )
        self.thread.start()

    def _do_begin_session(
        self, session_id: str, db_path: str = None, timeout: float = 300.0
    ):
        if self._active_session is not None:
            self._do_rollback_session(self._active_session.get("session_id"))

        conn = None
        if db_path:
            try:
                conn = get_db_connection(db_path)
                conn.execute("BEGIN IMMEDIATE")
            except Exception as e:
                logger.warning(
                    f"Failed to execute BEGIN IMMEDIATE for session {session_id}: {e}"
                )

        self._active_session = {
            "session_id": str(session_id),
            "db_path": db_path,
            "timeout": float(timeout),
            "start_time": time.time(),
            "conn": conn,
        }

    def _do_commit_session(self, session_id: str = None):
        if not self._active_session:
            return
        if session_id and str(self._active_session.get("session_id")) != str(session_id):
            return
        conn = self._active_session.get("conn")
        if conn:
            try:
                conn.commit()
            except Exception as e:
                logger.error(
                    f"Error committing transaction session {session_id}: {e}"
                )
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._active_session = None
        else:
            self._active_session = None

    def _do_rollback_session(self, session_id: str = None):
        if not self._active_session:
            return
        conn = self._active_session.get("conn")
        if conn:
            try:
                conn.rollback()
            except Exception as e:
                logger.warning(
                    f"Error rolling back transaction session {session_id}: {e}"
                )
            finally:
                self._active_session = None
        else:
            self._active_session = None

    def _check_session_timeout(self):
        if not self._active_session:
            return
        timeout = self._active_session.get("timeout", 300.0)
        start_time = self._active_session.get("start_time", 0.0)
        elapsed = time.time() - start_time
        if elapsed > timeout:
            sess_id = self._active_session.get("session_id")
            logger.warning(
                f"Transaction session {sess_id} timed out after {elapsed:.1f}s (limit: {timeout}s). Rolling back automatically."
            )
            self._do_rollback_session(sess_id)

    def _run(self):
        try:
            from app.core.shared_registry import _thread_local

            _thread_local.sandboxed = True
            _thread_local.reason = "database worker execution"
        except Exception:
            pass

        while True:
            item = self.q.get()
            if item[0] is None:
                break
            func, args, kwargs, result_q = item[:4]
            in_pool = item[4] if len(item) > 4 else False

            was_in_pool = False
            try:
                from app.core.shared_registry import _thread_local

                was_in_pool = getattr(_thread_local, "in_shared_worker_pool", False)
                if (
                    in_pool
                    or threading.current_thread().name.startswith("DBWorker")
                    or threading.current_thread().name.startswith("GlobalSharedWorker")
                ):
                    _thread_local.in_shared_worker_pool = True
            except Exception:
                pass

            self._check_session_timeout()

            try:
                result = func(*args, **kwargs)
                if result_q is not None:
                    result_q.put(("success", result))
            except Exception as e:
                if self._active_session is not None:
                    sess_id = self._active_session.get("session_id")
                    logger.error(
                        f"Task exception encountered during transaction session {sess_id}. "
                        f"Rolling back open session transaction automatically: {e}"
                    )
                    self._do_rollback_session(sess_id)
                if result_q is not None:
                    result_q.put(("error", e))
            finally:
                try:
                    from app.core.shared_registry import _thread_local

                    _thread_local.in_shared_worker_pool = was_in_pool
                except Exception:
                    pass
                self.q.task_done()

        if self._active_session is not None:
            self._do_rollback_session()

        # Ensure all database connections opened by this worker thread are closed
        # before the thread exits, preventing file locking issues on Windows.
        clear_connection_cache()

    def begin_session(
        self, session_id: str, db_path: str = None, timeout: float = 300.0
    ):
        """Begin an explicit transaction session for multi-batch operations."""
        return self.execute_write(
            self._do_begin_session, session_id, db_path=db_path, timeout=timeout
        )

    def commit_session(self, session_id: str):
        """Commit an active transaction session."""
        return self.execute_write(self._do_commit_session, session_id=session_id)

    def rollback_session(self, session_id: str):
        """Roll back an active transaction session."""
        return self.execute_write(self._do_rollback_session, session_id=session_id)

    def is_in_session(self) -> bool:
        """Return True if an active transaction session is open on the worker."""
        if self._active_session is not None:
            timeout = self._active_session.get("timeout", 300.0)
            start_time = self._active_session.get("start_time", 0.0)
            if time.time() - start_time > timeout:
                return False
            return True
        return False

    def get_active_session_id(self) -> str | None:
        """Return active session ID if a session is currently open."""
        if self.is_in_session() and self._active_session:
            return self._active_session.get("session_id")
        return None

    @contextmanager
    def session_scope(
        self, session_id: str, db_path: str = None, timeout: float = 300.0
    ):
        """Context manager providing explicit transaction session scope."""
        self.begin_session(session_id, db_path=db_path, timeout=timeout)
        try:
            yield
            self.commit_session(session_id)
        except Exception:
            self.rollback_session(session_id)
            raise

    def submit_write(self, func, *args, **kwargs):
        """Submit a database write operation to the queue without waiting for completion."""
        result_q = queue.Queue()
        with self._lock:
            if self._stopped:
                result_q.put(
                    ("error", RuntimeError("Database worker has been stopped"))
                )
                return result_q
            in_pool = False
            try:
                from app.core.shared_registry import _thread_local

                in_pool = (
                    getattr(_thread_local, "in_shared_worker_pool", False)
                    or threading.current_thread().name.startswith("GlobalSharedWorker")
                    or threading.current_thread().name.startswith("DBWorker")
                )
            except Exception:
                pass
            self.q.put((func, args, kwargs, result_q, in_pool))
        return result_q

    def execute_write(self, func, *args, **kwargs):
        """Submit a database write operation and synchronously block until it completes."""
        if threading.current_thread() is getattr(self, "_thread", None):
            return func(*args, **kwargs)
        result_q = self.submit_write(func, *args, **kwargs)
        status, result = result_q.get()
        if status == "error":
            raise result
        return result

    def execute_write_async(self, func, *args, **kwargs):
        """Submit a database write operation to the queue asynchronously and return immediately."""
        if threading.current_thread() is getattr(self, "_thread", None):
            func(*args, **kwargs)
            return
        with self._lock:
            if self._stopped:
                return
            in_pool = False
            try:
                from app.core.shared_registry import _thread_local

                in_pool = (
                    getattr(_thread_local, "in_shared_worker_pool", False)
                    or threading.current_thread().name.startswith("GlobalSharedWorker")
                    or threading.current_thread().name.startswith("DBWorker")
                )
            except Exception:
                pass
            self.q.put((func, args, kwargs, None, in_pool))

    def submit_background_job(self, func, *args, **kwargs):
        """Submit a heavy background job (VLM, EasyOCR, GGUF naming, decryption) off main thread via SharedWorkerPool."""
        with self._lock:
            if self._stopped:
                return None
        from app.core.shared_registry import SharedWorkerPool

        pool = SharedWorkerPool.get_instance()
        future = pool.submit(func, *args, **kwargs)

        def _on_done(fut):
            try:
                res = fut.result()
                self.notify_listeners("job_complete", res)
            except Exception as e:
                self.notify_listeners("job_error", e)

        future.add_done_callback(_on_done)
        return future

    def register_listener(self, callback):
        """Register a callback listener for background job status updates."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unregister_listener(self, callback):
        """Unregister a callback listener."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def notify_listeners(self, event_type: str, data=None):
        """Notify registered listeners of background worker events."""
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(event_type, data)
            except Exception:
                pass

    def stop(self):
        """Gracefully stop the worker thread and wait for it to finish."""
        try:
            from app.core.semantic_embeddings import SemanticEmbeddingManager

            SemanticEmbeddingManager.stop_all()
        except Exception:
            pass

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self.q.put((None, None, None, None))
        if self.thread.is_alive():
            self.thread.join()
