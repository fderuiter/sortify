"""Shared worker pool and audio concurrency guard.

``SharedWorkerPool`` is the one background pool for extraction and analysis
tasks, with thread limits and the network sandbox applied to each task.
``AudioConcurrencyGuard`` caps concurrent audio transcriptions.
"""

import concurrent.futures
import logging
import os
import threading
from contextlib import contextmanager
from typing import Callable

from app.core.concurrency import ContextPropagatingThreadPoolExecutor
from app.core.network_guard import _thread_local
from app.core.network_guard import install as apply_global_socket_sandbox


class SharedWorkerPool:
    """Global background task worker pool restricting concurrency and enforcing offline boundaries."""

    _instance = None

    @classmethod
    def get_instance(cls, max_workers=None):
        """Retrieve the singleton instance of SharedWorkerPool, initializing it if necessary."""
        if cls._instance is None:
            # Respect system limits / CPU counts to prevent starvation
            if max_workers is None:
                max_workers = min(4, os.cpu_count() or 2)
            cls._instance = cls(max_workers=max_workers)
        return cls._instance

    def __init__(self, max_workers: int):
        apply_global_socket_sandbox()
        self._executor = ContextPropagatingThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="GlobalSharedWorker",
            initializer=apply_global_socket_sandbox,
        )
        self.max_workers = max_workers

    def submit(self, fn, *args, **kwargs):
        """Submit a task to the pool, ensuring offline boundaries are enforced."""
        parent_sandboxed = getattr(_thread_local, "sandboxed", None)
        parent_reason = getattr(_thread_local, "reason", "worker execution")

        def offline_wrapped_fn(*a, **kw):
            is_sandboxed = True
            reason = parent_reason if parent_sandboxed is True else "worker execution"

            was_sandboxed = getattr(_thread_local, "sandboxed", False)
            old_reason = getattr(_thread_local, "reason", "worker execution")
            was_in_pool = getattr(_thread_local, "in_shared_worker_pool", False)

            _thread_local.sandboxed = is_sandboxed
            _thread_local.reason = reason
            _thread_local.in_shared_worker_pool = True
            try:
                return fn(*a, **kw)
            finally:
                _thread_local.sandboxed = was_sandboxed
                _thread_local.reason = old_reason
                _thread_local.in_shared_worker_pool = was_in_pool

        if (
            getattr(_thread_local, "in_shared_worker_pool", False)
            or threading.current_thread().name.startswith("GlobalSharedWorker")
            or threading.current_thread().name.startswith("DBWorker")
        ):
            fut = concurrent.futures.Future()
            try:
                res = offline_wrapped_fn(*args, **kwargs)
                fut.set_result(res)
            except Exception as exc:  # noqa: BLE001 - forwarded to the Future, as executor.submit does
                fut.set_exception(exc)
            return fut

        return self._executor.submit(offline_wrapped_fn, *args, **kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        """Map a function over iterables using the shared worker pool, enforcing offline boundaries."""
        parent_sandboxed = getattr(_thread_local, "sandboxed", None)
        parent_reason = getattr(_thread_local, "reason", "worker execution")

        def offline_wrapped_fn(*a, **kw):
            is_sandboxed = True
            reason = parent_reason if parent_sandboxed is True else "worker execution"

            was_sandboxed = getattr(_thread_local, "sandboxed", False)
            old_reason = getattr(_thread_local, "reason", "worker execution")
            was_in_pool = getattr(_thread_local, "in_shared_worker_pool", False)

            _thread_local.sandboxed = is_sandboxed
            _thread_local.reason = reason
            _thread_local.in_shared_worker_pool = True
            try:
                return fn(*a, **kw)
            finally:
                _thread_local.sandboxed = was_sandboxed
                _thread_local.reason = old_reason
                _thread_local.in_shared_worker_pool = was_in_pool

        if (
            getattr(_thread_local, "in_shared_worker_pool", False)
            or threading.current_thread().name.startswith("GlobalSharedWorker")
            or threading.current_thread().name.startswith("DBWorker")
        ):
            return [offline_wrapped_fn(*args) for args in zip(*iterables)]

        return self._executor.map(
            offline_wrapped_fn, *iterables, timeout=timeout, chunksize=chunksize
        )

    def shutdown(self, wait=False, cancel_futures=True):
        """Shutdown the underlying executor and reset singleton instance."""
        if hasattr(self, "_executor") and self._executor:
            try:
                self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                self._executor.shutdown(wait=wait)
            except Exception:
                logging.debug("Shared worker pool shutdown failed", exc_info=True)
        SharedWorkerPool._instance = None

    @classmethod
    def shutdown_instance(cls, wait=False):
        """Shutdown the global SharedWorkerPool singleton instance if it exists."""
        if cls._instance is not None:
            try:
                cls._instance._executor.shutdown(wait=wait, cancel_futures=True)
            except Exception:
                logging.debug("Shared worker pool shutdown failed", exc_info=True)
            cls._instance = None


class AudioConcurrencyGuard:
    """Concurrency guard restricting active audio transcription processes across worker threads."""

    _instance = None
    _class_lock = threading.Lock()

    @classmethod
    def get_instance(cls, limit: int | None = None):
        """Retrieve the singleton instance of AudioConcurrencyGuard, updating limit if specified."""
        with cls._class_lock:
            if cls._instance is None:
                if limit is None:
                    limit = 2
                cls._instance = cls(limit=limit)
            elif limit is not None:
                cls._instance.set_limit(limit)
            return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance (useful for testing)."""
        with cls._class_lock:
            cls._instance = None

    def __init__(self, limit: int = 2):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._limit = max(1, int(limit))
        self._active_count = 0

    @property
    def limit(self) -> int:
        """Get the configured maximum concurrent audio tasks limit."""
        with self._lock:
            return self._limit

    @property
    def active_count(self) -> int:
        """Get the current count of active audio tasks."""
        with self._lock:
            return self._active_count

    def set_limit(self, new_limit: int):
        """Update the maximum concurrent audio tasks limit and notify waiting threads."""
        with self._lock:
            self._limit = max(1, int(new_limit))
            self._cond.notify_all()

    def acquire_slot(
        self,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 0.05,
    ) -> bool:
        """Acquire an audio execution slot.

        Returns True if acquired, or False if cancellation was requested before acquisition.
        """
        with self._lock:
            if cancel_check and cancel_check():
                return False
            while self._active_count >= self._limit:
                self._cond.wait(timeout=poll_interval)
                if cancel_check and cancel_check():
                    return False
            self._active_count += 1
            return True

    def release_slot(self):
        """Release an audio execution slot and notify waiting tasks."""
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1
            self._cond.notify_all()

    @contextmanager
    def guard(
        self,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 0.05,
    ):
        """Context manager for acquiring and releasing an audio execution slot.

        Yields True if slot was acquired, or False if cancelled before acquiring.
        """
        acquired = self.acquire_slot(
            cancel_check=cancel_check, poll_interval=poll_interval
        )
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            self.release_slot()
