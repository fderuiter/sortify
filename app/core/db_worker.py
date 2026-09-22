"""Background worker queue for serialized database write operations."""

import queue
import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.db_conn import clear_connection_cache


class DBWorker:
    """A worker that sequentially executes database write operations and manages background enrichment tasks."""

    def __init__(self):
        self.q = queue.Queue()
        self._stopped = False
        self._lock = threading.Lock()
        self._listeners = []
        self._bg_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="AsyncEnrichmentWorker"
        )
        from app.core.shared_registry import ContextPropagatingThread

        self.thread = ContextPropagatingThread(
            target=self._run, daemon=True, name="DBWorkerThread"
        )
        self.thread.start()

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
                if in_pool or threading.current_thread().name.startswith("DBWorker") or threading.current_thread().name.startswith("GlobalSharedWorker"):
                    _thread_local.in_shared_worker_pool = True
            except Exception:
                pass

            try:
                result = func(*args, **kwargs)
                if result_q is not None:
                    result_q.put(("success", result))
            except Exception as e:
                if result_q is not None:
                    result_q.put(("error", e))
            finally:
                try:
                    from app.core.shared_registry import _thread_local

                    _thread_local.in_shared_worker_pool = was_in_pool
                except Exception:
                    pass
                self.q.task_done()

        # Ensure all database connections opened by this worker thread are closed
        # before the thread exits, preventing file locking issues on Windows.
        clear_connection_cache()

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
        result_q = self.submit_write(func, *args, **kwargs)
        status, result = result_q.get()
        if status == "error":
            raise result
        return result

    def execute_write_async(self, func, *args, **kwargs):
        """Submit a database write operation to the queue asynchronously and return immediately."""
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
        """Submit a heavy background job (VLM, EasyOCR, GGUF naming, decryption) off main thread with max 2 workers."""
        with self._lock:
            if self._stopped:
                return None
        future = self._bg_pool.submit(func, *args, **kwargs)

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

        try:
            self._bg_pool.shutdown(wait=False)
        except Exception:
            pass

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self.q.put((None, None, None, None))
        if self.thread.is_alive():
            self.thread.join()
