"""Background worker queue for serialized database write operations."""

import queue
import threading


class DBWorker:
    """A worker that sequentially executes database write operations on a background thread."""

    def __init__(self):
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            func, args, kwargs, result_q = self.q.get()
            if func is None:
                break
            try:
                result = func(*args, **kwargs)
                if result_q is not None:
                    result_q.put(("success", result))
            except Exception as e:
                if result_q is not None:
                    result_q.put(("error", e))
            finally:
                self.q.task_done()
        
        # Ensure all database connections opened by this worker thread are closed
        # before the thread exits, preventing file locking issues on Windows.
        try:
            from app.core.db_conn import clear_connection_cache
            clear_connection_cache()
        except ImportError:
            pass

    def submit_write(self, func, *args, **kwargs):
        """Submit a database write operation to the queue without waiting for completion."""
        result_q = queue.Queue()
        self.q.put((func, args, kwargs, result_q))
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
        self.q.put((func, args, kwargs, None))

    def stop(self):
        """Gracefully stop the worker thread and wait for it to finish."""
        self.q.put((None, None, None, None))
        if self.thread.is_alive():
            self.thread.join()


