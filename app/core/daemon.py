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
from app.core.path_utils import is_subpath_or_equal, safe_relpath
from app.core.pipeline import SortPipeline, drop_scope, validate_drop_paths
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.resilient_file_ops import resilient_remove
from app.core.scanner import get_files_recursively
from app.core.session import AppSession

logger = logging.getLogger("app.daemon")


def _normalize_watch_root(path_str: str) -> str:
    """Normalize an absolute watched directory path while preserving symlink entry paths."""
    expanded = os.path.expanduser(path_str.strip())
    return os.path.normpath(os.path.abspath(expanded))


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
        abs_p = _normalize_watch_root(cleaned)
        real_p = os.path.normpath(os.path.realpath(abs_p))
        key = (os.path.normcase(abs_p), os.path.normcase(real_p))
        if key not in seen:
            seen.add(key)
            normalized.append(abs_p)
    return normalized


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
    base_dir: str | list[str],
    config_filepath: Optional[str] = None,
    cancel_event: Optional[Any] = None,
) -> None:
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
    """Tracks an open quiet-window undo session for a single watched root folder."""

    root_dir: str
    session_id: Optional[str] = None
    history_manager: Optional[Any] = None
    opened_at: float = 0.0
    last_activity_at: float = 0.0
    in_flight_count: int = 0
    move_count: int = 0
    timer: Optional[threading.Timer] = None


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
    """Handler for file system events inside monitored directories."""

    def __init__(self, daemon: Any):
        self.daemon = daemon

    def on_any_event(self, event: Any) -> None:
        """Handle any file system event, publish FileChangeEvent to queue and trigger recalculation if valid."""
        if getattr(event, "is_directory", False) is True:
            return
        event_type = getattr(event, "event_type", "modified")
        if event_type in ("opened", "closed_no_write"):
            return

        src_path = getattr(event, "src_path", "")
        dest_path = getattr(event, "dest_path", None)

        if self.daemon.should_ignore_path(src_path):
            return
        if dest_path and self.daemon.should_ignore_path(dest_path):
            return

        # Suppress recalculation triggers if file moves are actively running,
        # but mark dirty flag when running without the live async triage loop
        if getattr(self.daemon, "is_moving", False):
            if self.daemon.is_self_generated_event(event):
                return
            loop = getattr(self.daemon, "_event_loop", None)
            if loop is None or not loop.is_running():
                self.daemon.mark_pending_dirty()
                return

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
    """Daemon that continuously monitors one or more watched folders using a shared async queue pipeline."""

    def __init__(
        self,
        settings: AppSettings,
        base_dir: str | Sequence[str] | None = None,
        watched_folders: Optional[Sequence[str]] = None,
    ):
        self.settings = settings
        initial_roots, tracks_cfg = self._collect_initial_roots(
            settings, base_dir, watched_folders
        )
        self._watched_folders: list[str] = initial_roots
        self._tracks_settings_watches: bool = tracks_cfg
        self.base_dir: str = (
            initial_roots[0]
            if initial_roots
            else (
                os.path.abspath(base_dir)
                if isinstance(base_dir, str) and base_dir
                else ""
            )
        )
        self.observer: Optional[Any] = None
        self._folder_handler = DaemonFolderHandler(self)
        self._watch_handles: dict[str, Any] = {}
        self._paused_folders: set[str] = set()
        self._settings_observer_cb: Optional[Callable[[Any], None]] = None

        self._state: DaemonState = DaemonState.STOPPED
        self._lock = threading.RLock()
        self._debounce_timer: Optional[threading.Timer] = None
        self._cancel_event = threading.Event()
        self._scoped_run_token: Optional[threading.Event] = None
        self._move_ref_count = 0
        self._pending_dirty = False
        self._active_move_paths: set[str] = set()
        self._first_event_time: Optional[float] = None

        # Configurable properties for async event pipeline
        self.max_queue_capacity = getattr(settings, "MAX_QUEUE_CAPACITY", 1000)
        self.dedup_window = getattr(settings, "DEDUP_WINDOW", 0.5)
        self.num_workers = getattr(settings, "MAX_WORKERS", 4)
        self.reconciliation_interval = getattr(
            settings, "RECONCILIATION_INTERVAL", 30.0
        )

        # Pipeline state
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_queue: Optional[asyncio.PriorityQueue[Any]] = None
        self._recent_events: dict[str, float] = {}
        self._recent_event_sizes: dict[str, int] = {}
        self._active_path_locks: dict[str, asyncio.Lock] = {}
        self._active_triage_paths: set[str] = set()
        self._pending_retriage_paths: set[str] = set()
        self._worker_tasks: list[asyncio.Task[Any]] = []
        self._reconciliation_task: Optional[asyncio.Task[Any]] = None
        self._app_session: Optional[AppSession] = None
        self._sessions_by_root: dict[str, AppSession] = {}
        self._folder_sort_locks: dict[str, threading.RLock] = {}
        self._undo_batches: dict[str, _FolderUndoBatch] = {}
        self._undo_quiet_window_override: Optional[float] = None
        self._queue_sequence_counter: int = 0

        # Telemetry & IPC Server State
        self._events_enqueued_count: int = 0
        self._events_processed_count: int = 0
        self._start_time: Optional[float] = None
        self._ipc_server: Any = None

        # Dedicated background execution thread
        self._execution_thread: Optional[threading.Thread] = None

        # Sort executor with per-folder lock serialization so separate watched
        # folders can triage independently while each folder's analyzer/session is
        # accessed safely (#717).
        self._sort_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None

        # Process-isolated worker management
        self._sorting_process: Optional[multiprocessing.process.BaseProcess] = None
        self._proc_cancel_event: Optional[Any] = None
        self._rerun_pending: bool = False

    @staticmethod
    def _collect_initial_roots(
        settings: Any,
        base_dir: str | Sequence[str] | None,
        watched_folders: Optional[Sequence[str]],
    ) -> tuple[list[str], bool]:
        if watched_folders is not None:
            candidates = list(watched_folders)
            tracks_cfg = True
        elif base_dir is not None:
            if isinstance(base_dir, str):
                candidates = [base_dir] if base_dir.strip() else []
                tracks_cfg = False
            else:
                candidates = list(base_dir)
                tracks_cfg = True
        else:
            cfg_watched = list(getattr(settings, "WATCHED_FOLDERS", []) or [])
            if cfg_watched:
                candidates = cfg_watched
                tracks_cfg = True
            else:
                candidates = [os.getcwd()]
                tracks_cfg = False

        normalized = _normalize_folder_list(candidates)
        deduped: list[str] = []
        for norm in normalized:
            if any(
                is_subpath_or_equal(norm, existing)
                or is_subpath_or_equal(existing, norm)
                for existing in deduped
            ):
                continue
            deduped.append(norm)
        return deduped, tracks_cfg

    def get_watched_folders(self) -> list[str]:
        """Return a snapshot list of normalized watched folder paths."""
        with self._lock:
            return list(self._watched_folders)

    @property
    def watched_folders(self) -> list[str]:
        """Watched root folders monitored by this daemon instance."""
        return self.get_watched_folders()

    @watched_folders.setter
    def watched_folders(self, folders: Sequence[str]) -> None:
        self.update_watched_folders(folders)

    def _list_paused_folders(self) -> list[str]:
        """Return watched folders that are currently paused individually."""
        with self._lock:
            return [
                f
                for f in self._watched_folders
                if os.path.normcase(f) in self._paused_folders
            ]

    def get_paused_folders(self) -> list[str]:
        """Return watched folders that are currently paused individually."""
        return self._list_paused_folders()

    def _resolve_watched_root(self, path: str) -> Optional[str]:
        """Find the watched root folder that contains ``path`` (deepest match wins)."""
        if not path:
            return None
        abs_p = os.path.normpath(os.path.abspath(path))
        with self._lock:
            roots = list(self._watched_folders)
            fallback = self.base_dir
        best: Optional[str] = None
        for root in roots:
            with contextlib.suppress(OSError, ValueError, TypeError):
                if is_subpath_or_equal(abs_p, root):
                    if best is None or len(root) > len(best):
                        best = root
        if best is not None:
            return best
        if not roots and fallback:
            with contextlib.suppress(Exception):
                if is_subpath_or_equal(abs_p, fallback):
                    return fallback
        return None

    def _is_folder_paused(self, root_dir: Optional[str]) -> bool:
        if not root_dir:
            return self.is_paused
        with self._lock:
            if self._state == DaemonState.PAUSED:
                return True
            return os.path.normcase(root_dir) in self._paused_folders

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

    def pause(self) -> None:
        """Pause processing of queued file triage events across all watched folders."""
        with self._lock:
            self._transition_to(DaemonState.PAUSED)
            self._paused_folders = {os.path.normcase(f) for f in self._watched_folders}
        self.flush_all_undo_batches()
        logger.info("Daemon triage processing paused.")

    def resume(self) -> None:
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
        """Pause triage for specific watched folders without stopping other roots."""
        targets = {os.path.normcase(f) for f in _normalize_folder_list(folders)}
        matched: list[str] = []
        with self._lock:
            for watched in self._watched_folders:
                key = os.path.normcase(watched)
                if key in targets:
                    self._paused_folders.add(key)
                    matched.append(watched)
            if (
                self._watched_folders
                and all(
                    os.path.normcase(w) in self._paused_folders
                    for w in self._watched_folders
                )
                and self._state != DaemonState.STOPPED
            ):
                self._transition_to(DaemonState.PAUSED)
        for folder in matched:
            self._flush_folder_batch_session(folder, force=True)
        return {
            "status": self.state.value,
            "paused_folders": self._list_paused_folders(),
            "watched_folders": self.watched_folders,
            "matched_folders": matched,
        }

    def resume_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Resume triage for specific watched folders."""
        targets = {os.path.normcase(f) for f in _normalize_folder_list(folders)}
        resumed_roots: list[str] = []
        with self._lock:
            if self._state == DaemonState.STOPPED:
                raise InvalidStateError("Cannot resume daemon when stopped")
            for watched in self._watched_folders:
                key = os.path.normcase(watched)
                if key in targets:
                    self._paused_folders.discard(key)
                    resumed_roots.append(watched)
            if self._state == DaemonState.PAUSED and any(
                os.path.normcase(w) not in self._paused_folders
                for w in self._watched_folders
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
            "matched_folders": resumed_roots,
        }

    def _schedule_folder_watch(self, folder: str) -> bool:
        """Attach a watchdog schedule handle for ``folder`` if observer is active."""
        obs = self.observer
        if obs is None:
            return False
        key = os.path.normcase(folder)
        with self._lock:
            if key in self._watch_handles:
                return True
        if not os.path.isdir(folder):
            logger.warning(f"Watched folder does not exist yet on disk: {folder}")
            return False
        try:
            watch = obs.schedule(self._folder_handler, folder, recursive=True)
            with self._lock:
                self._watch_handles[key] = watch
            logger.info(f"Scheduled watchdog observer watch for: {folder}")
            return True
        except Exception as e:
            logger.error(
                f"Failed to schedule watchdog observer for '{folder}': {e}",
                exc_info=True,
            )
            return False

    def _unschedule_folder_watch(self, folder: str) -> None:
        """Detach watchdog schedule handle and close cached session for ``folder``."""
        key = os.path.normcase(folder)
        with self._lock:
            watch = self._watch_handles.pop(key, None)
            self._paused_folders.discard(key)
            session = self._sessions_by_root.pop(key, None)
            if self._app_session is session:
                self._app_session = None
            if self.base_dir:
                primary = self._sessions_by_root.get(os.path.normcase(self.base_dir))
                if primary is not None:
                    self._app_session = primary

        obs = self.observer
        if obs is not None and watch is not None:
            with contextlib.suppress(Exception):
                obs.unschedule(watch)

        self._flush_folder_batch_session(folder, force=True)
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()

    def update_watched_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Dynamically reconcile active watchdog watches to match ``folders`` without restarting."""
        normalized_candidates = _normalize_folder_list(folders)
        next_roots: list[str] = []
        for norm in normalized_candidates:
            if any(
                is_subpath_or_equal(norm, existing)
                or is_subpath_or_equal(existing, norm)
                for existing in next_roots
            ):
                raise ValueError(
                    f"Overlapping watched folders are not allowed: '{norm}'"
                )
            if os.path.isdir(norm):
                next_roots.append(norm)

        with self._lock:
            self._tracks_settings_watches = True
            prev_map = {os.path.normcase(f): f for f in self._watched_folders}
            next_map = {os.path.normcase(f): f for f in next_roots}
            added = [f for k, f in next_map.items() if k not in prev_map]
            removed = [f for k, f in prev_map.items() if k not in next_map]
            self._watched_folders = list(next_roots)
            self.base_dir = next_roots[0] if next_roots else ""
            self._paused_folders.intersection_update(next_map.keys())

        for rem in removed:
            self._unschedule_folder_watch(rem)

        for add in added:
            self._schedule_folder_watch(add)

        if self._ipc_server is not None and hasattr(
            self._ipc_server, "refresh_pid_file"
        ):
            with contextlib.suppress(Exception):
                self._ipc_server.refresh_pid_file()

        if added and self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._run_reconciliation_audit(roots=added))
            )

        return {
            "status": "updated",
            "watched_folders": self.get_watched_folders(),
            "added": added,
            "removed": removed,
            "removed_count": len(removed),
        }

    def reload_watched_folders(
        self,
        watched_folders: Optional[Sequence[str]] = None,
        add_folders: Optional[Sequence[str]] = None,
        remove_folders: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        """Reload settings from disk and reconcile WATCHED_FOLDERS dynamically."""
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
                        if os.path.normcase(_normalize_watch_root(f)) not in rem_norms
                    ]
        self._tracks_settings_watches = True
        res = self.update_watched_folders(target_folders)
        return {
            **res,
            "status": "reloaded",
        }

    def stop_watching_folders(self, folders: Sequence[str]) -> dict[str, Any]:
        """Unschedule specific watched folder(s); signal full daemon stop if none remain."""
        remove_keys = {os.path.normcase(f) for f in _normalize_folder_list(folders)}
        with self._lock:
            remaining = [
                f
                for f in self._watched_folders
                if os.path.normcase(f) not in remove_keys
            ]
            removed = [
                f for f in self._watched_folders if os.path.normcase(f) in remove_keys
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
            "remaining_folders": remaining,
            "removed_folders": removed,
            "stopped_daemon": should_stop,
            "should_stop_daemon": should_stop,
        }

    def _on_watched_folders_setting_changed(self, new_folders: Any) -> None:
        """Observer callback invoked when AppSettings.WATCHED_FOLDERS changes in-process."""
        if not self.is_running or not isinstance(new_folders, list):
            return
        try:
            self.update_watched_folders(list(new_folders))
        except Exception:
            logger.warning(
                "Failed to apply dynamic WATCHED_FOLDERS update", exc_info=True
            )

    def get_status_telemetry(self) -> dict[str, Any]:
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
            paused_list = [
                f for f in watched if os.path.normcase(f) in self._paused_folders
            ]
            folder_states = {
                f: (
                    "paused"
                    if (paused or os.path.normcase(f) in self._paused_folders)
                    else ("watching" if os.path.isdir(f) else "missing")
                )
                for f in watched
            }

        return {
            "status": curr_state.value,
            "is_paused": paused,
            "is_running": running,
            "pid": os.getpid(),
            "base_dir": self.base_dir,
            "watched_folders": watched,
            "watched_folder_count": len(watched),
            "paused_folders": paused_list,
            "folder_states": folder_states,
            "queue_depth": queue_depth,
            "max_queue_capacity": self.max_queue_capacity,
            "active_workers": active_workers,
            "busy_workers": len(active_triages),
            "active_triage_paths": active_triages,
            "is_moving": self.is_moving,
            "pending_dirty": self.pending_dirty,
            "uptime_seconds": round(uptime, 2),
        }

    def get_health_telemetry(self) -> dict[str, Any]:
        """Get process health check telemetry for IPC monitoring."""
        queue_depth = self._event_queue.qsize() if self._event_queue is not None else 0
        queue_full = (
            self._event_queue.full() if self._event_queue is not None else False
        )
        uptime = time.time() - self._start_time if self._start_time else 0.0
        with self._lock:
            paused = self._is_paused
            watched = list(self._watched_folders)

        status_str = "paused" if paused else ("degraded" if queue_full else "ok")
        return {
            "status": status_str,
            "uptime_seconds": round(uptime, 2),
            "queue_full": queue_full,
            "queue_depth": queue_depth,
            "watched_folders": watched,
            "pid": os.getpid(),
        }

    def get_metrics_telemetry(self) -> dict[str, Any]:
        """Get cumulative performance and event metrics for IPC monitoring."""
        queue_depth = self._event_queue.qsize() if self._event_queue is not None else 0
        active_workers = len([t for t in self._worker_tasks if not t.done()])
        uptime = time.time() - self._start_time if self._start_time else 0.0
        with self._lock:
            enqueued = self._events_enqueued_count
            processed = self._events_processed_count
            watched_count = len(self._watched_folders)

        return {
            "events_enqueued": enqueued,
            "events_processed": processed,
            "queue_depth": queue_depth,
            "max_queue_capacity": self.max_queue_capacity,
            "active_workers": active_workers,
            "watched_folder_count": watched_count,
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

    def mark_pending_dirty(self) -> None:
        """Thread-safe mark of pending dirty state."""
        with self._lock:
            if self._is_running:
                self._pending_dirty = True

    def _resolve_event_path(
        self, file_path: str, base_dir: Optional[str] = None
    ) -> str:
        """Resolve relative event paths against ``base_dir`` or ``self.base_dir`` rather than ``os.getcwd()``."""
        if not file_path:
            return ""
        if os.path.isabs(file_path):
            return os.path.abspath(file_path)
        anchor = base_dir or self.base_dir
        if anchor:
            return os.path.abspath(os.path.join(anchor, file_path))
        return os.path.abspath(file_path)

    def is_self_generated_event(self, event: Any) -> bool:
        """Check if filesystem event is generated by internal move operations."""
        with self._lock:
            if not self._active_move_paths:
                return False

            raw_src = getattr(event, "src_path", None) or getattr(
                event, "file_path", None
            )
            src = (
                os.path.normcase(self._resolve_event_path(raw_src)) if raw_src else None
            )
            raw_dst = getattr(event, "dest_path", None)
            dst = (
                os.path.normcase(self._resolve_event_path(raw_dst)) if raw_dst else None
            )

            if src and src in self._active_move_paths:
                return True
            if dst and dst in self._active_move_paths:
                return True

            return False

    def _add_parent_paths(
        self, path: str, paths_set: set[str], base_dir: Optional[str] = None
    ) -> None:
        """Add parent directories of a path up to its watched root to paths_set."""
        if not path:
            return
        curr = os.path.dirname(os.path.normcase(os.path.abspath(path)))
        resolved_root = base_dir or self._resolve_watched_root(path) or self.base_dir
        base = os.path.normcase(os.path.abspath(resolved_root)) if resolved_root else ""
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
        plan: dict[str, Any],
        current_dest: str = "",
        base_dir: Optional[str] = None,
    ) -> set[str]:
        """Extract all source and destination paths (and parent directories) involved in a move plan."""
        paths: set[str] = set()
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
        self,
        plan: Any = None,
        active_paths: Any = None,
        base_dir: Optional[str] = None,
    ):
        """Context manager to scoped-suppress filesystem events during active file move execution."""
        effective_base = base_dir or self.base_dir
        paths_to_add: set[str] = set()
        if active_paths:
            for p in active_paths:
                if p:
                    norm_p = os.path.normcase(os.path.abspath(p))
                    paths_to_add.add(norm_p)
                    self._add_parent_paths(
                        norm_p, paths_to_add, base_dir=effective_base
                    )
        if plan and effective_base:
            try:
                from app.core.verifier import VerificationEngine

                moves = VerificationEngine.get_moves(effective_base, plan)
                for _rel_src, src, dst in moves:
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
        """Check if path should be ignored to prevent infinite feedback loops."""
        if not path:
            return True
        norm_path = os.path.normpath(path).replace("\\", "/")

        quarantine_dir_name = (
            getattr(self.settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine")
            or "_Compliance_Quarantine"
        )
        ignored_patterns = [
            ".autosorter",
            ".sortify_duplicates",
            ".sortify_overwritten",
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

        from app.core.path_utils import get_session_base_dir
        from app.core.scanner import ScanFilter

        session_base = get_session_base_dir()
        if session_base.name in norm_path or str(session_base) in norm_path:
            return True

        matched_root = self._resolve_watched_root(path) or self.base_dir
        filter_target = (
            os.path.normpath(os.path.join(matched_root, path))
            if matched_root and not os.path.isabs(path)
            else norm_path
        )
        scan_filter = ScanFilter.from_settings(
            self.settings,
            base_dir=matched_root,
        )
        if (
            scan_filter.check_path(
                filter_target,
                base_dir=matched_root,
                check_depth=bool(matched_root),
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

        file_path = self._resolve_event_path(change_event.file_path)
        if not file_path or self.should_ignore_path(file_path):
            return False
        change_event.file_path = file_path

        norm_p = os.path.normcase(file_path)
        now = time.time()
        current_size = -1
        with contextlib.suppress(OSError):
            if os.path.isfile(file_path):
                current_size = os.path.getsize(file_path)

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

            if norm_p in self._active_triage_paths:
                if change_event.event_type in ("modified", "closed", "moved"):
                    self._pending_retriage_paths.add(norm_p)
                logger.debug(
                    f"Path is currently being triaged, deduplicating event: {file_path}"
                )
                return False

            last_time = self._recent_events.get(norm_p)
            if last_time is not None and (now - last_time) < self.dedup_window:
                prev_size = self._recent_event_sizes.get(norm_p, -1)
                if current_size < 0 or prev_size < 0 or current_size == prev_size:
                    logger.debug(f"Deduplicating rapid event for path: {file_path}")
                    return False

            self._recent_events[norm_p] = now
            if current_size >= 0:
                self._recent_event_sizes[norm_p] = current_size
            if len(self._recent_events) > 2000:
                cutoff = now - (self.dedup_window * 5)
                self._recent_events = {
                    k: v for k, v in self._recent_events.items() if v > cutoff
                }
                self._recent_event_sizes = {
                    k: v
                    for k, v in self._recent_event_sizes.items()
                    if k in self._recent_events
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

    def _get_folder_sort_lock(self, base_dir: str) -> threading.RLock:
        """Retrieve or create a threading.RLock serializing sort operations for a watched folder."""
        key = os.path.normcase(os.path.abspath(base_dir)) if base_dir else "__default__"
        with self._lock:
            lock = self._folder_sort_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._folder_sort_locks[key] = lock
            return lock

    def _get_or_create_session(self, base_dir: Optional[str] = None) -> AppSession:
        """Thread-safe lazy initialization of an AppSession per watched root folder."""
        target_dir = os.path.abspath(base_dir or self.base_dir)
        key = os.path.normcase(target_dir)
        with self._lock:
            existing_single = getattr(self, "_app_session", None)
            if existing_single is not None and (
                not getattr(existing_single, "base_dir", None)
                or os.path.normcase(os.path.abspath(str(existing_single.base_dir)))
                == key
            ):
                self._sessions_by_root[key] = existing_single
                return existing_single

            session = self._sessions_by_root.get(key)
            if session is None:
                session = AppSession(self.settings, target_dir)
                self._sessions_by_root[key] = session
                if self._app_session is None:
                    self._app_session = session
            return session

    @staticmethod
    def _extract_history_manager(session_or_hm: Any) -> Optional[Any]:
        """Resolve a real HistoryManager from an AppSession or direct HistoryManager reference."""
        if session_or_hm is None:
            return None
        if hasattr(session_or_hm, "start_session") and isinstance(
            getattr(session_or_hm, "db_path", None), (str, os.PathLike)
        ):
            return session_or_hm
        hm = getattr(session_or_hm, "history_manager", None) or getattr(
            session_or_hm, "history", None
        )
        if hm is not None and isinstance(
            getattr(hm, "db_path", None), (str, os.PathLike)
        ):
            return hm
        return None

    def _get_quiet_window_bounds(self) -> tuple[float, float]:
        """Return (debounce_delay, max_debounce_delay) in seconds for undo session batching."""
        if self._undo_quiet_window_override is not None:
            override = max(0.05, float(self._undo_quiet_window_override))
            return override, max(override, 5.0)
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
        abs_root = os.path.abspath(root_dir)
        norm_root = os.path.normcase(abs_root)
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                batch = _FolderUndoBatch(root_dir=abs_root)
                self._undo_batches[norm_root] = batch
            batch.in_flight_count += 1
            batch.last_activity_at = time.time()
            if batch.timer is not None:
                batch.timer.cancel()
                batch.timer = None

    def _peek_folder_batch_session(self, root_dir: str) -> Optional[str]:
        """Return currently open quiet-window session ID for ``root_dir``, if any."""
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
        self,
        root_dir: str,
        session_id: str,
        history_ref: Any,
        move_count: int = 1,
    ) -> None:
        """Attach an already-started Phase 0 session to ``root_dir``'s quiet-window undo batch."""
        hm = self._extract_history_manager(history_ref)
        if not session_id or hm is None:
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
                batch.history_manager = hm
                batch.opened_at = now
            batch.move_count += max(0, move_count)
            batch.last_activity_at = now

    def _acquire_folder_batch_session(
        self, root_dir: str, session_or_hm: Any
    ) -> Optional[str]:
        """Return or lazily start an active shared HistoryManager session_id for ``root_dir``."""
        hm = self._extract_history_manager(session_or_hm)
        if hm is None:
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
                batch.history_manager = hm
            return existing_sid

        try:
            new_sid = str(hm.start_session(abs_root))
        except Exception:
            logger.debug("Could not start folder undo batch session", exc_info=True)
            return None

        with self._lock:
            batch = self._undo_batches.setdefault(
                norm_root, _FolderUndoBatch(root_dir=abs_root)
            )
            if batch.session_id is None:
                batch.session_id = new_sid
                batch.history_manager = hm
                batch.opened_at = time.time()
                batch.last_activity_at = batch.opened_at
                batch.move_count = 0
                return new_sid
            concurrent_sid = batch.session_id

        with contextlib.suppress(Exception):
            hm.complete_session(new_sid)
        return concurrent_sid

    def _record_folder_batch_moves(self, root_dir: str, count: int = 1) -> None:
        if count <= 0:
            return
        norm_root = os.path.normcase(os.path.abspath(root_dir))
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is not None:
                batch.move_count += count
                batch.last_activity_at = time.time()

    def _end_folder_triage_activity(self, root_dir: str) -> None:
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
            if batch.session_id is None or batch.in_flight_count > 0:
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
                        delay, self._on_folder_quiet_timer, args=(abs_root,)
                    )
                    timer.daemon = True
                    batch.timer = timer
                    timer.start()

        if immediate_flush:
            self._flush_folder_batch_session(abs_root, force=True)

    def _on_folder_quiet_timer(self, root_dir: str) -> None:
        norm_root = os.path.normcase(os.path.abspath(root_dir))
        with self._lock:
            batch = self._undo_batches.get(norm_root)
            if batch is None:
                return
            batch.timer = None
            if batch.in_flight_count > 0:
                return
        self._flush_folder_batch_session(root_dir)

    def _flush_folder_batch_session(
        self, root_dir: str, *, force: bool = False
    ) -> Optional[str]:
        """Finalize an open quiet-window undo session for ``root_dir`` once activity settles."""
        folder_lock = self._get_folder_sort_lock(root_dir)
        with folder_lock:
            norm_root = os.path.normcase(os.path.abspath(root_dir))
            with self._lock:
                batch = self._undo_batches.get(norm_root)
                if batch is None:
                    return None
                if batch.in_flight_count > 0 and not force:
                    return None
                if batch.timer is not None:
                    batch.timer.cancel()
                    batch.timer = None
                sid = batch.session_id
                hm = batch.history_manager
                move_count = batch.move_count
                batch.session_id = None
                batch.history_manager = None
                batch.opened_at = 0.0
                batch.move_count = 0

            if not sid or hm is None:
                return None

            try:
                ledger = getattr(hm, "moves", None)
                total_moves = move_count
                if ledger is not None and hasattr(ledger, "list_moves"):
                    from app.core.moves import DONE, VERIFIED

                    recorded = ledger.list_moves(sid)
                    total_moves = sum(
                        1
                        for m in recorded
                        if getattr(m, "state", None) in (DONE, VERIFIED)
                    )
                if total_moves > 0:
                    hm.complete_session(sid)
                    return sid

                from app.core.db_conn import get_db_connection

                db_path = getattr(hm, "db_path", None)
                if db_path:

                    def _delete_empty() -> None:
                        conn = get_db_connection(str(db_path))
                        with conn:
                            conn.execute(
                                "DELETE FROM sessions WHERE session_id = ? AND status = 'active'",
                                (sid,),
                            )

                    worker = getattr(getattr(hm, "db", None), "worker", None)
                    if worker is not None and hasattr(worker, "execute_write"):
                        worker.execute_write(_delete_empty)
                    elif hasattr(hm, "_run_write"):
                        hm._run_write(_delete_empty)
                    else:
                        _delete_empty()
            except Exception:
                logger.warning(
                    f"Failed to finalize quiet-window undo batch session {sid}",
                    exc_info=True,
                )
            return None

    def flush_all_undo_batches(self) -> list[str]:
        """Flush all open folder undo batch sessions immediately."""
        with self._lock:
            roots = [b.root_dir for b in self._undo_batches.values()]
        completed: list[str] = []
        for root in roots:
            sid = self._flush_folder_batch_session(root, force=True)
            if sid:
                completed.append(sid)
        return completed

    def _start_pipeline_event_loop(self) -> None:
        """Initialize and launch the background asyncio event loop thread for streaming event processing."""
        from app.core.ipc import DaemonIPCServer
        from app.core.shared_registry import ContextPropagatingThread

        ready_event = threading.Event()
        startup_error: list[BaseException] = []

        def _loop_thread_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._event_loop = loop
            self._event_queue = asyncio.PriorityQueue(maxsize=self.max_queue_capacity)
            self._start_time = time.time()

            # Initialize and launch the shared user-scoped IPC server on the daemon event loop
            self._ipc_server = DaemonIPCServer(self)
            try:
                loop.run_until_complete(self._ipc_server.start())
            except Exception as e:
                logger.error(f"Failed to start daemon IPC server: {e}", exc_info=True)
                startup_error.append(e)
                ready_event.set()
                loop.close()
                return

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

                loop.close()

        self._execution_thread = ContextPropagatingThread(
            target=_loop_thread_main, daemon=True
        )
        self._execution_thread.start()
        ready_event.wait(timeout=5.0)
        if startup_error:
            with self._lock:
                if self._state != DaemonState.STOPPED:
                    self._transition_to(DaemonState.STOPPED)
            raise startup_error[0]

    async def _triage_worker(
        self, worker_id: int, cancel_token: Optional[threading.Event] = None
    ) -> None:
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
    ) -> None:
        """Triage a single file event without whole-directory scans."""
        file_path = event.file_path
        if not file_path:
            return

        abs_path = self._resolve_event_path(file_path)
        if not abs_path or self.should_ignore_path(abs_path):
            return

        root_dir = self._resolve_watched_root(abs_path)
        if self._is_folder_paused(root_dir):
            return

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
                if event.event_type == "created":
                    is_empty_new = False
                    with contextlib.suppress(OSError):
                        is_empty_new = (
                            os.path.isfile(abs_path) and os.path.getsize(abs_path) == 0
                        )
                    if is_empty_new:
                        await asyncio.sleep(0.03)
                await self._triage_file_path(abs_path, cancel_token=cancel_token)
            finally:
                should_retriage = False
                with self._lock:
                    self._active_triage_paths.discard(norm_p)
                    if norm_p in self._pending_retriage_paths:
                        self._pending_retriage_paths.discard(norm_p)
                        should_retriage = True
                    if should_retriage:
                        self._recent_events.pop(norm_p, None)
                        self._recent_event_sizes.pop(norm_p, None)
                if should_retriage and self.is_running and os.path.isfile(abs_path):
                    self.enqueue_event(
                        FileChangeEvent(
                            event_type="modified",
                            file_path=abs_path,
                            timestamp=time.time(),
                        )
                    )

    async def _in_sort_thread(
        self,
        fn: Any,
        /,
        *args: Any,
        folder_root: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Run blocking sort work on the daemon's sort pool, serialized per watched folder."""
        if self._sort_executor is None:
            max_pool = max(2, min(8, int(self.num_workers or 2)))
            self._sort_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=max_pool, thread_name_prefix="daemon-sort"
            )
        target_root = folder_root or self.base_dir
        folder_lock = self._get_folder_sort_lock(target_root) if target_root else None

        def _locked_call() -> Any:
            if folder_lock is not None:
                with folder_lock:
                    return fn(*args, **kwargs)
            return fn(*args, **kwargs)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._sort_executor, _locked_call)

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
        """Build a SortPipeline bound to daemon settings, target base_dir, and telemetry."""
        target_base = (
            base_dir or getattr(app_session, "base_dir", None) or self.base_dir
        )
        pipeline = SortPipeline(
            app_session, base_dir=target_base, settings=self.settings
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

    def _plan_and_execute_phase(
        self,
        pipeline: SortPipeline,
        items: list[str],
        *,
        base_dir: Optional[str] = None,
        session_id: Optional[str] = None,
        manage_session_lifecycle: bool = True,
        **plan_kwargs: Any,
    ) -> tuple[Any, Any]:
        """Plan one phase via SortPipeline and execute moves inside scoped_move_phase."""
        target_base = base_dir or getattr(pipeline, "base_dir", None) or self.base_dir
        plan = pipeline.plan(items, **plan_kwargs)
        if not plan:
            return plan, None
        cancel_check = plan_kwargs.get("cancel_check")
        if cancel_check and cancel_check():
            return plan, None

        effective_sid = session_id
        if not manage_session_lifecycle and effective_sid is None and target_base:
            effective_sid = self._acquire_folder_batch_session(
                target_base, pipeline.session
            )

        exec_kwargs: dict[str, Any] = {}
        if effective_sid is not None:
            exec_kwargs["session_id"] = effective_sid
        if not manage_session_lifecycle:
            exec_kwargs["manage_session_lifecycle"] = False

        outcome = pipeline.execute(
            plan,
            move_context=functools.partial(
                self.scoped_move_phase, base_dir=target_base
            ),
            **exec_kwargs,
        )
        if (
            not manage_session_lifecycle
            and effective_sid
            and target_base
            and outcome is not None
        ):
            real_moves = int(
                (getattr(outcome, "summary", None) or {}).get("real_move_count", 0) or 0
            )
            if real_moves > 0:
                self._record_folder_batch_moves(target_base, real_moves)
        return plan, outcome

    def _run_triage_phase(
        self,
        app_session: Any,
        items: list[str],
        *,
        base_dir: Optional[str] = None,
        session_id: Optional[str] = None,
        manage_session_lifecycle: bool = True,
        **plan_kwargs: Any,
    ) -> tuple[Any, Any]:
        """Construct an isolated SortPipeline on the sort thread and execute one triage phase."""
        target_base = (
            base_dir or getattr(app_session, "base_dir", None) or self.base_dir
        )
        pipeline = self._build_pipeline(app_session, base_dir=target_base)
        pipeline._defer_unarchive_children = True
        return self._plan_and_execute_phase(
            pipeline,
            items,
            base_dir=target_base,
            session_id=session_id,
            manage_session_lifecycle=manage_session_lifecycle,
            **plan_kwargs,
        )

    def _enqueue_unarchived_children(
        self,
        outcome: Any,
        rel_path: str,
        *,
        base_dir: Optional[str] = None,
    ) -> int:
        """Enqueue extracted archive children from a SortPipeline outcome onto the triage queue."""
        if outcome is None:
            return 0
        unpacked_files = list(
            (getattr(outcome, "summary", None) or {}).get("unarchived_children") or []
        )
        registered_count = 0
        for child_path in unpacked_files:
            resolved_child = self._resolve_event_path(child_path, base_dir=base_dir)
            if os.path.isfile(resolved_child) and not self.should_ignore_path(
                resolved_child
            ):
                child_event = FileChangeEvent(
                    event_type="created",
                    file_path=resolved_child,
                    timestamp=time.time(),
                )
                if self.enqueue_event(child_event):
                    registered_count += 1
        if registered_count > 0:
            logger.info(
                "SortPipeline unarchived %s: enqueued %d child files into triage loop",
                rel_path,
                registered_count,
            )
        return registered_count

    async def _triage_file_path(
        self, abs_path: str, cancel_token: Optional[threading.Event] = None
    ) -> None:
        """Targeted triage on a single item path using QuarantineInterceptorService staging and compliance policy evaluation."""
        if not os.path.exists(abs_path):
            logger.debug(f"Target file missing before triage: {abs_path}")
            return

        if self.should_ignore_path(abs_path):
            return

        root_dir = self._resolve_watched_root(abs_path)
        if not root_dir:
            logger.debug("Skipping event outside active watched folders: %s", abs_path)
            return

        if self._is_folder_paused(root_dir):
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

        def cancel_check() -> bool:
            return self._is_cancelled(cancel_token)

        self._reload_settings()

        if cancel_check():
            return

        batch_hm = self._extract_history_manager(app_session)

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
                    folder_root=root_dir,
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

            # Execute Phase 0 safety & strict quarantine holds off-thread; non-quarantine
            # policies (redact, archive, retain, unarchive, routing) flow into SortPipeline.
            existing_batch_sid = (
                self._peek_folder_batch_session(root_dir)
                if batch_hm is not None
                else None
            )
            phase0_fn = getattr(interceptor, "_process_phase0_job", None)

            def _run_phase0() -> Any:
                with self.scoped_move_phase(
                    active_paths=[abs_path, staged_filepath],
                    base_dir=root_dir,
                ):
                    if callable(phase0_fn):
                        return phase0_fn(
                            job_id,
                            session_id=existing_batch_sid,
                            complete_session=(batch_hm is None),
                            history_manager=batch_hm,
                        )
                    return interceptor.process_quarantine_job(job_id)

            raw_record = await asyncio.to_thread(_run_phase0)

            if cancel_check():
                return

            quarantine_record = validate_quarantine_record(raw_record)
            status = quarantine_record.status

            # If file was quarantined or held for manual review / DLQ, triage is complete
            if status in (
                "QUARANTINED",
                "MANUAL_REVIEW_REQUIRED",
                "DEAD_LETTER_QUEUE",
            ):
                rec_sid = getattr(
                    quarantine_record, "move_session_id", None
                ) or quarantine_record.get("move_session_id")
                if rec_sid and batch_hm is not None:
                    self._adopt_folder_batch_session(
                        root_dir,
                        str(rec_sid),
                        batch_hm,
                        move_count=1,
                    )
                should_cleanup_origin = os.path.exists(abs_path) and (
                    (status == "QUARANTINED" and not rec_sid)
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

            # Cleanup staging copy if present after Phase 0 release
            if (
                staged_filepath
                and os.path.exists(staged_filepath)
                and os.path.abspath(abs_path) != os.path.abspath(staged_filepath)
            ):
                resilient_remove(staged_filepath)

            # Phase 1: Fast-Path Rule Evaluation via SortPipeline
            if not os.path.exists(abs_path):
                return

            _fast_plan, fast_outcome = await self._in_sort_thread(
                self._run_triage_phase,
                app_session,
                [rel_path],
                base_dir=root_dir,
                manage_session_lifecycle=False,
                fast_path_only=True,
                target_paths=[rel_path],
                run_metadata_pass=True,
                cancel_check=cancel_check,
                folder_root=root_dir,
            )
            if fast_outcome is not None:
                self._enqueue_unarchived_children(
                    fast_outcome, rel_path, base_dir=root_dir
                )
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
            has_content_policies = any(
                isinstance(p, dict)
                and p.get("enabled", True)
                and (
                    str(p.get("type", "")).lower()
                    in ("keyword", "action", "compliance", "secret", "credential", "")
                    or str(p.get("action", "")).lower()
                    in ("redact", "quarantine", "archive", "retain")
                    or str(p.get("expression", "")).strip().lower()
                    in ("secret", "credential")
                )
                for p in (policies or [])
            )
            deferred_jev_results: dict[str, Any] | None = None
            try:
                from app.core.shared_registry import SharedModelRegistry

                jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
                jev_res = await self._in_sort_thread(
                    jev_engine.classify, abs_path, folder_root=root_dir
                )
                if (
                    jev_res
                    and getattr(jev_res, "is_classified", False)
                    and getattr(jev_res, "confidence", 0.0) >= 0.5
                ):
                    if has_content_policies:
                        deferred_jev_results = {rel_path: jev_res, abs_path: jev_res}
                    else:
                        _jev_plan, jev_outcome = await self._in_sort_thread(
                            self._run_triage_phase,
                            app_session,
                            [rel_path],
                            base_dir=root_dir,
                            manage_session_lifecycle=False,
                            fast_path_only=True,
                            jev_results={rel_path: jev_res, abs_path: jev_res},
                            cancel_check=cancel_check,
                            folder_root=root_dir,
                        )
                        if jev_outcome is not None:
                            self._enqueue_unarchived_children(
                                jev_outcome, rel_path, base_dir=root_dir
                            )
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
                self._run_triage_phase,
                app_session,
                [rel_path],
                base_dir=root_dir,
                manage_session_lifecycle=False,
                fast_path_only=False,
                target_paths=[rel_path],
                jev_results=deferred_jev_results,
                cancel_check=cancel_check,
                folder_root=root_dir,
            )
            if slow_outcome is not None:
                self._enqueue_unarchived_children(
                    slow_outcome, rel_path, base_dir=root_dir
                )
                logger.info(
                    f"Targeted Slow-Path AI triage completed for {rel_path}: {slow_outcome.summary}"
                )
        finally:
            self._end_folder_triage_activity(root_dir)

    async def _reconciliation_worker(
        self, cancel_token: Optional[threading.Event] = None
    ) -> None:
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

            # Retry attaching watches for watched folders that were temporarily offline/missing
            for folder in self.get_watched_folders():
                if os.path.isdir(folder):
                    self._schedule_folder_watch(folder)

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
        *,
        roots: Optional[list[str]] = None,
    ) -> None:
        """Low-priority directory audit across watched folders to enqueue untracked files."""
        target_roots = (
            list(roots)
            if roots is not None
            else (
                self.get_watched_folders() or ([self.base_dir] if self.base_dir else [])
            )
        )
        if not target_roots:
            return

        token = cancel_token or self._cancel_event

        def _scan_all() -> list[str]:
            from app.core.scanner import ScanFilter, active_scan_filter

            collected: list[str] = []
            for root_dir in target_roots:
                if (
                    not root_dir
                    or not os.path.isdir(root_dir)
                    or self._is_folder_paused(root_dir)
                ):
                    continue
                sf = ScanFilter.from_settings(self.settings, base_dir=root_dir)
                with active_scan_filter(sf):
                    files = get_files_recursively(root_dir)
                for f in files:
                    resolved = self._resolve_event_path(f, base_dir=root_dir)
                    if resolved and not self.should_ignore_path(resolved):
                        collected.append(resolved)
            return collected

        scanned_files = await asyncio.to_thread(_scan_all)
        now = time.time()
        enqueued_count = 0

        for abs_file in scanned_files:
            if self._is_cancelled(token):
                break
            norm_p = os.path.normcase(abs_file)
            with self._lock:
                if norm_p in self._active_triage_paths:
                    continue
                last_t = self._recent_events.get(norm_p)
                if last_t is not None and (now - last_t) < self.dedup_window * 2:
                    continue

            change_event = FileChangeEvent(
                event_type="reconciliation",
                file_path=abs_file,
                timestamp=now,
            )
            if self._is_cancelled(token):
                break
            if self.enqueue_event(change_event):
                enqueued_count += 1

        if enqueued_count > 0:
            logger.info(
                f"Reconciliation audit enqueued {enqueued_count} untracked files across {len(target_roots)} folder(s)."
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

    def start(self) -> None:
        """Start the continuous watchdog daemon, workers, event loop, and single multi-folder observer."""
        with self._lock:
            if self._state == DaemonState.RUNNING:
                return
            if not self._watched_folders and self.base_dir:
                self._watched_folders = [_normalize_watch_root(self.base_dir)]
            self._transition_to(DaemonState.RUNNING)
            self._queue_sequence_counter = 0
            self._move_ref_count = 0
            self._pending_dirty = False
            self._active_move_paths.clear()
            self._cancel_event.clear()
            self._scoped_run_token = None
            self._recent_events.clear()
            self._recent_event_sizes.clear()
            self._active_path_locks.clear()
            self._active_triage_paths.clear()
            self._pending_retriage_paths.clear()
            self._watch_handles.clear()
            self._paused_folders.clear()
            self._sorting_process = None
            self._proc_cancel_event = None
            self._rerun_pending = False

        roots = self.get_watched_folders()
        logger.info(f"Starting continuous watchdog daemon for: {', '.join(roots)}")

        # Subscribe to live WATCHED_FOLDERS updates on AppSettings
        if (
            hasattr(self.settings, "add_observer")
            and self._settings_observer_cb is None
        ):
            self._settings_observer_cb = self._on_watched_folders_setting_changed
            with contextlib.suppress(Exception):
                self.settings.add_observer(
                    "WATCHED_FOLDERS", self._settings_observer_cb
                )

        # Launch background asyncio event loop & shared IPC server
        self._start_pipeline_event_loop()

        # Start a single Watchdog Observer scheduling all watched folders
        self.observer = Observer()
        for folder in roots:
            self._schedule_folder_watch(folder)
        self.observer.start()

        # Trigger initial low-priority reconciliation audit across watched roots
        if self._event_loop and self._event_loop.is_running():
            self._event_loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._run_reconciliation_audit())
            )

    def stop(self) -> None:
        """Stop the continuous watchdog daemon, workers, observer, and event loop."""
        with self._lock:
            if self._state == DaemonState.STOPPED:
                return
            self._transition_to(DaemonState.STOPPED)
            self._move_ref_count = 0
            self._pending_dirty = False
            self._rerun_pending = False
            self._active_move_paths.clear()
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

        loop = self._event_loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)

        if self._execution_thread and self._execution_thread.is_alive():
            self._execution_thread.join(timeout=3.0)

        # Let any in-flight move finish before flushing undo batches and closing sessions.
        executor, self._sort_executor = self._sort_executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

        self.flush_all_undo_batches()

        self._event_loop = None
        self._event_queue = None

        with self._lock:
            sessions_to_close = list(self._sessions_by_root.values())
            single_session = getattr(self, "_app_session", None)
            if single_session is not None and single_session not in sessions_to_close:
                sessions_to_close.append(single_session)
            self._sessions_by_root.clear()
            self._app_session = None

        for session in sessions_to_close:
            try:
                session.close()
            except Exception:
                logger.debug("Failed to close daemon app session", exc_info=True)

        logger.info("Watchdog daemon stopped.")

    def trigger_recalculation(self) -> None:
        """Thread-safe and debounced trigger for sorting run."""
        with self._lock:
            if not self.is_running or self._move_ref_count > 0:
                return

            now = time.time()
            first_event_time = getattr(self, "_first_event_time", None)
            if first_event_time is None:
                first_event_time = now
                self._first_event_time = first_event_time

            elapsed = now - first_event_time
            debounce_delay = getattr(self.settings, "DEBOUNCE_DELAY", 0.6)
            max_debounce_delay = getattr(self.settings, "MAX_DEBOUNCE_DELAY", 5.0)

            if elapsed >= max_debounce_delay:
                return

            if self._state != DaemonState.RECALCULATING:
                self._transition_to(DaemonState.RECALCULATING)

            if self._scoped_run_token is not None:
                self._scoped_run_token.set()
            if self._proc_cancel_event is not None:
                try:
                    self._proc_cancel_event.set()
                except Exception:
                    logger.debug("Failed to signal worker cancel event", exc_info=True)

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

    def _schedule_run(self, cancel_event: Any) -> None:
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

            if self._sorting_process is not None and self._sorting_process.is_alive():
                self._rerun_pending = True
                logger.info(
                    "Background sorting worker process active; queueing subsequent run."
                )
                return

            self._spawn_sorting_process(cancel_event)

    def _spawn_sorting_process(self, cancel_event: Any = None) -> None:
        """Spawn a process-isolated worker subprocess to execute the sorting routine."""
        _ = cancel_event
        with self._lock:
            if not self.is_running:
                return
            self._rerun_pending = False

            config_filepath = getattr(self.settings, "filepath", None)
            roots = (
                list(self._watched_folders)
                if self._watched_folders
                else ([self.base_dir] if self.base_dir else [])
            )

            ctx = multiprocessing.get_context("spawn")
            mp_cancel_event = ctx.Event()
            self._proc_cancel_event = mp_cancel_event

            proc = ctx.Process(
                target=_run_sorting_process_entry,
                args=(roots, config_filepath, mp_cancel_event),
                daemon=False,
            )
            self._sorting_process = proc
            proc.start()

            monitor_thread = threading.Thread(
                target=self._monitor_sorting_process, args=(proc,), daemon=True
            )
            monitor_thread.start()

    def _monitor_sorting_process(self, proc: Any) -> None:
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

    def _run_sorting_sync(self, cancel_event: Any) -> None:
        """Run the core synchronous sorting runner across all watched folders."""
        if cancel_event.is_set():
            with self._lock:
                if (
                    self._state == DaemonState.RECALCULATING
                    and self._scoped_run_token is cancel_event
                ):
                    self._transition_to(DaemonState.RUNNING)
            return

        logger.info("Executing background sorting run...")

        def cancel_check() -> bool:
            return cancel_event.is_set() or self._is_cancelled()

        self._reload_settings(log_error=True)

        roots = self.get_watched_folders() or ([self.base_dir] if self.base_dir else [])
        try:
            from app.core.scanner import ScanFilter, active_scan_filter

            for root_dir in roots:
                if cancel_check():
                    return
                if not root_dir or not os.path.isdir(root_dir):
                    continue
                if self._is_folder_paused(root_dir):
                    continue

                app_session: Optional[AppSession] = None
                try:
                    app_session = AppSession(self.settings, root_dir)
                    pipeline = self._build_pipeline(app_session, base_dir=root_dir)
                    daemon_scan_filter = ScanFilter.from_settings(
                        self.settings, base_dir=root_dir
                    )

                    # PHASE 1: Fast-Path (Deterministic Rules)
                    if cancel_check():
                        return
                    with active_scan_filter(daemon_scan_filter):
                        files = get_files_recursively(root_dir)
                    files = [f for f in files if not self.should_ignore_path(f)]

                    if not files:
                        logger.info(f"No files found to organize in {root_dir}.")
                        continue

                    fast_path_plan, fast_outcome = self._plan_and_execute_phase(
                        self._build_pipeline(app_session, base_dir=root_dir),
                        files,
                        base_dir=root_dir,
                        fast_path_only=True,
                        run_metadata_pass=True,
                        cancel_check=cancel_check,
                    )
                    fast_path_moved_destinations: set[str] = set()

                    if fast_outcome is not None:
                        logger.info(
                            f"Phase 1 (Fast-Path) completed successfully: {fast_outcome.summary}"
                        )
                        fast_path_moved_destinations = _extract_plan_destinations(
                            fast_path_plan, base_dir=root_dir
                        )

                    if cancel_check():
                        return

                    app_session.db.invalidate_cache()
                    with active_scan_filter(daemon_scan_filter):
                        files = get_files_recursively(root_dir)
                    files = [f for f in files if not self.should_ignore_path(f)]

                    if not files:
                        logger.info(f"No remaining files to organize in {root_dir}.")
                        continue

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
                        logger.info(f"No AI sorting actions needed in {root_dir}.")
                        continue

                    slow_outcome = pipeline.execute(
                        slow_path_plan,
                        move_context=functools.partial(
                            self.scoped_move_phase, base_dir=root_dir
                        ),
                    )
                    logger.info(
                        f"Phase 2 (Slow-Path AI) completed successfully: {slow_outcome.summary}"
                    )
                finally:
                    if app_session:
                        app_session.close()

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


def start_daemon(
    settings: AppSettings,
    base_dir: str | Sequence[str] | None = None,
    watched_folders: Optional[Sequence[str]] = None,
    *,
    extra_dirs: Optional[Sequence[str]] = None,
) -> None:
    """Start the persistent multi-folder directory-watching daemon service."""
    folders: list[str] = []
    if watched_folders is not None:
        folders.extend(watched_folders)
    if isinstance(base_dir, str):
        if base_dir.strip():
            folders.append(base_dir)
    elif base_dir is not None:
        folders.extend(base_dir)
    if extra_dirs:
        folders.extend(extra_dirs)

    if folders:
        daemon = ContinuousWatchdogDaemon(settings, watched_folders=folders)
    else:
        cfg_watched = list(getattr(settings, "WATCHED_FOLDERS", []) or [])
        if cfg_watched:
            daemon = ContinuousWatchdogDaemon(settings, watched_folders=cfg_watched)
        else:
            daemon = ContinuousWatchdogDaemon(settings, base_dir=os.getcwd())

    daemon.start()

    try:
        while daemon.is_running:
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        daemon.stop()
