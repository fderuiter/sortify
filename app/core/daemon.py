"""Continuous background watchdog service daemon."""

import asyncio
import concurrent.futures
import contextlib
import functools
import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import AppSettings
from app.core.domain_contracts import (
    _make_json_serializable,
    validate_quarantine_record,
)
from app.core.lifecycle import LifecycleTelemetryMixin
from app.core.metadata import MetadataPass as MetadataPass
from app.core.mover import is_subpath_or_equal
from app.core.path_utils import safe_relpath
from app.core.pipeline import SortPipeline, drop_scope, validate_drop_paths
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.resilient_file_ops import resilient_remove
from app.core.scanner import get_files_recursively
from app.core.session import AppSession

logger = logging.getLogger("app.daemon")


def lower_process_priority() -> None:
    """Immediately lower the scheduling priority of the current process across Linux, macOS, and Windows upon startup."""
    try:
        import psutil

        p = psutil.Process()
        if sys.platform == "win32":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(19)
        return
    except Exception:
        logger.debug("psutil priority lowering failed; trying fallback", exc_info=True)

    # Standard library fallback if psutil is not available or fails
    if sys.platform == "win32":
        try:
            import ctypes

            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            logger.debug("Win32 SetPriorityClass failed", exc_info=True)
    else:
        if hasattr(os, "nice"):
            try:
                current = os.nice(0)
                if current < 19:
                    os.nice(19 - current)
            except OSError:
                logger.debug("os.nice() failed", exc_info=True)


def _run_sorting_process_entry(
    base_dir: str,
    config_filepath: Optional[str] = None,
    cancel_event: Optional[Any] = None,
):
    """Entry point for the process-isolated background sorting worker subprocess."""
    lower_process_priority()
    try:
        from app.config import AppSettings

        settings = (
            AppSettings(filepath=config_filepath) if config_filepath else AppSettings()
        )
        settings.load()
        daemon = ContinuousWatchdogDaemon(settings, base_dir)
        daemon._is_running = True
        event_to_pass = cancel_event if cancel_event is not None else threading.Event()
        daemon._run_sorting_sync(event_to_pass)
    except Exception as e:
        logger.error(f"Error in process-isolated sorting worker: {e}", exc_info=True)


@dataclass
class FileChangeEvent:
    """Structured filesystem event message for async queue streaming."""

    event_type: str
    file_path: str
    timestamp: float = field(default_factory=time.time)
    dest_path: Optional[str] = None
    priority_key: Optional[tuple[int, float]] = None
    archival_priority: int = 5
    archival_priority_score: float = 0.0


@dataclass
class _FolderUndoBatch:
    """Per-watched-folder quiet-window undo session state."""

    root_dir: str
    session_id: Optional[str] = None
    history_manager: Optional[Any] = None
    opened_at: float = 0.0
    last_activity_at: float = 0.0
    in_flight_count: int = 0
    move_count: int = 0
    timer: Optional[threading.Timer] = None


def _normalize_folder_list(folders: Sequence[str]) -> list[str]:
    """Normalize and deduplicate directory paths while preserving order."""
    normalized: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw in folders:
        if not raw or not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        abs_p = os.path.normpath(os.path.abspath(os.path.expanduser(cleaned)))
        real_p = os.path.normpath(os.path.realpath(abs_p))
        key = (os.path.normcase(abs_p), os.path.normcase(real_p))
        if key not in seen:
            seen.add(key)
            normalized.append(abs_p)
    return normalized


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
                safe_relpath(content, base_dir).replace("\\", "/")
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
            self.daemon.mark_pending_dirty()
            loop = getattr(self.daemon, "_event_loop", None)
            if loop is None or not loop.is_running():
                return

        event_type = getattr(event, "event_type", "modified")
        change_event = FileChangeEvent(
            event_type=event_type,
            file_path=src_path,
            timestamp=time.time(),
            dest_path=dest_path,
        )
        self.daemon.enqueue_event(change_event)


class DaemonState(str, Enum):
    """Operational lifecycle states of ContinuousWatchdogDaemon."""

    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    RECALCULATING = "recalculating"


class InvalidStateError(RuntimeError, ValueError):
    """Raised when an invalid daemon state transition is attempted."""


_VALID_TRANSITIONS: dict[DaemonState, set[DaemonState]] = {
    DaemonState.STOPPED: {DaemonState.STOPPED, DaemonState.RUNNING},
    DaemonState.RUNNING: {
        DaemonState.STOPPED,
        DaemonState.RUNNING,
        DaemonState.PAUSED,
        DaemonState.RECALCULATING,
    },
    DaemonState.PAUSED: {
        DaemonState.STOPPED,
        DaemonState.PAUSED,
        DaemonState.RUNNING,
    },
    DaemonState.RECALCULATING: {
        DaemonState.STOPPED,
        DaemonState.RUNNING,
        DaemonState.PAUSED,
        DaemonState.RECALCULATING,
    },
}


class ContinuousWatchdogDaemon(LifecycleTelemetryMixin):
    """Daemon that continuously monitors one or more folders using a shared async queue pipeline."""

    def __init__(
        self,
        settings: AppSettings,
        base_dir: Optional[str] = None,
        watched_folders: Optional[Sequence[str]] = None,
    ):
        self.settings = settings
        if watched_folders is not None:
            resolved_folders = _normalize_folder_list(watched_folders)
            self._tracks_settings_watches = True
        elif base_dir is not None:
            resolved_folders = (
                _normalize_folder_list([base_dir]) if str(base_dir).strip() else []
            )
            self._tracks_settings_watches = False
        else:
            configured = list(getattr(settings, "WATCHED_FOLDERS", []) or [])
            resolved_folders = (
                _normalize_folder_list(configured)
                if configured
                else _normalize_folder_list([os.getcwd()])
            )
            self._tracks_settings_watches = bool(configured)

        self._watched_folders: list[str] = resolved_folders
        self.base_dir: str = (
            resolved_folders[0]
            if resolved_folders
            else (os.path.abspath(base_dir) if base_dir else "")
        )
        self.observer: Any = None
        self._folder_handler: Optional[DaemonFolderHandler] = None
        self._watch_handles: dict[str, Any] = {}
        self._paused_folders: set[str] = set()
        self._sessions_by_root: dict[str, AppSession] = {}
        self._folder_sort_locks: dict[str, threading.RLock] = {}
        self._undo_batches: dict[str, _FolderUndoBatch] = {}
        self._settings_observer_cb: Optional[Callable[[Any], None]] = None

        self._state: DaemonState = DaemonState.STOPPED
        self._lock = threading.RLock()
        self._debounce_timer = None
        self._cancel_event = threading.Event()
        self._scoped_run_token: Optional[threading.Event] = None
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
        self._event_queue: Optional[asyncio.PriorityQueue] = None
        self._recent_events: dict[str, float] = {}
        self._active_path_locks: dict[str, asyncio.Lock] = {}
        self._active_triage_paths: set[str] = set()
        self._worker_tasks: list[asyncio.Task] = []
        self._reconciliation_task: Optional[asyncio.Task] = None
        self._app_session: Optional[AppSession] = None
        self._queue_sequence_counter: int = 0

        # Telemetry & IPC Server State
        self._events_enqueued_count: int = 0
        self._events_processed_count: int = 0
        self._start_time: Optional[float] = None
        self._ipc_server: Any = None

        # We run the actual sorting loop on a dedicated background execution thread
        self._execution_thread = None

        # Triage's blocking work (staging, planning, moving, training) runs here,
        # one job at a time, so the event loop stays free for IPC and queueing
        # and the shared analyzer is never used from two threads at once (#717).
        self._sort_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

        # Process-isolated worker management
        self._sorting_process: Optional[multiprocessing.process.BaseProcess] = None
        self._proc_cancel_event: Optional[Any] = None
        self._rerun_pending: bool = False

    @property
    def state(self) -> DaemonState:
        """Get the current operational lifecycle state of the daemon."""
        with self._lock:
            return self._state

    @state.setter
    def state(self, new_state: DaemonState) -> None:
        """Set daemon state through validated transition guards."""
        with self._lock:
            self._transition_to(new_state)

    def _transition_to(self, new_state: DaemonState) -> None:
        """Validate and transition to a new daemon state."""
        if not isinstance(new_state, DaemonState):
            try:
                new_state = DaemonState(new_state)
            except ValueError:
                raise InvalidStateError(f"Invalid daemon state: {new_state}")

        if new_state == self._state:
            return

        valid_targets = _VALID_TRANSITIONS.get(self._state, set())
        if new_state not in valid_targets:
            raise InvalidStateError(
                f"Cannot transition daemon state from {self._state.name} to {new_state.name}"
            )

        old_state = self._state
        self._state = new_state
        logger.info(f"Daemon state transitioned: {old_state.name} -> {new_state.name}")
        self._notify_lifecycle_listeners(
            old_state, new_state, event_type="DAEMON_STATE_CHANGED"
        )

    @property
    def is_running(self) -> bool:
        """Check if daemon is currently active (running, paused, or recalculating)."""
        with self._lock:
            return self._state != DaemonState.STOPPED

    @property
    def _is_running(self) -> bool:
        """Backwards compatibility accessor for running state."""
        with self._lock:
            return self._state != DaemonState.STOPPED

    @_is_running.setter
    def _is_running(self, value: bool) -> None:
        with self._lock:
            if value:
                if self._state == DaemonState.STOPPED:
                    self._transition_to(DaemonState.RUNNING)
            else:
                if self._state != DaemonState.STOPPED:
                    self._transition_to(DaemonState.STOPPED)

    @property
    def is_paused(self) -> bool:
        """Check if daemon triage processing is currently paused."""
        with self._lock:
            return self._state == DaemonState.PAUSED

    @property
    def _is_paused(self) -> bool:
        """Backwards compatibility accessor for paused state."""
        with self._lock:
            return self._state == DaemonState.PAUSED

    @_is_paused.setter
    def _is_paused(self, value: bool) -> None:
        with self._lock:
            if value:
                self._transition_to(DaemonState.PAUSED)
            else:
                if self._state == DaemonState.PAUSED:
                    self._transition_to(DaemonState.RUNNING)

    @property
    def scoped_run_token(self) -> Optional[threading.Event]:
        """Get the current scoped recalculation cancellation token, if active."""
        with self._lock:
            return self._scoped_run_token

    def _is_cancelled(self, token: Optional[threading.Event] = None) -> bool:
        """Cooperatively check if lifecycle token or scoped run token is cancelled."""
        if not self.is_running or self._cancel_event.is_set():
            return True
        if token is not None and token.is_set():
            return True
        return False

    @property
    def watched_folders(self) -> list[str]:
        """Return ordered list of folders currently configured on this daemon."""
        with self._lock:
            return list(self._watched_folders)

    @watched_folders.setter
    def watched_folders(self, folders: Sequence[str]) -> None:
        self.update_watched_folders(folders)

    def _list_paused_folders(self) -> list[str]:
        with self._lock:
            return [
                folder
                for folder in self._watched_folders
                if os.path.normcase(folder) in self._paused_folders
            ]

    def _resolve_watched_root(self, path: str) -> Optional[str]:
        """Return the watched root folder containing ``path``, or None if outside all watched roots."""
        if not path:
            return None
        abs_path = os.path.normpath(os.path.abspath(path))
        with self._lock:
            roots = list(self._watched_folders)
            fallback_base = self.base_dir
        for root in roots:
            with contextlib.suppress(OSError, ValueError, TypeError):
                if is_subpath_or_equal(abs_path, root):
                    return root
        if not roots and fallback_base:
            with contextlib.suppress(Exception):
                if is_subpath_or_equal(abs_path, fallback_base):
                    return fallback_base
        return None

    def _get_quiet_window_bounds(self) -> tuple[float, float]:
        """Return (debounce_delay, max_debounce_delay) in seconds for undo session batching."""
        raw_debounce = getattr(self.settings, "DEBOUNCE_DELAY", 0.6)
        raw_max = getattr(self.settings, "MAX_DEBOUNCE_DELAY", 5.0)
        try:
            debounce = max(0.05, float(raw_debounce))
        except (TypeError, ValueError):
            debounce = 0.6
        try:
            max_debounce = max(debounce, float(raw_max))
        except (TypeError, ValueError):
            max_debounce = max(debounce, 5.0)
        return debounce, max_debounce

    def _begin_folder_triage_activity(self, root_dir: str) -> None:
        """Mark that a file under ``root_dir`` is actively entering triage."""
        norm_root = os.path.normcase(os.path.abspath(root_dir))
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                batch = _FolderUndoBatch(root_dir=os.path.abspath(root_dir))
                self._undo_batches[norm_root] = batch
            batch.in_flight_count += 1
            batch.last_activity_at = time.time()
            if batch.timer is not None:
                batch.timer.cancel()
                batch.timer = None

    def _acquire_folder_batch_session(
        self, root_dir: str, history_manager: Any
    ) -> Optional[str]:
        """Return an active shared HistoryManager session_id for ``root_dir``'s quiet window."""
        if history_manager is None or not isinstance(
            getattr(history_manager, "db_path", None), (str, os.PathLike)
        ):
            return None
        abs_root = os.path.abspath(root_dir)
        norm_root = os.path.normcase(abs_root)
        now = time.time()
        _, max_debounce = self._get_quiet_window_bounds()

        expired_sid: Optional[str] = None
        expired_hm: Optional[Any] = None
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                batch = _FolderUndoBatch(root_dir=abs_root)
                self._undo_batches[norm_root] = batch
            if batch.timer is not None:
                batch.timer.cancel()
                batch.timer = None
            if (
                batch.session_id is not None
                and batch.opened_at > 0
                and (now - batch.opened_at) >= max_debounce
            ):
                expired_sid = batch.session_id
                expired_hm = batch.history_manager
                batch.session_id = None
                batch.opened_at = 0.0
                batch.move_count = 0

            existing_sid = batch.session_id

        if expired_sid and expired_hm is not None:
            with contextlib.suppress(Exception):
                expired_hm.complete_session(expired_sid)

        if existing_sid:
            with self._lock:
                batch = self._undo_batches[norm_root]
                batch.last_activity_at = time.time()
                batch.history_manager = history_manager
            return existing_sid

        new_sid = str(history_manager.start_session(abs_root))
        with self._lock:
            batch = self._undo_batches.setdefault(
                norm_root, _FolderUndoBatch(root_dir=abs_root)
            )
            if batch.session_id is None:
                batch.session_id = new_sid
                batch.history_manager = history_manager
                batch.opened_at = time.time()
                batch.last_activity_at = batch.opened_at
                batch.move_count = 0
                return new_sid
            concurrent_sid = batch.session_id
        # Another worker opened one concurrently; complete our unused session cleanly.
        with contextlib.suppress(Exception):
            history_manager.complete_session(new_sid)
        return concurrent_sid

    def _peek_folder_batch_session(self, root_dir: str) -> Optional[str]:
        """Return currently open quiet-window session_id for ``root_dir``, if any."""
        norm_root = os.path.normcase(os.path.abspath(root_dir))
        now = time.time()
        _, max_debounce = self._get_quiet_window_bounds()
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None or batch.session_id is None:
                return None
            if batch.opened_at > 0 and (now - batch.opened_at) >= max_debounce:
                return None
            return batch.session_id

    def _adopt_folder_batch_session(
        self, root_dir: str, session_id: str, history_manager: Any
    ) -> None:
        """Register a newly started Phase 0 session_id on ``root_dir``'s quiet-window batch."""
        if not session_id or history_manager is None:
            return
        abs_root = os.path.abspath(root_dir)
        norm_root = os.path.normcase(abs_root)
        now = time.time()
        with self._lock:
            batch = self._undo_batches.setdefault(
                norm_root, _FolderUndoBatch(root_dir=abs_root)
            )
            if batch.session_id is None:
                batch.session_id = session_id
                batch.history_manager = history_manager
                batch.opened_at = now
            batch.last_activity_at = now
            batch.move_count += 1

    def _record_folder_batch_moves(self, root_dir: str, moved_count: int) -> None:
        if moved_count <= 0:
            return
        norm_root = os.path.normcase(os.path.abspath(root_dir))
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is not None:
                batch.move_count += moved_count
                batch.last_activity_at = time.time()

    def _end_folder_triage_activity(self, root_dir: str) -> None:
        """Decrement active triage count for ``root_dir`` and arm quiet-window completion timer."""
        abs_root = os.path.abspath(root_dir)
        norm_root = os.path.normcase(abs_root)
        debounce, max_debounce = self._get_quiet_window_bounds()
        now = time.time()
        immediate_flush = False

        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                return
            batch.in_flight_count = max(0, batch.in_flight_count - 1)
            batch.last_activity_at = now
            if batch.session_id is None:
                return
            if batch.in_flight_count > 0:
                return
            if not self.is_running:
                immediate_flush = True
            else:
                elapsed = (
                    max(0.0, now - batch.opened_at) if batch.opened_at > 0 else 0.0
                )
                if elapsed >= max_debounce:
                    immediate_flush = True
                else:
                    delay = min(debounce, max(0.02, max_debounce - elapsed))
                    if batch.timer is not None:
                        batch.timer.cancel()
                    timer = threading.Timer(
                        delay, self._on_folder_quiet_timer, args=(norm_root,)
                    )
                    timer.daemon = True
                    batch.timer = timer
                    timer.start()

        if immediate_flush:
            self._flush_folder_batch_session(norm_root)

    def _on_folder_quiet_timer(self, norm_root: str) -> None:
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                return
            batch.timer = None
            if batch.in_flight_count > 0:
                return
        self._flush_folder_batch_session(norm_root)

    def _flush_folder_batch_session(self, norm_root: str) -> Optional[str]:
        """Complete and clear any open quiet-window undo session for ``norm_root``."""
        sid: Optional[str] = None
        hm: Optional[Any] = None
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                return None
            if batch.timer is not None:
                batch.timer.cancel()
                batch.timer = None
            sid = batch.session_id
            hm = batch.history_manager
            batch.session_id = None
            batch.opened_at = 0.0
            batch.move_count = 0

        if sid and hm is not None:
            try:
                moves = hm.moves.list_moves(sid) if hasattr(hm, "moves") else []
                if moves:
                    hm.complete_session(sid)
                else:
                    # Remove unused 0-move session row so undo --latest never hits an empty session.
                    from app.core.db_conn import get_db_connection

                    worker = getattr(getattr(hm, "db", None), "worker", None)

                    def _drop_empty() -> None:
                        conn = get_db_connection(str(hm.db_path))
                        with conn:
                            conn.execute(
                                "DELETE FROM sessions WHERE session_id = ?", (sid,)
                            )

                    if worker is not None and hasattr(worker, "execute_write"):
                        worker.execute_write(_drop_empty)
                    else:
                        _drop_empty()
            except Exception:
                logger.debug(
                    "Failed to finalize batched undo session %s", sid, exc_info=True
                )
        return sid

    def flush_all_undo_batches(self) -> None:
        """Complete all open quiet-window undo sessions across watched folders."""
        with self._lock:
            keys = list(self._undo_batches.keys())
        for key in keys:
            self._flush_folder_batch_session(key)

    def pause(self):
        """Pause processing of queued file triage events across all watched folders."""
        with self._lock:
            self._transition_to(DaemonState.PAUSED)
            self._paused_folders = {os.path.normcase(f) for f in self._watched_folders}
        self.flush_all_undo_batches()
        logger.info("Daemon triage processing paused.")

    def resume(self):
        """Resume processing of queued file triage events across all watched folders."""
        with self._lock:
            if self._state == DaemonState.PAUSED:
                self._transition_to(DaemonState.RUNNING)
            elif self._state == DaemonState.STOPPED:
                raise InvalidStateError("Cannot resume daemon when stopped")
            self._paused_folders.clear()
        if self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._run_reconciliation_audit())
            )
        logger.info("Daemon triage processing resumed.")

    def pause_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Pause triage for specific watched folders (or transition daemon to PAUSED if all are paused)."""
        targets = {os.path.normcase(f) for f in _normalize_folder_list(folders)}
        matched_norms: list[str] = []
        with self._lock:
            for root in self._watched_folders:
                norm_r = os.path.normcase(root)
                if norm_r in targets:
                    self._paused_folders.add(norm_r)
                    matched_norms.append(norm_r)
            if (
                self._watched_folders
                and all(
                    os.path.normcase(r) in self._paused_folders
                    for r in self._watched_folders
                )
                and self._state != DaemonState.STOPPED
            ):
                self._transition_to(DaemonState.PAUSED)
        for norm_r in matched_norms:
            self._flush_folder_batch_session(norm_r)
        return {
            "status": self.state.value,
            "paused_folders": self._list_paused_folders(),
            "watched_folders": self.watched_folders,
        }

    def resume_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Resume triage for specific watched folders."""
        targets = {os.path.normcase(f) for f in _normalize_folder_list(folders)}
        resumed_roots: list[str] = []
        with self._lock:
            if self._state == DaemonState.STOPPED:
                raise InvalidStateError("Cannot resume daemon when stopped")
            for root in self._watched_folders:
                norm_r = os.path.normcase(root)
                if norm_r in targets and norm_r in self._paused_folders:
                    self._paused_folders.discard(norm_r)
                    resumed_roots.append(root)
            if self._state == DaemonState.PAUSED and any(
                os.path.normcase(r) not in self._paused_folders
                for r in self._watched_folders
            ):
                self._transition_to(DaemonState.RUNNING)
        if resumed_roots and self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._run_reconciliation_audit(roots=resumed_roots)
                )
            )
        return {
            "status": self.state.value,
            "paused_folders": self._list_paused_folders(),
            "watched_folders": self.watched_folders,
        }

    def update_watched_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Dynamically reconcile active Observer watches to match ``folders`` without restarting."""
        normalized = [f for f in _normalize_folder_list(folders) if os.path.isdir(f)]
        added_roots: list[str] = []
        removed_norms: list[str] = []
        sessions_to_close: list[AppSession] = []

        with self._lock:
            self._tracks_settings_watches = True
            new_norm_map = {os.path.normcase(f): f for f in normalized}
            old_norm_map = {os.path.normcase(f): f for f in self._watched_folders}

            for norm_f, folder in new_norm_map.items():
                if norm_f not in old_norm_map:
                    added_roots.append(folder)
            for norm_f in old_norm_map:
                if norm_f not in new_norm_map:
                    removed_norms.append(norm_f)

            self._watched_folders = list(normalized)
            self.base_dir = normalized[0] if normalized else ""
            self._paused_folders.intersection_update(new_norm_map.keys())

            observer = self.observer
            if observer is not None:
                if self._folder_handler is None:
                    self._folder_handler = DaemonFolderHandler(self)
                for norm_r in removed_norms:
                    handle = self._watch_handles.pop(norm_r, None)
                    if handle is not None:
                        with contextlib.suppress(Exception):
                            observer.unschedule(handle)
                for folder in normalized:
                    norm_f = os.path.normcase(folder)
                    if norm_f not in self._watch_handles:
                        try:
                            self._watch_handles[norm_f] = observer.schedule(
                                self._folder_handler, folder, recursive=True
                            )
                        except Exception as exc:
                            logger.error(
                                "Failed to schedule watch for '%s': %s",
                                folder,
                                exc,
                                exc_info=True,
                            )

            for norm_r in removed_norms:
                popped_sess = self._sessions_by_root.pop(norm_r, None)
                if popped_sess is not None:
                    if popped_sess is self._app_session:
                        self._app_session = None
                    sessions_to_close.append(popped_sess)

            if self.base_dir:
                primary_sess = self._sessions_by_root.get(
                    os.path.normcase(self.base_dir)
                )
                if primary_sess is not None:
                    self._app_session = primary_sess

        for norm_r in removed_norms:
            self._flush_folder_batch_session(norm_r)
        for sess in sessions_to_close:
            with contextlib.suppress(Exception):
                sess.close()

        ipc_srv = self._ipc_server
        if ipc_srv is not None and hasattr(ipc_srv, "refresh_pid_file"):
            with contextlib.suppress(Exception):
                ipc_srv.refresh_pid_file()

        if added_roots and self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    self._run_reconciliation_audit(roots=added_roots)
                )
            )

        return {
            "watched_folders": self.watched_folders,
            "added": added_roots,
            "removed_count": len(removed_norms),
        }

    def reload_watched_folders(
        self,
        watched_folders: Optional[Sequence[str]] = None,
        add_folders: Optional[Sequence[str]] = None,
        remove_folders: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """Reload settings from disk and dynamically apply WATCHED_FOLDERS updates."""
        self._reload_settings(log_error=True)
        if watched_folders is not None:
            target_folders = list(watched_folders)
        else:
            configured = list(getattr(self.settings, "WATCHED_FOLDERS", []) or [])
            if add_folders is None and remove_folders is None:
                target_folders = configured
            else:
                with self._lock:
                    base_list = (
                        list(self._watched_folders)
                        if self._watched_folders
                        else list(configured)
                    )
                target_folders = list(base_list)
                if add_folders:
                    target_folders.extend(add_folders)
                if remove_folders:
                    rem_norms = {
                        os.path.normcase(f)
                        for f in _normalize_folder_list(remove_folders)
                    }
                    target_folders = [
                        f
                        for f in target_folders
                        if os.path.normcase(os.path.normpath(os.path.abspath(f)))
                        not in rem_norms
                    ]
        self._tracks_settings_watches = True
        res = self.update_watched_folders(target_folders)
        return {
            "status": "reloaded",
            **res,
        }

    def stop_watching_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Unschedule specific watched folders; signal full daemon stop if none remain."""
        targets = {os.path.normcase(f) for f in _normalize_folder_list(folders)}
        with self._lock:
            remaining = [
                f for f in self._watched_folders if os.path.normcase(f) not in targets
            ]
        self.update_watched_folders(remaining)
        should_stop = len(remaining) == 0
        return {
            "status": "stopping" if should_stop else self.state.value,
            "message": (
                "Daemon shutting down"
                if should_stop
                else f"Unscheduled requested folder(s); {len(remaining)} folder(s) remain watched"
            ),
            "watched_folders": remaining,
            "stopped_daemon": should_stop,
            "should_stop_daemon": should_stop,
        }

    def get_status_telemetry(self) -> dict:
        """Get real-time operational status telemetry for IPC inspection."""
        queue_depth = self._event_queue.qsize() if self._event_queue is not None else 0
        active_workers = len([t for t in self._worker_tasks if not t.done()])
        uptime = time.time() - self._start_time if self._start_time else 0.0
        with self._lock:
            active_triages = list(self._active_triage_paths)
            curr_state = self._state
            paused = curr_state == DaemonState.PAUSED
            running = curr_state != DaemonState.STOPPED
            watched = list(self._watched_folders)
            paused_folders = [
                f for f in watched if os.path.normcase(f) in self._paused_folders
            ]
            base_dir = self.base_dir

        return {
            "status": curr_state.value,
            "is_paused": paused,
            "is_running": running,
            "pid": os.getpid(),
            "base_dir": base_dir,
            "watched_folders": watched,
            "watched_folder_count": len(watched),
            "paused_folders": paused_folders,
            "queue_depth": queue_depth,
            "max_queue_capacity": self.max_queue_capacity,
            "active_workers": active_workers,
            "busy_workers": len(active_triages),
            "active_triage_paths": active_triages,
            "is_moving": self.is_moving,
            "pending_dirty": self.pending_dirty,
            "uptime_seconds": round(uptime, 2),
        }

    def get_health_telemetry(self) -> dict:
        """Get process health check telemetry for IPC monitoring."""
        queue_depth = self._event_queue.qsize() if self._event_queue is not None else 0
        queue_full = (
            self._event_queue.full() if self._event_queue is not None else False
        )
        uptime = time.time() - self._start_time if self._start_time else 0.0
        with self._lock:
            paused = self._is_paused

        status_str = "paused" if paused else ("degraded" if queue_full else "ok")
        return {
            "status": status_str,
            "uptime_seconds": round(uptime, 2),
            "queue_full": queue_full,
            "queue_depth": queue_depth,
            "pid": os.getpid(),
        }

    def get_metrics_telemetry(self) -> dict:
        """Get cumulative performance and event metrics for IPC monitoring."""
        queue_depth = self._event_queue.qsize() if self._event_queue is not None else 0
        active_workers = len([t for t in self._worker_tasks if not t.done()])
        uptime = time.time() - self._start_time if self._start_time else 0.0
        with self._lock:
            enqueued = self._events_enqueued_count
            processed = self._events_processed_count

        return {
            "events_enqueued": enqueued,
            "events_processed": processed,
            "queue_depth": queue_depth,
            "max_queue_capacity": self.max_queue_capacity,
            "active_workers": active_workers,
            "uptime_seconds": round(uptime, 2),
        }

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

    def _add_parent_paths(
        self, path: str, paths_set: set, base_dir: Optional[str] = None
    ):
        """Add parent directories of a path up to base_dir to paths_set."""
        if not path:
            return
        curr = os.path.dirname(os.path.normcase(os.path.abspath(path)))
        resolved_base = base_dir or self._resolve_watched_root(path) or self.base_dir
        base = os.path.normcase(os.path.abspath(resolved_base)) if resolved_base else ""
        while curr:
            paths_set.add(curr)
            if not base or curr == base or len(curr) <= len(base):
                break
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent

    def _extract_plan_paths(
        self,
        plan: dict,
        current_dest: str = "",
        base_dir: Optional[str] = None,
    ) -> set:
        """Extract all source and destination paths (and parent directories) involved in a move plan."""
        paths = set()
        if not isinstance(plan, dict):
            return paths

        base = base_dir if base_dir is not None else (self.base_dir or "")

        for key, content in plan.items():
            if isinstance(content, str):
                src_path = (
                    os.path.join(base, key) if base and not os.path.isabs(key) else key
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
                self._add_parent_paths(norm_src, paths, base_dir=base)
                self._add_parent_paths(norm_dst, paths, base_dir=base)

            elif isinstance(content, dict):
                if content.get("__type__") == "file":
                    rel_src = content.get("relative_source") or key
                    filename = content.get("target_filename") or os.path.basename(key)
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
                    self._add_parent_paths(norm_src, paths, base_dir=base)
                    self._add_parent_paths(norm_dst, paths, base_dir=base)

                elif content.get("__type__") == "directory":
                    continue
                else:
                    sub_paths = self._extract_plan_paths(
                        content,
                        current_dest=os.path.join(current_dest, key),
                        base_dir=base,
                    )
                    paths.update(sub_paths)

        return paths

    @contextlib.contextmanager
    def scoped_move_phase(
        self, plan=None, active_paths=None, base_dir: Optional[str] = None
    ):
        """Context manager to scoped-suppress filesystem events during active file move execution."""
        effective_base = base_dir or self.base_dir
        paths_to_add = set()
        if active_paths:
            for p in active_paths:
                if p:
                    norm_p = os.path.normcase(os.path.abspath(p))
                    paths_to_add.add(norm_p)
                    self._add_parent_paths(
                        norm_p, paths_to_add, base_dir=effective_base
                    )
        if plan:
            try:
                from app.core.verifier import VerificationEngine

                moves = VerificationEngine.get_moves(effective_base, plan)
                for rel_src, src, dst in moves:
                    if src:
                        norm_s = os.path.normcase(os.path.abspath(src))
                        paths_to_add.add(norm_s)
                        self._add_parent_paths(
                            norm_s, paths_to_add, base_dir=effective_base
                        )
                    if dst:
                        norm_d = os.path.normcase(os.path.abspath(dst))
                        paths_to_add.add(norm_d)
                        self._add_parent_paths(
                            norm_d, paths_to_add, base_dir=effective_base
                        )
            except Exception:
                logger.debug(
                    "Could not resolve plan moves for the move guard", exc_info=True
                )
            paths_to_add.update(self._extract_plan_paths(plan, base_dir=effective_base))

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
        quarantine_dir_name = (
            getattr(self.settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine")
            or "_Compliance_Quarantine"
        )
        ignored_patterns = [
            ".autosorter",
            "sortify.db",
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
            "_Compliance_Quarantine",
            quarantine_dir_name,
        ]

        for pattern in ignored_patterns:
            if pattern in norm_path:
                return True

        # Also ignore any temporary folder/session folders
        from app.core.path_utils import get_session_base_dir
        from app.core.scanner import ScanFilter

        session_base = get_session_base_dir()
        if session_base.name in norm_path or str(session_base) in norm_path:
            return True

        owning_root = self._resolve_watched_root(path) or self.base_dir
        scan_filter = ScanFilter.from_settings(
            self.settings,
            base_dir=owning_root,
        )
        if (
            scan_filter.check_path(
                norm_path,
                base_dir=owning_root,
                check_depth=bool(owning_root),
                check_hidden=True,
            )
            is not None
        ):
            return True

        return False

    def enqueue_event(self, change_event: FileChangeEvent) -> bool:
        """Publish a FileChangeEvent directly to the asynchronous PriorityQueue with deduplication."""
        if not change_event or not change_event.file_path:
            return False

        file_path = change_event.file_path
        if self.should_ignore_path(file_path):
            return False

        norm_p = os.path.normcase(os.path.abspath(file_path))
        now = time.time()

        if change_event.priority_key is None:
            try:
                from app.core.shared_registry import SharedModelRegistry

                jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
                jev_res = jev_engine.classify(file_path)
                if jev_res:
                    change_event.archival_priority = getattr(
                        jev_res, "archival_priority", 5
                    )
                    change_event.archival_priority_score = getattr(
                        jev_res, "archival_priority_score", 0.0
                    )
            except Exception:
                logger.debug(
                    f"Jev priority classification failed for {file_path}",
                    exc_info=True,
                )
                change_event.archival_priority = 5
                change_event.archival_priority_score = 0.0

            change_event.priority_key = (
                change_event.archival_priority,
                -change_event.archival_priority_score,
            )

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

            self._queue_sequence_counter += 1
            seq = self._queue_sequence_counter
            self._events_enqueued_count += 1

        queue_entry = (change_event.priority_key, seq, change_event)

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
                    lambda: queue.put_nowait(queue_entry) if not queue.full() else None
                )
                return True
            else:
                try:
                    queue.put_nowait(queue_entry)
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

    def _get_or_create_session(self, root_dir: Optional[str] = None) -> AppSession:
        """Thread-safe lazy initialization of a per-watched-root AppSession."""
        with self._lock:
            target_root = os.path.abspath(root_dir) if root_dir else self.base_dir
            norm_root = os.path.normcase(target_root) if target_root else ""
            if (
                self._app_session is not None
                and self.base_dir
                and norm_root == os.path.normcase(self.base_dir)
            ):
                self._sessions_by_root[norm_root] = self._app_session
                return self._app_session

            existing = self._sessions_by_root.get(norm_root) if norm_root else None
            if existing is not None:
                return existing

            session = AppSession(self.settings, target_root)
            if norm_root:
                self._sessions_by_root[norm_root] = session
            if not self.base_dir or norm_root == os.path.normcase(self.base_dir):
                self._app_session = session
            return session

    def _start_pipeline_event_loop(self):
        """Initialize and launch the background asyncio event loop thread for streaming event processing."""
        from app.core.ipc import DaemonIPCServer
        from app.core.shared_registry import ContextPropagatingThread

        ready_event = threading.Event()

        def _loop_thread_main():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._event_loop = loop
            self._event_queue = asyncio.PriorityQueue(maxsize=self.max_queue_capacity)
            self._start_time = time.time()

            # Initialize and launch the IPC server on the daemon event loop
            self._ipc_server = DaemonIPCServer(self)
            try:
                loop.run_until_complete(self._ipc_server.start())
            except Exception as e:
                logger.error(f"Failed to start daemon IPC server: {e}", exc_info=True)

            self._worker_tasks = [
                loop.create_task(self._triage_worker(i))
                for i in range(self.num_workers)
            ]
            self._reconciliation_task = loop.create_task(self._reconciliation_worker())

            ready_event.set()

            try:
                loop.run_forever()
            finally:
                if self._ipc_server:
                    try:
                        self._ipc_server.stop()
                    except Exception:
                        logger.debug(
                            "Failed to stop IPC server on loop exit", exc_info=True
                        )
                    self._ipc_server = None
                for task in self._worker_tasks:
                    task.cancel()
                if self._reconciliation_task:
                    self._reconciliation_task.cancel()

                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )

                # Don't close the session here: cancelling a task doesn't stop
                # the move it started on the sort thread. stop() closes it once
                # that thread has drained.
                loop.close()

        self._execution_thread = ContextPropagatingThread(
            target=_loop_thread_main, daemon=True
        )
        self._execution_thread.start()
        ready_event.wait(timeout=5.0)

    async def _triage_worker(
        self, worker_id: int, cancel_token: Optional[threading.Event] = None
    ):
        """Async worker task that continuously consumes FileChangeEvents and triages paths."""
        logger.info(f"Async triage worker {worker_id} started.")
        token = cancel_token or self._cancel_event
        while self.is_running and not self._is_cancelled(token):
            if self.is_paused:
                await asyncio.sleep(0.05)
                continue
            try:
                if self._event_queue is None:
                    await asyncio.sleep(0.05)
                    continue
                raw_item = await self._event_queue.get()
            except (asyncio.CancelledError, RuntimeError):
                break

            if (
                isinstance(raw_item, tuple)
                and len(raw_item) == 3
                and isinstance(raw_item[2], FileChangeEvent)
            ):
                event = raw_item[2]
            elif isinstance(raw_item, FileChangeEvent):
                event = raw_item
            else:
                event = raw_item

            try:
                await self._process_single_event(event, cancel_token=token)
                with self._lock:
                    self._events_processed_count += 1
            except Exception as e:
                logger.error(
                    f"Worker {worker_id} error processing event {event}: {e}",
                    exc_info=True,
                )
            finally:
                if self._event_queue is not None:
                    self._event_queue.task_done()

    async def _process_single_event(
        self, event: FileChangeEvent, cancel_token: Optional[threading.Event] = None
    ):
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
                await self._triage_file_path(abs_path, cancel_token=cancel_token)
            finally:
                with self._lock:
                    self._active_triage_paths.discard(norm_p)

    async def _in_sort_thread(self, fn, /, *args, **kwargs):
        """Run blocking sort work on the daemon's sort thread and await it."""
        if self._sort_executor is None:
            self._sort_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="daemon-sort"
            )
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._sort_executor, functools.partial(fn, *args, **kwargs)
        )

    def _reload_settings(self, *, log_error: bool = False) -> None:
        """Flush any pending debounced settings save timer and reload settings from disk."""
        try:
            save_timer = getattr(self.settings, "_save_timer", None)
            if save_timer is not None:
                try:
                    save_timer.cancel()
                except Exception:
                    logger.debug("Failed to cancel settings save timer", exc_info=True)
                self.settings._save()
            self.settings.load()
        except Exception as e:
            if log_error:
                logger.error(f"Error loading settings dynamically: {e}", exc_info=True)
            else:
                logger.debug("Failed to reload settings before triage", exc_info=True)

    def _build_pipeline(
        self, app_session: Any, base_dir: Optional[str] = None
    ) -> SortPipeline:
        """Build a SortPipeline bound to daemon settings, owning root base_dir, and telemetry."""
        effective_base = (
            base_dir or getattr(app_session, "base_dir", None) or self.base_dir
        )
        pipeline = SortPipeline(
            app_session, base_dir=effective_base, settings=self.settings
        )
        pipeline.add_state_change_hook(
            lambda old_s, new_s, _ctx=None: self.emit_telemetry(
                "pipeline_state_change",
                old_state=old_s.value,
                new_state=new_s.value,
                pipeline_state=new_s.value,
            )
        )
        return pipeline

    def _get_folder_sort_lock(self, root_dir: Optional[str]) -> threading.RLock:
        """Return per-watched-root RLock serializing SortPipeline plan/execute phases."""
        key = os.path.normcase(os.path.abspath(root_dir)) if root_dir else "__default__"
        with self._lock:
            lock = self._folder_sort_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._folder_sort_locks[key] = lock
            return lock

    def _plan_and_execute_phase(
        self,
        pipeline: SortPipeline,
        items: list[str],
        *,
        execute_kwargs: Optional[dict[str, Any]] = None,
        root_dir: Optional[str] = None,
        batch_history_manager: Optional[Any] = None,
        **plan_kwargs: Any,
    ) -> tuple[Any, Any]:
        """Plan one phase via SortPipeline and execute moves inside scoped_move_phase."""
        from app.core.pipeline import PipelineLifecycleState

        effective_root = root_dir or pipeline.base_dir or self.base_dir
        with self._get_folder_sort_lock(effective_root):
            active_states = (
                PipelineLifecycleState.PLANNING,
                PipelineLifecycleState.EXECUTING,
                PipelineLifecycleState.ROLLING_BACK,
            )
            if pipeline.state in active_states:
                pipeline._state = PipelineLifecycleState.CREATED
            if (
                hasattr(pipeline.session, "lifecycle_state")
                and getattr(pipeline.session, "lifecycle_state", None) in active_states
            ):
                pipeline.session.lifecycle_state = PipelineLifecycleState.CREATED

            plan = pipeline.plan(items, **plan_kwargs)
            if not plan:
                return plan, None
            cancel_check = plan_kwargs.get("cancel_check")
            if cancel_check and cancel_check():
                return plan, None

            exec_kw: dict[str, Any] = dict(execute_kwargs or {})
            if (
                batch_history_manager is not None
                and effective_root
                and "session_id" not in exec_kw
            ):
                sid = self._acquire_folder_batch_session(
                    effective_root, batch_history_manager
                )
                if sid:
                    exec_kw["session_id"] = sid
                    exec_kw["complete_session"] = False

            move_ctx = (
                functools.partial(self.scoped_move_phase, base_dir=effective_root)
                if effective_root and effective_root != self.base_dir
                else self.scoped_move_phase
            )
            outcome = pipeline.execute(plan, move_context=move_ctx, **exec_kw)
            if outcome is not None and effective_root and "session_id" in exec_kw:
                moved_n = int(
                    outcome.summary.get("moved_files", 0) or len(outcome.moved)
                )
                self._record_folder_batch_moves(effective_root, moved_n)
            return plan, outcome

    async def _triage_file_path(
        self, abs_path: str, cancel_token: Optional[threading.Event] = None
    ):
        """Targeted triage on a single item path strictly within its owning watched root."""
        if not os.path.exists(abs_path):
            logger.debug(f"Target file missing before triage: {abs_path}")
            return

        if self.should_ignore_path(abs_path):
            return

        root_dir = self._resolve_watched_root(abs_path)
        if not root_dir:
            logger.debug("Skipping event outside active watched folders: %s", abs_path)
            return

        with self._lock:
            if os.path.normcase(root_dir) in self._paused_folders:
                logger.debug(
                    "Skipping triage for paused folder '%s': %s", root_dir, abs_path
                )
                return

        if is_subpath_or_equal(abs_path, root_dir):
            try:
                rel_path = safe_relpath(abs_path, root_dir).replace("\\", "/")
            except ValueError:
                norm_abs = os.path.abspath(abs_path)
                norm_base = os.path.abspath(root_dir)
                if norm_abs.lower().startswith(norm_base.lower()):
                    rel_path = (
                        norm_abs[len(norm_base) :].lstrip("\\/").replace("\\", "/")
                    )
                else:
                    rel_path = os.path.basename(abs_path)
        else:
            rel_path = os.path.basename(abs_path)

        app_session = self._get_or_create_session(root_dir)

        def cancel_check():
            return self._is_cancelled(cancel_token)

        self._reload_settings()

        if cancel_check():
            return

        raw_hm = getattr(app_session, "history_manager", None)
        batch_hm = (
            raw_hm
            if raw_hm is not None
            and isinstance(getattr(raw_hm, "db_path", None), (str, os.PathLike))
            else None
        )

        self._begin_folder_triage_activity(root_dir)
        try:
            # Phase 0: Quarantine Interceptor Staging & Compliance Policy Evaluation
            policies = getattr(self.settings, "POLICIES", [])
            worker_timeout = getattr(self.settings, "WORKER_TIMEOUT", 300.0)
            interceptor = QuarantineInterceptorService(
                db=app_session.db,
                policies=policies,
                worker_timeout=worker_timeout,
                runtime_settings=self.settings,
                history_manager=batch_hm,
            )

            try:
                staged_info = await self._in_sort_thread(
                    interceptor.stage_incoming_file,
                    source_path=abs_path,
                    base_dir=root_dir,
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

            if cancel_check():
                return

            open_batch_sid = (
                self._peek_folder_batch_session(root_dir)
                if batch_hm is not None
                else None
            )

            def _run_phase0_job():
                with self.scoped_move_phase(
                    active_paths=[abs_path, staged_filepath],
                    base_dir=root_dir,
                ):
                    return interceptor.process_quarantine_job(
                        job_id,
                        session_id=open_batch_sid,
                        complete_session=batch_hm is None,
                        history_manager=batch_hm,
                    )

            raw_record = await asyncio.to_thread(_run_phase0_job)

            if cancel_check():
                return

            quarantine_record = validate_quarantine_record(raw_record)
            recorded_phase0_sid = getattr(
                quarantine_record, "move_session_id", None
            ) or (
                raw_record.get("move_session_id")
                if isinstance(raw_record, dict)
                else None
            )
            if recorded_phase0_sid and batch_hm is not None:
                self._adopt_folder_batch_session(
                    root_dir, str(recorded_phase0_sid), batch_hm
                )

            status = quarantine_record.status
            policy_action = quarantine_record.policy_action

            # If file was quarantined, archived, or routed to DLQ / manual review, triage is complete
            if status in (
                "QUARANTINED",
                "ARCHIVED",
                "MANUAL_REVIEW_REQUIRED",
                "DEAD_LETTER_QUEUE",
            ):
                should_cleanup_origin = os.path.exists(abs_path) and (
                    (status in ("QUARANTINED", "ARCHIVED") and not recorded_phase0_sid)
                    or status in ("MANUAL_REVIEW_REQUIRED", "DEAD_LETTER_QUEUE")
                )
                if should_cleanup_origin:
                    with self.scoped_move_phase(
                        active_paths=[abs_path], base_dir=root_dir
                    ):
                        resilient_remove(abs_path)
                logger.info(
                    f"Quarantine interceptor completed for {rel_path} with status {status}"
                )
                return

            if policy_action == "unarchive" or status == "UNARCHIVED":
                unpacked_files = (
                    quarantine_record.get("unpacked_files")
                    or (
                        raw_record.get("unpacked_files")
                        if isinstance(raw_record, dict)
                        else None
                    )
                    or (
                        raw_record.get("extracted_files")
                        if isinstance(raw_record, dict)
                        else None
                    )
                    or []
                )
                if not unpacked_files and getattr(quarantine_record, "audit_log", None):
                    for entry in reversed(quarantine_record.audit_log):
                        if isinstance(entry, dict) and "unpacked_files" in entry:
                            unpacked_files = entry["unpacked_files"]
                            break

                registered_count = 0
                for child_path in unpacked_files:
                    if os.path.isfile(child_path) and not self.should_ignore_path(
                        child_path
                    ):
                        child_event = FileChangeEvent(
                            event_type="created",
                            file_path=child_path,
                            timestamp=time.time(),
                        )
                        if self.enqueue_event(child_event):
                            registered_count += 1

                if (
                    staged_filepath
                    and os.path.exists(staged_filepath)
                    and os.path.abspath(abs_path) != os.path.abspath(staged_filepath)
                ):
                    resilient_remove(staged_filepath)

                logger.info(
                    f"Quarantine interceptor unarchived {rel_path}: enqueued {registered_count} child files into triage loop"
                )
                return

            # If a compliance action (redact, archive, quarantine, retain, sensitivity_hold) was executed, triage is complete
            if policy_action in (
                "redact",
                "archive",
                "quarantine",
                "retain",
                "sensitivity_hold",
            ):
                if os.path.exists(abs_path) and not recorded_phase0_sid:
                    with self.scoped_move_phase(
                        active_paths=[abs_path], base_dir=root_dir
                    ):
                        resilient_remove(abs_path)
                logger.info(
                    f"Quarantine interceptor compliance action {policy_action} executed for {rel_path}"
                )
                return

            # Cleanup staging copy if present after quarantine release
            if (
                staged_filepath
                and os.path.exists(staged_filepath)
                and os.path.abspath(abs_path) != os.path.abspath(staged_filepath)
            ):
                resilient_remove(staged_filepath)

            # Phase 1: Fast-Path Rule Evaluation for released / non-sensitive files
            if not os.path.exists(abs_path):
                return

            pipeline = self._build_pipeline(app_session, base_dir=root_dir)

            _fast_plan, fast_outcome = await self._in_sort_thread(
                self._plan_and_execute_phase,
                pipeline,
                [rel_path],
                root_dir=root_dir,
                batch_history_manager=batch_hm,
                fast_path_only=True,
                target_paths=[rel_path],
                run_metadata_pass=True,
                cancel_check=cancel_check,
            )
            if fast_outcome is not None:
                logger.info(
                    f"Targeted Fast-Path triage completed for {rel_path}: {fast_outcome.summary}"
                )
                return

            if cancel_check():
                return

            if not os.path.exists(abs_path):
                logger.debug(f"Target file missing before triage: {abs_path}")
                return

            # Phase 1.5: Inline Non-Generative Jev Triage Phase
            try:
                from app.core.shared_registry import SharedModelRegistry

                jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
                jev_res = await self._in_sort_thread(jev_engine.classify, abs_path)
                if (
                    jev_res
                    and getattr(jev_res, "is_classified", False)
                    and getattr(jev_res, "confidence", 0.0) >= 0.5
                ):
                    _jev_plan, jev_outcome = await self._in_sort_thread(
                        self._plan_and_execute_phase,
                        pipeline,
                        [rel_path],
                        root_dir=root_dir,
                        batch_history_manager=batch_hm,
                        fast_path_only=True,
                        jev_results={rel_path: jev_res, abs_path: jev_res},
                        cancel_check=cancel_check,
                    )
                    if jev_outcome is not None:
                        logger.info(
                            f"Targeted Jev Fast-Path triage completed for {rel_path}: {jev_outcome.summary}"
                        )
                        return
            except Exception as e:
                logger.warning(
                    f"Jev fast-path triage failed for {rel_path}: {e}. Falling back to slow-path AI processing.",
                    exc_info=True,
                )

            if cancel_check():
                return

            if not os.path.exists(abs_path):
                return

            # Phase 2: Text Extraction, Incremental Model Training, and AI Planning via SortPipeline
            _slow_plan, slow_outcome = await self._in_sort_thread(
                self._plan_and_execute_phase,
                pipeline,
                [rel_path],
                root_dir=root_dir,
                batch_history_manager=batch_hm,
                fast_path_only=False,
                target_paths=[rel_path],
                cancel_check=cancel_check,
            )
            if slow_outcome is not None:
                logger.info(
                    f"Targeted Slow-Path AI triage completed for {rel_path}: {slow_outcome.summary}"
                )
        finally:
            self._end_folder_triage_activity(root_dir)

    async def _reconciliation_worker(
        self, cancel_token: Optional[threading.Event] = None
    ):
        """Periodic background reconciliation task executing low-priority directory audits."""
        logger.info("Background reconciliation task started.")
        token = cancel_token or self._cancel_event
        while self.is_running and not self._is_cancelled(token):
            try:
                await asyncio.sleep(self.reconciliation_interval)
            except asyncio.CancelledError:
                break

            if self._is_cancelled(token):
                break

            if (
                self._event_queue is not None
                and self._event_queue.empty()
                and not self.is_moving
            ):
                try:
                    await self._run_reconciliation_audit(cancel_token=token)
                except Exception as e:
                    logger.error(
                        f"Error during periodic background reconciliation: {e}",
                        exc_info=True,
                    )

    async def _run_reconciliation_audit(
        self,
        cancel_token: Optional[threading.Event] = None,
        roots: Optional[Sequence[str]] = None,
    ):
        """Low-priority directory audit across watched folders to enqueue untracked files."""
        with self._lock:
            target_roots = (
                list(roots) if roots is not None else list(self._watched_folders)
            )
            if not target_roots and self.base_dir:
                target_roots = [self.base_dir]
            paused_norms = set(self._paused_folders)

        valid_roots = [
            r
            for r in target_roots
            if r and os.path.isdir(r) and os.path.normcase(r) not in paused_norms
        ]
        if not valid_roots:
            return

        token = cancel_token or self._cancel_event

        def _scan() -> list[str]:
            from app.core.scanner import ScanFilter, active_scan_filter

            discovered: list[str] = []
            for root in valid_roots:
                if self._is_cancelled(token):
                    break
                sf = ScanFilter.from_settings(self.settings, base_dir=root)
                with active_scan_filter(sf):
                    rel_files = get_files_recursively(root)
                for f in rel_files:
                    full_p = f if os.path.isabs(f) else os.path.join(root, f)
                    if not self.should_ignore_path(full_p):
                        discovered.append(full_p)
            return discovered

        scanned_files = await asyncio.to_thread(_scan)
        now = time.time()
        enqueued_count = 0

        for file_path in scanned_files:
            if self._is_cancelled(token):
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
            if self._is_cancelled(token):
                break
            if self.enqueue_event(change_event):
                enqueued_count += 1

        if enqueued_count > 0:
            logger.info(
                f"Reconciliation audit enqueued {enqueued_count} untracked files."
            )

    def _execute_dropped_items(
        self,
        validated_paths: list[str],
        *,
        dry_run: bool = False,
        dest_dir: Optional[str] = None,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run SortPipeline over pre-validated drop target paths."""
        _ = dest_dir
        processed_items = []
        error_list = list(errors) if errors is not None else []

        for abs_path in validated_paths:
            try:
                if not (os.path.isdir(abs_path) or os.path.isfile(abs_path)):
                    continue
                base_dir, files = drop_scope(abs_path)
                session = AppSession(self.settings, base_dir=base_dir)
                try:
                    plan, outcome = SortPipeline(session).run(files, dry_run=dry_run)
                    if outcome is None:
                        summary = {
                            "status": "dry_run",
                            "plan": _make_json_serializable(plan),
                        }
                    else:
                        summary = outcome.summary
                    processed_items.append(
                        {
                            "path": abs_path,
                            "type": "directory" if files is None else "file",
                            "summary": summary,
                        }
                    )
                finally:
                    session.close()
            except Exception as e:
                logger.error(
                    f"Error processing dropped path {abs_path}: {e}", exc_info=True
                )
                error_list.append(f"Processing error for {abs_path}: {e}")

        status_res = (
            "success" if processed_items else ("error" if error_list else "no_op")
        )
        return {
            "status": status_res,
            "processed_count": len(processed_items),
            "processed_items": processed_items,
            "errors": error_list,
        }

    def process_dropped_items(
        self,
        paths: list[str],
        dry_run: bool = False,
        dest_dir: Optional[str] = None,
    ) -> dict[str, Any]:
        """Process dropped file or directory path payloads through active triage pipeline."""
        validated_paths, issues = validate_drop_paths(
            paths,
            settings=self.settings,
            ignore_predicate=self.should_ignore_path,
        )
        errors: list[str] = []
        for issue in issues:
            if issue.reason == "missing":
                errors.append(f"Path does not exist: {issue.clean_path}")
            elif issue.reason == "ignored":
                logger.info(f"Skipping ignored dropped path: {issue.abs_path}")
            elif issue.reason == "protected":
                errors.append(f"Protected path blocked: {issue.abs_path}")

        return self._execute_dropped_items(
            validated_paths,
            dry_run=dry_run,
            dest_dir=dest_dir,
            errors=errors,
        )

    def start(self):
        """Start the continuous watchdog daemon, workers, event loop, and file system observer."""
        with self._lock:
            if self._state == DaemonState.RUNNING:
                return
            self._transition_to(DaemonState.RUNNING)
            self._queue_sequence_counter = 0
            self._move_ref_count = 0
            self._pending_dirty = False
            self._active_move_paths.clear()
            self._cancel_event.clear()
            self._scoped_run_token = None
            self._recent_events.clear()
            self._active_path_locks.clear()
            self._active_triage_paths.clear()
            self._paused_folders.clear()
            self._watch_handles.clear()
            self._sorting_process = None
            self._proc_cancel_event = None
            self._rerun_pending = False
            folders_to_watch = list(self._watched_folders)
            if not folders_to_watch and self.base_dir:
                folders_to_watch = [self.base_dir]
                self._watched_folders = [self.base_dir]

        target_desc = (
            ", ".join(folders_to_watch)
            if folders_to_watch
            else (self.base_dir or "<idle>")
        )
        logger.info(f"Starting continuous watchdog daemon for: {target_desc}")

        # Launch background asyncio event loop & worker pool
        self._start_pipeline_event_loop()

        # Start single watchdog observer across all configured watched folders
        self.observer = Observer()
        self._folder_handler = DaemonFolderHandler(self)
        with self._lock:
            for folder in list(self._watched_folders):
                if os.path.isdir(folder):
                    norm_f = os.path.normcase(folder)
                    try:
                        self._watch_handles[norm_f] = self.observer.schedule(
                            self._folder_handler, folder, recursive=True
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to schedule watch for '%s': %s",
                            folder,
                            exc,
                            exc_info=True,
                        )
        self.observer.start()

        if hasattr(self.settings, "add_observer"):

            def _on_watched_folders_updated(new_folders: Any) -> None:
                if self.is_running:
                    self.update_watched_folders(list(new_folders or []))

            self._settings_observer_cb = _on_watched_folders_updated
            with contextlib.suppress(Exception):
                self.settings.add_observer(
                    "WATCHED_FOLDERS", _on_watched_folders_updated
                )

        # Trigger initial low-priority reconciliation audit
        if self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._run_reconciliation_audit())
            )

    def stop(self):
        """Stop the continuous watchdog daemon, workers, observer, and event loop."""
        with self._lock:
            if self._state == DaemonState.STOPPED:
                return
            self._transition_to(DaemonState.STOPPED)
            self._move_ref_count = 0
            self._pending_dirty = False
            self._rerun_pending = False
            self._active_move_paths.clear()
            self._paused_folders.clear()
            self._cancel_event.set()
            if self._scoped_run_token is not None:
                self._scoped_run_token.set()
            if self._proc_cancel_event is not None:
                try:
                    self._proc_cancel_event.set()
                except Exception:
                    logger.debug("Failed to signal worker cancel event", exc_info=True)
            self._first_event_time = None

            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None

            obs_cb = self._settings_observer_cb
            self._settings_observer_cb = None

            proc = self._sorting_process
            self._sorting_process = None
            self._proc_cancel_event = None

        if obs_cb is not None and hasattr(self.settings, "remove_observer"):
            with contextlib.suppress(Exception):
                self.settings.remove_observer("WATCHED_FOLDERS", obs_cb)

        if proc is not None:
            if proc.is_alive():
                try:
                    proc.terminate()
                    proc.join(timeout=3.0)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=1.0)
                except Exception as e:
                    logger.error(
                        f"Error terminating background worker process: {e}",
                        exc_info=True,
                    )
            else:
                try:
                    proc.join(timeout=1.0)
                except Exception:
                    logger.debug("Failed to join worker process", exc_info=True)
            try:
                proc.close()
            except Exception:
                logger.debug("Failed to close worker process", exc_info=True)

        if self._ipc_server:
            try:
                self._ipc_server.stop()
            except Exception as e:
                logger.error(f"Error stopping IPC server: {e}", exc_info=True)
            finally:
                self._ipc_server = None

        if self.observer:
            try:
                self.observer.stop()
                self.observer.join()
            except Exception as e:
                logger.error(f"Error stopping observer: {e}", exc_info=True)
            finally:
                self.observer = None
                with self._lock:
                    self._watch_handles.clear()
                    self._folder_handler = None

        loop = self._event_loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

        if self._execution_thread and self._execution_thread.is_alive():
            self._execution_thread.join(timeout=3.0)

        # Let an in-flight move finish before the session it uses is closed.
        executor, self._sort_executor = self._sort_executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

        # Complete any open quiet-window undo sessions before closing AppSessions.
        self.flush_all_undo_batches()

        self._event_loop = None
        self._event_queue = None

        with self._lock:
            sessions_to_close: list[AppSession] = []
            seen_ids: set[int] = set()
            primary = getattr(self, "_app_session", None)
            if primary is not None:
                seen_ids.add(id(primary))
                sessions_to_close.append(primary)
            for sess in self._sessions_by_root.values():
                if id(sess) not in seen_ids:
                    seen_ids.add(id(sess))
                    sessions_to_close.append(sess)
            self._app_session = None
            self._sessions_by_root.clear()

        for sess in sessions_to_close:
            try:
                sess.close()
            except Exception:
                logger.debug("Failed to close daemon app session", exc_info=True)

        logger.info("Watchdog daemon stopped.")

    def trigger_recalculation(self):
        """Thread-safe and debounced trigger for sorting run."""
        with self._lock:
            if not self.is_running or self._move_ref_count > 0:
                return

            # Track the start time of the first event in a sequence
            now = time.time()
            first_event_time = getattr(self, "_first_event_time", None)
            if first_event_time is None:
                first_event_time = now
                self._first_event_time = first_event_time

            elapsed = now - first_event_time
            debounce_delay = getattr(self.settings, "DEBOUNCE_DELAY", 0.6)
            max_debounce_delay = getattr(self.settings, "MAX_DEBOUNCE_DELAY", 5.0)

            if elapsed >= max_debounce_delay:
                # Under continuous event traffic, if the max debounce delay has been reached,
                # we let the already scheduled timer execute rather than canceling and rescheduling it.
                # This guarantees that the run initiates and doesn't get starved by rapid events.
                return

            if self._state != DaemonState.RECALCULATING:
                self._transition_to(DaemonState.RECALCULATING)

            # Interrupt current run immediately by signaling active scoped run token
            if self._scoped_run_token is not None:
                self._scoped_run_token.set()
            if self._proc_cancel_event is not None:
                try:
                    self._proc_cancel_event.set()
                except Exception:
                    logger.debug("Failed to signal worker cancel event", exc_info=True)

            # Create a new scoped run token for the upcoming run without replacing daemon lifecycle token
            self._scoped_run_token = threading.Event()

            max_delay = max(0.0, max_debounce_delay - elapsed)
            delay = min(debounce_delay, max_delay)

            if self._debounce_timer:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(
                delay, self._schedule_run, args=(self._scoped_run_token,)
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _schedule_run(self, cancel_event):
        """Timer callback that executes or queues the worker subprocess execution."""
        with self._lock:
            if not self.is_running or (
                cancel_event is not None and cancel_event.is_set()
            ):
                if (
                    self._state == DaemonState.RECALCULATING
                    and self._scoped_run_token is cancel_event
                ):
                    self._transition_to(DaemonState.RUNNING)
                return
            self._first_event_time = None

            loop = self._event_loop
            if loop is not None and loop.is_running():
                if self._state == DaemonState.RECALCULATING:
                    self._transition_to(DaemonState.RUNNING)
                with contextlib.suppress(RuntimeError):
                    asyncio.run_coroutine_threadsafe(
                        self._run_reconciliation_audit(cancel_token=cancel_event),
                        loop,
                    )
                return

            if self._sorting_process is not None and self._sorting_process.is_alive():
                self._rerun_pending = True
                logger.info(
                    "Background sorting worker process active; queueing subsequent run."
                )
                return

            self._spawn_sorting_process(cancel_event)

    def _spawn_sorting_process(self, cancel_event=None):
        """Spawn a process-isolated worker subprocess to execute the sorting routine."""
        with self._lock:
            if not self.is_running:
                return
            self._rerun_pending = False

            config_filepath = getattr(self.settings, "filepath", None)

            ctx = multiprocessing.get_context("spawn")
            mp_cancel_event = ctx.Event()
            self._proc_cancel_event = mp_cancel_event

            proc = ctx.Process(
                target=_run_sorting_process_entry,
                args=(self.base_dir, config_filepath, mp_cancel_event),
                daemon=False,
            )
            self._sorting_process = proc
            proc.start()

            monitor_thread = threading.Thread(
                target=self._monitor_sorting_process, args=(proc,), daemon=True
            )
            monitor_thread.start()

    def _monitor_sorting_process(self, proc: Any):
        """Monitor the lifecycle of the worker subprocess and trigger queued runs upon completion."""
        try:
            proc.join()
        except Exception:
            logger.debug("Failed to join worker process", exc_info=True)

        try:
            proc.close()
        except Exception:
            logger.debug("Failed to close worker process", exc_info=True)

        should_rerun = False
        with self._lock:
            if self._sorting_process is proc:
                self._sorting_process = None
                self._proc_cancel_event = None

            if self.is_running and self._rerun_pending:
                self._rerun_pending = False
                should_rerun = True
            elif self._state == DaemonState.RECALCULATING:
                self._transition_to(DaemonState.RUNNING)

        if should_rerun:
            logger.info(
                "Active sorting process completed. Executing queued sorting run."
            )
            self.trigger_recalculation()

    def _run_sorting_sync(self, cancel_event):
        """Run the core synchronous sorting runner."""
        # Check if canceled before starting
        if cancel_event.is_set():
            with self._lock:
                if (
                    self._state == DaemonState.RECALCULATING
                    and self._scoped_run_token is cancel_event
                ):
                    self._transition_to(DaemonState.RUNNING)
            return

        logger.info("Executing background sorting run...")

        # Define the cancel check callback
        def cancel_check():
            return cancel_event.is_set() or self._is_cancelled()

        # Dynamic reload of settings
        self._reload_settings(log_error=True)

        app_session = None
        try:
            app_session = AppSession(self.settings, self.base_dir)
            pipeline = self._build_pipeline(app_session)

            from app.core.scanner import ScanFilter, active_scan_filter

            daemon_scan_filter = ScanFilter.from_settings(
                self.settings, base_dir=self.base_dir
            )

            # PHASE 1: Fast-Path (Deterministic Rules)
            if cancel_check():
                return
            with active_scan_filter(daemon_scan_filter):
                files = get_files_recursively(self.base_dir)
            files = [f for f in files if not self.should_ignore_path(f)]

            if not files:
                logger.info("No files found to organize.")
                return

            fast_path_plan, fast_outcome = self._plan_and_execute_phase(
                pipeline,
                files,
                fast_path_only=True,
                run_metadata_pass=True,
                cancel_check=cancel_check,
            )
            fast_path_moved_destinations: set[str] = set()

            if fast_outcome is not None:
                logger.info(f"Phase 1 (Fast-Path) completed: {fast_outcome.summary}")
                fast_path_moved_destinations = _extract_plan_destinations(
                    fast_path_plan, base_dir=self.base_dir
                )

            if cancel_check():
                return

            # REFRESH STATE: Refresh scanned directory state & update file paths in memory to prevent path corruption
            app_session.db.invalidate_cache()
            with active_scan_filter(daemon_scan_filter):
                files = get_files_recursively(self.base_dir)
            files = [f for f in files if not self.should_ignore_path(f)]

            if not files:
                logger.info("No remaining files to organize.")
                return

            # PHASE 2: Slow-Path (AI Classification & Clustering) via SortPipeline
            slow_path_plan = pipeline.plan(
                files,
                fast_path_only=False,
                run_metadata_pass=True,
                bypassed_items=fast_path_moved_destinations,
                cancel_check=cancel_check,
            )

            if cancel_check():
                return

            if not slow_path_plan:
                logger.info("No AI sorting actions needed.")
                return

            slow_outcome = pipeline.execute(
                slow_path_plan, move_context=self.scoped_move_phase
            )
            logger.info(
                f"Phase 2 (Slow-Path AI) completed successfully: {slow_outcome.summary}"
            )

        except Exception as e:
            logger.error(
                f"Error during continuous watchdog execution run: {e}", exc_info=True
            )
        finally:
            with self._lock:
                if (
                    self._state == DaemonState.RECALCULATING
                    and self._scoped_run_token is cancel_event
                ):
                    self._transition_to(DaemonState.RUNNING)
            if app_session:
                app_session.close()


def start_daemon(
    settings: AppSettings,
    base_dir: str | None = None,
    watched_folders: Sequence[str] | None = None,
):
    """Start the persistent directory-watching daemon service."""
    if watched_folders is not None:
        daemon = ContinuousWatchdogDaemon(
            settings, base_dir=base_dir, watched_folders=watched_folders
        )
    elif base_dir:
        daemon = ContinuousWatchdogDaemon(settings, base_dir=base_dir)
    else:
        configured = list(getattr(settings, "WATCHED_FOLDERS", []) or [])
        if configured:
            daemon = ContinuousWatchdogDaemon(settings, watched_folders=configured)
        else:
            daemon = ContinuousWatchdogDaemon(settings, base_dir=os.getcwd())

    daemon.start()

    # Keep the main thread alive while monitoring
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        daemon.stop()
