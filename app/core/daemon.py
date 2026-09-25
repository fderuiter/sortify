"""Continuous background watchdog service daemon."""

import asyncio
import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import AppSettings
from app.core.metadata import MetadataPass
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.resilient_file_ops import resilient_remove
from app.core.scanner import get_files_recursively
from app.core.session import AppSession

logger = logging.getLogger("app.daemon")


@dataclass
class FileChangeEvent:
    """Structured filesystem event message for async queue streaming."""

    event_type: str
    file_path: str
    timestamp: float = field(default_factory=time.time)
    dest_path: Optional[str] = None


def _extract_plan_destinations(plan, base_dir=None, current_dest="", dests_set=None):
    if dests_set is None:
        dests_set = set()
    if not isinstance(plan, dict):
        return dests_set
    if plan.get("__type__") in ("file", "directory"):
        return dests_set
    for key, content in plan.items():
        if isinstance(content, dict):
            if content.get("__type__") == "file":
                filename = content.get("target_filename") or os.path.basename(key)
                rel_dst = os.path.join(current_dest, filename).replace("\\", "/")
                dests_set.add(rel_dst)
            elif content.get("__type__") == "directory":
                continue
            else:
                _extract_plan_destinations(
                    content,
                    base_dir=base_dir,
                    current_dest=os.path.join(current_dest, key),
                    dests_set=dests_set,
                )
        elif isinstance(content, str):
            rel_dst = (
                os.path.relpath(content, base_dir).replace("\\", "/")
                if base_dir and os.path.isabs(content)
                else content.replace("\\", "/")
            )
            dests_set.add(rel_dst)
    return dests_set


class DaemonFolderHandler(FileSystemEventHandler):
    """Handler for file system events inside monitored directory."""

    def __init__(self, daemon):
        self.daemon = daemon

    def on_any_event(self, event):
        """Handle any file system event, publish FileChangeEvent to queue and trigger recalculation if valid."""
        src_path = getattr(event, "src_path", "")
        dest_path = getattr(event, "dest_path", None)

        if self.daemon.should_ignore_path(src_path):
            return
        if dest_path and self.daemon.should_ignore_path(dest_path):
            return

        # Suppress recalculation triggers if file moves are actively running,
        # but mark dirty flag if external filesystem event occurs
        if getattr(self.daemon, "is_moving", False):
            if self.daemon.is_self_generated_event(event):
                return
            else:
                self.daemon.mark_pending_dirty()
                return

        event_type = getattr(event, "event_type", "modified")
        change_event = FileChangeEvent(
            event_type=event_type,
            file_path=src_path,
            timestamp=time.time(),
            dest_path=dest_path,
        )
        self.daemon.enqueue_event(change_event)


class ContinuousWatchdogDaemon:
    """Daemon that continuously monitors a folder using an async queue pipeline."""

    def __init__(self, settings: AppSettings, base_dir: str):
        self.settings = settings
        self.base_dir = os.path.abspath(base_dir)
        self.observer = None

        self._lock = threading.Lock()
        self._debounce_timer = None
        self._cancel_event = threading.Event()
        self._is_running = False
        self._move_ref_count = 0
        self._pending_dirty = False
        self._active_move_paths = set()
        self._first_event_time = None

        # Configurable properties for async event pipeline
        self.max_queue_capacity = getattr(settings, "MAX_QUEUE_CAPACITY", 1000)
        self.dedup_window = getattr(settings, "DEDUP_WINDOW", 0.5)
        self.num_workers = getattr(settings, "MAX_WORKERS", 4)
        self.reconciliation_interval = getattr(
            settings, "RECONCILIATION_INTERVAL", 30.0
        )

        # Pipeline state
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_queue: Optional[asyncio.Queue[FileChangeEvent]] = None
        self._recent_events: dict[str, float] = {}
        self._active_path_locks: dict[str, asyncio.Lock] = {}
        self._active_triage_paths: set[str] = set()
        self._worker_tasks: list[asyncio.Task] = []
        self._reconciliation_task: Optional[asyncio.Task] = None
        self._app_session: Optional[AppSession] = None

        # We run the actual sorting loop on a dedicated background execution thread
        self._execution_thread = None

    @property
    def is_moving(self) -> bool:
        """Thread-safe check if file relocation operations are actively executing."""
        with self._lock:
            return self._move_ref_count > 0

    @property
    def pending_dirty(self) -> bool:
        """Thread-safe check if external filesystem events occurred during active move operations."""
        with self._lock:
            return self._pending_dirty

    def mark_pending_dirty(self):
        """Thread-safe mark of pending dirty state."""
        with self._lock:
            if self._is_running:
                self._pending_dirty = True

    def is_self_generated_event(self, event) -> bool:
        """Check if filesystem event is generated by internal move operations."""
        with self._lock:
            if not self._active_move_paths:
                return False

            src = (
                os.path.normcase(os.path.abspath(event.src_path))
                if getattr(event, "src_path", None)
                else None
            )
            dst = (
                os.path.normcase(os.path.abspath(event.dest_path))
                if getattr(event, "dest_path", None)
                else None
            )

            if src and src in self._active_move_paths:
                return True
            if dst and dst in self._active_move_paths:
                return True

            return False

    def _add_parent_paths(self, path: str, paths_set: set):
        """Add parent directories of a path up to base_dir to paths_set."""
        if not path:
            return
        curr = os.path.dirname(os.path.normcase(os.path.abspath(path)))
        base = os.path.normcase(os.path.abspath(self.base_dir)) if self.base_dir else ""
        while curr:
            paths_set.add(curr)
            if not base or curr == base or len(curr) <= len(base):
                break
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent

    def _extract_plan_paths(self, plan: dict, current_dest: str = "") -> set:
        """Extract all source and destination paths (and parent directories) involved in a move plan."""
        paths = set()
        if not isinstance(plan, dict):
            return paths

        base = self.base_dir or ""

        for key, content in plan.items():
            if isinstance(content, str):
                src_path = (
                    os.path.join(base, key)
                    if base and not os.path.isabs(key)
                    else key
                )
                dst_path = (
                    os.path.join(base, content)
                    if base and not os.path.isabs(content)
                    else content
                )

                norm_src = os.path.normcase(os.path.abspath(src_path))
                norm_dst = os.path.normcase(os.path.abspath(dst_path))
                paths.add(norm_src)
                paths.add(norm_dst)
                self._add_parent_paths(norm_src, paths)
                self._add_parent_paths(norm_dst, paths)

            elif isinstance(content, dict):
                if content.get("__type__") == "file":
                    rel_src = content.get("relative_source") or key
                    filename = (
                        content.get("target_filename") or os.path.basename(key)
                    )
                    rel_dst = os.path.join(current_dest, filename)

                    src_path = (
                        os.path.join(base, rel_src)
                        if base and not os.path.isabs(rel_src)
                        else rel_src
                    )
                    dst_path = (
                        os.path.join(base, rel_dst)
                        if base and not os.path.isabs(rel_dst)
                        else rel_dst
                    )

                    norm_src = os.path.normcase(os.path.abspath(src_path))
                    norm_dst = os.path.normcase(os.path.abspath(dst_path))
                    paths.add(norm_src)
                    paths.add(norm_dst)
                    self._add_parent_paths(norm_src, paths)
                    self._add_parent_paths(norm_dst, paths)

                elif content.get("__type__") == "directory":
                    continue
                else:
                    sub_paths = self._extract_plan_paths(
                        content, current_dest=os.path.join(current_dest, key)
                    )
                    paths.update(sub_paths)

        return paths

    @contextlib.contextmanager
    def scoped_move_phase(self, plan=None, active_paths=None):
        """Context manager to scoped-suppress filesystem events during active file move execution."""
        paths_to_add = set()
        if active_paths:
            for p in active_paths:
                if p:
                    norm_p = os.path.normcase(os.path.abspath(p))
                    paths_to_add.add(norm_p)
                    self._add_parent_paths(norm_p, paths_to_add)
        if plan:
            try:
                from app.core.verifier import VerificationEngine

                moves = VerificationEngine.get_moves(self.base_dir, plan)
                for rel_src, src, dst in moves:
                    if src:
                        norm_s = os.path.normcase(os.path.abspath(src))
                        paths_to_add.add(norm_s)
                        self._add_parent_paths(norm_s, paths_to_add)
                    if dst:
                        norm_d = os.path.normcase(os.path.abspath(dst))
                        paths_to_add.add(norm_d)
                        self._add_parent_paths(norm_d, paths_to_add)
            except Exception:
                pass
            paths_to_add.update(self._extract_plan_paths(plan))

        with self._lock:
            self._move_ref_count += 1
            self._active_move_paths.update(paths_to_add)

        try:
            yield
        finally:
            should_trigger = False
            with self._lock:
                self._move_ref_count = max(0, self._move_ref_count - 1)
                if self._move_ref_count == 0:
                    self._active_move_paths.clear()
                    if self._pending_dirty:
                        self._pending_dirty = False
                        should_trigger = True

            if should_trigger:
                self.trigger_recalculation()

    def should_ignore_path(self, path: str) -> bool:
        """Check if path should be ignored to prevent infinite feedback loop."""
        if not path:
            return True
        norm_path = os.path.normpath(path).replace("\\", "/")

        # Ignore database files, cache/temp folders, and application metadata files
        ignored_patterns = [
            ".autosorter",
            "autosorter.db",
            "history.db",
            "cache.db",
            "plan.json",
            ".git",
            ".branches",
            ".pytest_cache",
            "__pycache__",
            "settings.json",
            "autosorter.log",
            "_Quarantine_Staging",
        ]

        for pattern in ignored_patterns:
            if pattern in norm_path:
                return True

        # Also ignore any temporary folder/session folders
        from app.core.path_utils import get_session_base_dir

        session_base = get_session_base_dir()
        if session_base.name in norm_path or str(session_base) in norm_path:
            return True

        # Suffix matching on lowercase file extensions using IGNORED_EXTENSIONS configuration
        ignored_exts = getattr(
            self.settings, "IGNORED_EXTENSIONS", [".crdownload", ".tmp", ".download"]
        )
        lower_path = norm_path.lower()
        for ext in ignored_exts:
            if ext and isinstance(ext, str) and ext.strip():
                ext_clean = ext.strip().lower()
                if not ext_clean.startswith("."):
                    ext_clean = f".{ext_clean}"
                if lower_path.endswith(ext_clean):
                    return True

        return False

    def enqueue_event(self, change_event: FileChangeEvent) -> bool:
        """Publish a FileChangeEvent directly to the asynchronous FIFO queue with deduplication."""
        if not change_event or not change_event.file_path:
            return False

        file_path = change_event.file_path
        if self.should_ignore_path(file_path):
            return False

        norm_p = os.path.normcase(os.path.abspath(file_path))
        now = time.time()

        with self._lock:
            if not self._is_running:
                return False

            last_time = self._recent_events.get(norm_p)
            if last_time is not None and (now - last_time) < self.dedup_window:
                logger.debug(f"Deduplicating rapid event for path: {file_path}")
                return False

            if norm_p in self._active_triage_paths:
                logger.debug(
                    f"Path is currently being triaged, deduplicating event: {file_path}"
                )
                return False

            self._recent_events[norm_p] = now
            if len(self._recent_events) > 2000:
                cutoff = now - (self.dedup_window * 5)
                self._recent_events = {
                    k: v for k, v in self._recent_events.items() if v > cutoff
                }

        queue = self._event_queue
        loop = self._event_loop

        if queue is not None:
            if queue.full():
                logger.warning(
                    f"Queue full (capacity {self.max_queue_capacity}), dropping event for: {file_path}"
                )
                return False

            if loop and loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: queue.put_nowait(change_event) if not queue.full() else None
                )
                return True
            else:
                try:
                    queue.put_nowait(change_event)
                    return True
                except asyncio.QueueFull:
                    logger.warning(f"Queue full, dropping event: {file_path}")
                    return False

        return False

    def _get_path_lock(self, norm_path: str) -> asyncio.Lock:
        """Retrieve or create an asyncio.Lock for a specific normalized file path."""
        with self._lock:
            if norm_path not in self._active_path_locks:
                self._active_path_locks[norm_path] = asyncio.Lock()
            return self._active_path_locks[norm_path]

    def _get_or_create_session(self) -> AppSession:
        """Thread-safe lazy initialization of the daemon AppSession."""
        with self._lock:
            if getattr(self, "_app_session", None) is None:
                self._app_session = AppSession(self.settings, self.base_dir)
            return self._app_session

    def _start_pipeline_event_loop(self):
        """Initialize and launch the background asyncio event loop thread for streaming event processing."""
        from app.core.shared_registry import ContextPropagatingThread

        ready_event = threading.Event()

        def _loop_thread_main():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._event_loop = loop
            self._event_queue = asyncio.Queue(maxsize=self.max_queue_capacity)

            self._worker_tasks = [
                loop.create_task(self._triage_worker(i))
                for i in range(self.num_workers)
            ]
            self._reconciliation_task = loop.create_task(
                self._reconciliation_worker()
            )

            ready_event.set()

            try:
                loop.run_forever()
            finally:
                for task in self._worker_tasks:
                    task.cancel()
                if self._reconciliation_task:
                    self._reconciliation_task.cancel()

                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )

                if getattr(self, "_app_session", None):
                    try:
                        self._app_session.close()
                    except Exception:
                        pass
                    self._app_session = None
                loop.close()

        self._execution_thread = ContextPropagatingThread(
            target=_loop_thread_main, daemon=True
        )
        self._execution_thread.start()
        ready_event.wait(timeout=5.0)

    async def _triage_worker(self, worker_id: int):
        """Async worker task that continuously consumes FileChangeEvents and triages paths."""
        logger.info(f"Async triage worker {worker_id} started.")
        while self._is_running and not self._cancel_event.is_set():
            try:
                if self._event_queue is None:
                    await asyncio.sleep(0.05)
                    continue
                event = await self._event_queue.get()
            except (asyncio.CancelledError, RuntimeError):
                break

            try:
                await self._process_single_event(event)
            except Exception as e:
                logger.error(
                    f"Worker {worker_id} error processing event {event}: {e}",
                    exc_info=True,
                )
            finally:
                if self._event_queue is not None:
                    self._event_queue.task_done()

    async def _process_single_event(self, event: FileChangeEvent):
        """Triage a single file event without whole-directory scans."""
        file_path = event.file_path
        if not file_path or self.should_ignore_path(file_path):
            return

        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
            return

        if self.is_moving and self.is_self_generated_event(event):
            return

        norm_p = os.path.normcase(abs_path)
        lock = self._get_path_lock(norm_p)

        async with lock:
            with self._lock:
                self._active_triage_paths.add(norm_p)
            try:
                await self._triage_file_path(abs_path)
            finally:
                with self._lock:
                    self._active_triage_paths.discard(norm_p)

    async def _triage_file_path(self, abs_path: str):
        """Targeted triage on a single item path using QuarantineInterceptorService staging and compliance policy evaluation."""
        if not os.path.exists(abs_path):
            logger.debug(f"Target file missing before triage: {abs_path}")
            return

        if self.should_ignore_path(abs_path):
            return

        if self.should_ignore_path(abs_path):
            return

        rel_path = (
            os.path.relpath(abs_path, self.base_dir).replace("\\", "/")
            if self.base_dir and abs_path.startswith(self.base_dir)
            else os.path.basename(abs_path)
        )

        app_session = self._get_or_create_session()

        def cancel_check():
            return self._cancel_event.is_set() or not self._is_running

        try:
            self.settings.load()
        except Exception:
            pass

        if cancel_check():
            return

        # Phase 0: Quarantine Interceptor Staging & Compliance Policy Evaluation
        policies = getattr(self.settings, "POLICIES", [])
        worker_timeout = getattr(self.settings, "WORKER_TIMEOUT", 300.0)
        interceptor = QuarantineInterceptorService(
            db=app_session.db,
            policies=policies,
            worker_timeout=worker_timeout,
        )

        try:
            staged_info = interceptor.stage_incoming_file(
                source_path=abs_path,
                base_dir=self.base_dir,
                original_relative_path=rel_path,
            )
        except Exception as e:
            logger.error(
                f"Failed to stage incoming file {abs_path} in quarantine: {e}",
                exc_info=True,
            )
            return

        job_id = staged_info["job_id"]
        staged_filepath = staged_info.get("staged_filepath")

        # Unlink/remove original unisolated file from base_dir post-staging
        if staged_filepath and os.path.abspath(abs_path) != os.path.abspath(staged_filepath):
            if os.path.exists(abs_path):
                resilient_remove(abs_path)

        if cancel_check():
            return

        # Execute deep forensic inspection, PII redaction, and compliance policy evaluation off-thread
        quarantine_record = await asyncio.to_thread(
            interceptor.process_quarantine_job, job_id
        )

        if cancel_check():
            return

        status = (
            quarantine_record.get("status")
            if isinstance(quarantine_record, dict)
            else None
        )
        policy_action = (
            quarantine_record.get("policy_action")
            if isinstance(quarantine_record, dict)
            else None
        )

        # If file was quarantined, archived, or routed to DLQ / manual review, triage is complete
        if status in ("QUARANTINED", "ARCHIVED", "MANUAL_REVIEW_REQUIRED", "DEAD_LETTER_QUEUE"):
            logger.info(
                f"Quarantine interceptor completed for {rel_path} with status {status}"
            )
            return

        # If a compliance action (redact, archive, quarantine, retain) was executed, triage is complete
        if policy_action in ("redact", "archive", "quarantine", "retain"):
            logger.info(
                f"Quarantine interceptor compliance action {policy_action} executed for {rel_path}"
            )
            return

        # Phase 1: Fast-Path Rule Evaluation for released / non-sensitive files
        if not os.path.exists(abs_path):
            return

        MetadataPass.run(
            self.base_dir, [rel_path], self.settings, app_session.db, None, cancel_check
        )

        if cancel_check():
            return

        fast_path_plan = app_session.generate_sorting_plan(
            fast_path_only=True, target_paths=[rel_path]
        )
        if fast_path_plan:
            with self.scoped_move_phase(plan=fast_path_plan):
                summary = app_session.execute_moves(fast_path_plan)
            logger.info(
                f"Targeted Fast-Path triage completed for {rel_path}: {summary}"
            )
            return

        if cancel_check():
            return

        if not os.path.exists(abs_path):
            logger.debug(f"Target file missing before triage: {abs_path}")
            return

        # Phase 2: Text Extraction & Incremental Model Training
        async for item, text, file_hash, was_skipped in app_session.process_items_async(
            [rel_path], cancel_check
        ):
            if cancel_check():
                break
            if not was_skipped:
                chunk = {item: {"text": text, "hash": file_hash}}
                await asyncio.to_thread(app_session.partial_fit, chunk)
                chunk.clear()

        if cancel_check():
            return

        slow_path_plan = app_session.generate_sorting_plan(
            fast_path_only=False, target_paths=[rel_path]
        )
        if slow_path_plan:
            with self.scoped_move_phase(plan=slow_path_plan):
                summary = app_session.execute_moves(slow_path_plan)
            logger.info(
                f"Targeted Slow-Path AI triage completed for {rel_path}: {summary}"
            )

    async def _reconciliation_worker(self):
        """Periodic background reconciliation task executing low-priority directory audits."""
        logger.info("Background reconciliation task started.")
        while self._is_running and not self._cancel_event.is_set():
            try:
                await asyncio.sleep(self.reconciliation_interval)
            except asyncio.CancelledError:
                break

            if not self._is_running or self._cancel_event.is_set():
                break

            if (
                self._event_queue is not None
                and self._event_queue.empty()
                and not self.is_moving
            ):
                try:
                    await self._run_reconciliation_audit()
                except Exception as e:
                    logger.error(
                        f"Error during periodic background reconciliation: {e}",
                        exc_info=True,
                    )

    async def _run_reconciliation_audit(self):
        """Low-priority directory audit to enqueue any untracked files missed during OS buffer overflows."""
        if not self.base_dir or not os.path.exists(self.base_dir):
            return

        def _scan():
            files = get_files_recursively(self.base_dir)
            return [f for f in files if not self.should_ignore_path(f)]

        scanned_files = await asyncio.to_thread(_scan)
        now = time.time()
        enqueued_count = 0

        for file_path in scanned_files:
            if self._cancel_event.is_set() or not self._is_running:
                break
            norm_p = os.path.normcase(os.path.abspath(file_path))
            with self._lock:
                if norm_p in self._active_triage_paths:
                    continue
                last_t = self._recent_events.get(norm_p)
                if last_t is not None and (now - last_t) < self.dedup_window * 2:
                    continue

            change_event = FileChangeEvent(
                event_type="reconciliation",
                file_path=file_path,
                timestamp=now,
            )
            if self.enqueue_event(change_event):
                enqueued_count += 1

        if enqueued_count > 0:
            logger.info(
                f"Reconciliation audit enqueued {enqueued_count} untracked files."
            )

    def start(self):
        """Start the continuous watchdog daemon, workers, event loop, and file system observer."""
        with self._lock:
            if self._is_running:
                return
            self._is_running = True
            self._move_ref_count = 0
            self._pending_dirty = False
            self._active_move_paths.clear()
            self._cancel_event.clear()
            self._recent_events.clear()
            self._active_path_locks.clear()
            self._active_triage_paths.clear()

        logger.info(f"Starting continuous watchdog daemon for: {self.base_dir}")
        print(f"Starting continuous watchdog daemon for: {self.base_dir}")

        # Launch background asyncio event loop & worker pool
        self._start_pipeline_event_loop()

        # Start watchdog observer
        self.observer = Observer()
        handler = DaemonFolderHandler(self)
        self.observer.schedule(handler, self.base_dir, recursive=True)
        self.observer.start()

        # Trigger initial low-priority reconciliation audit
        if self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._run_reconciliation_audit())
            )

    def stop(self):
        """Stop the continuous watchdog daemon, workers, observer, and event loop."""
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
            self._move_ref_count = 0
            self._pending_dirty = False
            self._active_move_paths.clear()
            self._cancel_event.set()
            self._first_event_time = None

            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None

        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except Exception as e:
                logger.error(f"Error stopping observer: {e}")
            finally:
                self.observer = None

        loop = self._event_loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

        if self._execution_thread and self._execution_thread.is_alive():
            self._execution_thread.join(timeout=3.0)

        self._event_loop = None
        self._event_queue = None

        with self._lock:
            if getattr(self, "_app_session", None):
                try:
                    self._app_session.close()
                except Exception:
                    pass
                self._app_session = None

        logger.info("Watchdog daemon stopped.")
        print("Watchdog daemon stopped.")

    def trigger_recalculation(self):
        """Thread-safe and debounced trigger for sorting run."""
        with self._lock:
            if not self._is_running or self._move_ref_count > 0:
                return

            # Track the start time of the first event in a sequence
            now = time.time()
            if getattr(self, "_first_event_time", None) is None:
                self._first_event_time = now

            elapsed = now - self._first_event_time
            debounce_delay = getattr(self.settings, "DEBOUNCE_DELAY", 0.6)
            max_debounce_delay = getattr(self.settings, "MAX_DEBOUNCE_DELAY", 5.0)

            if elapsed >= max_debounce_delay:
                # Under continuous event traffic, if the max debounce delay has been reached,
                # we let the already scheduled timer execute rather than canceling and rescheduling it.
                # This guarantees that the run initiates and doesn't get starved by rapid events.
                return

            # Interrupt current run immediately by setting the cancel event
            self._cancel_event.set()

            # Reset cancel event for the upcoming run
            self._cancel_event = threading.Event()

            max_delay = max(0.0, max_debounce_delay - elapsed)
            delay = min(debounce_delay, max_delay)

            if self._debounce_timer:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(
                delay, self._schedule_run, args=(self._cancel_event,)
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _schedule_run(self, cancel_event):
        """Timer callback that executes the run on a background thread."""
        with self._lock:
            if not self._is_running or cancel_event.is_set():
                return
            self._first_event_time = None

            from app.core.shared_registry import ContextPropagatingThread

            # Start a background execution thread for sorting
            # (Ensures we don't block the timer thread or watchdog event handling)
            thread = ContextPropagatingThread(
                target=self._run_sorting_sync, args=(cancel_event,), daemon=True
            )
            thread.start()

    def _run_sorting_sync(self, cancel_event):
        """Run the core synchronous sorting runner."""
        # Check if canceled before starting
        if cancel_event.is_set():
            return

        logger.info("Executing background sorting run...")
        print("Executing background sorting run...")

        # Define the cancel check callback
        def cancel_check():
            return cancel_event.is_set() or not self._is_running

        # Dynamic reload of settings
        try:
            self.settings.load()
        except Exception as e:
            logger.error(f"Error loading settings dynamically: {e}")

        # Setup isolated event loop for this background thread to process async tasks
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        app_session = None
        try:
            app_session = AppSession(self.settings, self.base_dir)

            # PHASE 1: Fast-Path (Deterministic Rules)
            if cancel_check():
                return
            files = get_files_recursively(self.base_dir)
            files = [f for f in files if not self.should_ignore_path(f)]

            if not files:
                logger.info("No files found to organize.")
                return

            # Run MetadataPass to identify deterministic rule-matched files and mark them as BYPASSED
            MetadataPass.run(
                self.base_dir, files, self.settings, app_session.db, None, cancel_check
            )

            if cancel_check():
                return

            # Generate and execute fast-path plan
            fast_path_plan = app_session.generate_sorting_plan(fast_path_only=True)
            fast_path_moved_destinations = set()

            if fast_path_plan:
                with self.scoped_move_phase(plan=fast_path_plan):
                    fast_path_summary = app_session.execute_moves(fast_path_plan)
                logger.info(f"Phase 1 (Fast-Path) completed: {fast_path_summary}")

                # Retrieve destination paths from the fast_path_plan to bypass them in Phase 2
                fast_path_moved_destinations = _extract_plan_destinations(
                    fast_path_plan, base_dir=self.base_dir
                )

            if cancel_check():
                return

            # REFRESH STATE: Refresh scanned directory state & update file paths in memory to prevent path corruption
            app_session.db.invalidate_cache()
            files = get_files_recursively(self.base_dir)
            files = [f for f in files if not self.should_ignore_path(f)]

            if not files:
                logger.info("No remaining files to organize.")
                return

            # PHASE 2: Slow-Path (AI Classification & Clustering)
            # Run MetadataPass on remaining files, unioned with fast-path moved files to ensure they are bypassed!
            bypassed_files_2 = MetadataPass.run(
                self.base_dir, files, self.settings, app_session.db, None, cancel_check
            )
            bypassed_set = {f.replace("\\", "/") for f in bypassed_files_2}.union(
                {d.replace("\\", "/") for d in fast_path_moved_destinations}
            )
            items_to_sort = [
                f for f in files if f.replace("\\", "/") not in bypassed_set
            ]

            if cancel_check():
                return

            # Process remaining unorganized files (Heavy Text Extraction & OCR)
            if items_to_sort:

                async def process_and_fit():
                    async for (
                        item,
                        text,
                        file_hash,
                        was_skipped,
                    ) in app_session.process_items_async(items_to_sort, cancel_check):
                        if cancel_check():
                            break
                        if not was_skipped:
                            chunk = {item: {"text": text, "hash": file_hash}}
                            await asyncio.to_thread(app_session.partial_fit, chunk)
                            chunk.clear()
                            del chunk
                        del text

                loop.run_until_complete(process_and_fit())

            if cancel_check():
                return

            # Generate slow-path AI plan (full plan, which naturally filters already moved files)
            slow_path_plan = app_session.generate_sorting_plan(fast_path_only=False)
            if not slow_path_plan:
                logger.info("No AI sorting actions needed.")
                return

            if cancel_check():
                return

            # Execute Phase 2 moves (AI Classification)
            with self.scoped_move_phase(plan=slow_path_plan):
                summary = app_session.execute_moves(slow_path_plan)
            logger.info(f"Phase 2 (Slow-Path AI) completed successfully: {summary}")
            print(f"Silent move execution completed successfully: {summary}")

        except Exception as e:
            logger.error(
                f"Error during continuous watchdog execution run: {e}", exc_info=True
            )
            print(f"Error during background sorting run: {e}")
        finally:
            if app_session:
                app_session.close()
            try:
                loop.close()
            except Exception:
                pass


def start_daemon(settings: AppSettings, base_dir: str = None):
    """Start the persistent directory-watching daemon service."""
    if not base_dir:
        base_dir = os.getcwd()

    daemon = ContinuousWatchdogDaemon(settings, base_dir)
    daemon.start()

    # Keep the main thread alive while monitoring
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        daemon.stop()
