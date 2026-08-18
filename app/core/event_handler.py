"""Core event handler and synchronized debouncing module for file system monitoring."""

import logging
import os
import time

from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("app.events")


def should_ignore_path(path: str, settings=None) -> bool:
    """Check if a file system path should be ignored by monitoring.

    Synchronously checks internal databases, application metadata,
    temporary session folders, and dynamic IGNORED_EXTENSIONS from configuration.
    """
    if not path:
        return True

    norm_path = os.path.normpath(path).replace("\\", "/")

    # Ignore database files, application metadata, VCS, and internal cache directories
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
    ]

    for pattern in ignored_patterns:
        if pattern in norm_path:
            return True

    # Also ignore temporary folder / session folders
    if "autosorter_sessions" in norm_path:
        return True

    # Suffix matching on lowercase file extensions using IGNORED_EXTENSIONS configuration
    default_exts = [".crdownload", ".tmp", ".download"]
    ignored_exts = default_exts
    if settings is not None:
        ignored_exts = getattr(settings, "IGNORED_EXTENSIONS", default_exts)

    lower_path = norm_path.lower()
    for ext in ignored_exts:
        if ext and lower_path.endswith(ext.lower()):
            return True

    return False


class DebounceTracker:
    """Synchronized debouncing and starvation tracker for filesystem events."""

    def __init__(self, settings):
        self.settings = settings
        self.first_event_time = None

    def record_event(self, now=None) -> tuple[float, bool]:
        """Record a file system event and calculate debounce timing.

        Returns
        -------
            tuple[float, bool]: (calculated_delay, is_starved)
                - calculated_delay: Delay in seconds to sleep/schedule before execution.
                - is_starved: True if elapsed >= MAX_DEBOUNCE_DELAY. If True, existing
                  scheduled tasks/timers should be allowed to run rather than rescheduled.
        """
        if now is None:
            now = time.time()

        if self.first_event_time is None:
            self.first_event_time = now

        elapsed = now - self.first_event_time
        debounce_delay = getattr(self.settings, "DEBOUNCE_DELAY", 0.6)
        max_debounce_delay = getattr(self.settings, "MAX_DEBOUNCE_DELAY", 5.0)

        if elapsed >= max_debounce_delay:
            return 0.0, True

        max_delay = max(0.0, max_debounce_delay - elapsed)
        delay = min(debounce_delay, max_delay)
        return delay, False

    def reset(self):
        """Reset the tracked first event timestamp."""
        self.first_event_time = None


class CoreEventHandler(FileSystemEventHandler):
    """Unified core watchdog event handler for filesystem monitoring.

    Filters paths using `should_ignore_path` and safely dispatches valid
    events to mode-specific runtime loops.
    """

    def __init__(self, callback, settings, loop=None):
        """Initialize the core event handler.

        Args:
            callback: Function/callable to invoke when a valid event occurs.
            settings: AppSettings configuration instance.
            loop: Optional asyncio event loop for thread-safe UI dispatching.
        """
        super().__init__()
        self.callback = callback
        self.settings = settings
        self.loop = loop

    def should_ignore_path(self, path: str) -> bool:
        """Check if path should be ignored."""
        return should_ignore_path(path, self.settings)

    def on_any_event(self, event):
        """Handle any file system event, filtering ignored paths before triggering callback."""
        if self.should_ignore_path(event.src_path):
            return
        if hasattr(event, "dest_path") and self.should_ignore_path(event.dest_path):
            return

        if self.loop is not None and getattr(self.loop, "is_running", lambda: True)():
            try:
                self.loop.call_soon_threadsafe(self.callback)
            except RuntimeError:
                # Event loop closed or shutting down
                pass
        else:
            self.callback()
