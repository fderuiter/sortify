import sqlite3
import queue
import threading
from contextlib import contextmanager

class DBWorker:
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

    def submit_write(self, func, *args, **kwargs):
        result_q = queue.Queue()
        self.q.put((func, args, kwargs, result_q))
        return result_q
        
    def execute_write(self, func, *args, **kwargs):
        result_q = self.submit_write(func, *args, **kwargs)
        status, result = result_q.get()
        if status == "error":
            raise result
        return result

    def execute_write_async(self, func, *args, **kwargs):
        self.q.put((func, args, kwargs, None))

worker = DBWorker()
