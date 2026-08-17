"""Continuous background watchdog service daemon."""

import asyncio
import logging
import os
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import AppSettings
from app.core.metadata import MetadataPass
from app.core.scanner import get_files_recursively
from app.core.session import AppSession

logger = logging.getLogger("app.daemon")


class DaemonFolderHandler(FileSystemEventHandler):
    """Handler for file system events inside monitored directory."""

    def __init__(self, daemon):
        self.daemon = daemon

    def on_any_event(self, event):
        """Handle any file system event, trigger recalculation if valid."""
        # We must ignore application metadata, local databases, and temporary cache folders to prevent infinite trigger loops
        if self.daemon.should_ignore_path(event.src_path):
            return
        if hasattr(event, "dest_path") and self.daemon.should_ignore_path(
            event.dest_path
        ):
            return

        # Trigger sorting recalculation (thread-safe and debounced)
        self.daemon.trigger_recalculation()


class ContinuousWatchdogDaemon:
    """Daemon that continuously monitors a folder and triggers silent sorting."""

    def __init__(self, settings: AppSettings, base_dir: str):
        self.settings = settings
        self.base_dir = os.path.abspath(base_dir)
        self.observer = None

        self._lock = threading.Lock()
        self._debounce_timer = None
        self._cancel_event = threading.Event()
        self._is_running = False
        self._first_event_time = None

        # We run the actual sorting loop on a dedicated background execution thread
        self._execution_thread = None

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
        ]

        for pattern in ignored_patterns:
            if pattern in norm_path:
                return True

        # Also ignore any temporary folder/session folders
        if "autosorter_sessions" in norm_path:
            return True

        # Suffix matching on lowercase file extensions using IGNORED_EXTENSIONS configuration
        ignored_exts = getattr(
            self.settings, "IGNORED_EXTENSIONS", [".crdownload", ".tmp", ".download"]
        )
        lower_path = norm_path.lower()
        for ext in ignored_exts:
            if lower_path.endswith(ext.lower()):
                return True

        return False

    def start(self):
        """Start the continuous watchdog daemon and files system observer."""
        with self._lock:
            if self._is_running:
                return
            self._is_running = True
            self._cancel_event.clear()

        logger.info(f"Starting continuous watchdog daemon for: {self.base_dir}")
        print(f"Starting continuous watchdog daemon for: {self.base_dir}")

        # Start watchdog observer
        self.observer = Observer()
        handler = DaemonFolderHandler(self)
        self.observer.schedule(handler, self.base_dir, recursive=True)
        self.observer.start()

        # Trigger an initial sorting run on start
        self.trigger_recalculation()

    def stop(self):
        """Stop the continuous watchdog daemon and join the observer thread."""
        with self._lock:
            if not self._is_running:
                return
            self._is_running = False
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

        logger.info("Watchdog daemon stopped.")
        print("Watchdog daemon stopped.")

    def trigger_recalculation(self):
        """Thread-safe and debounced trigger for sorting run."""
        with self._lock:
            if not self._is_running:
                return

            # Interrupt current run immediately by setting the cancel event
            self._cancel_event.set()

            # Reset cancel event for the upcoming run
            self._cancel_event = threading.Event()

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
            self._first_event_time = None
            if not self._is_running or cancel_event.is_set():
                return

            # Start a background execution thread for sorting
            # (Ensures we don't block the timer thread or watchdog event handling)
            thread = threading.Thread(
                target=self._run_sorting_sync, args=(cancel_event,)
            )
            thread.daemon = True
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

            # 1. Scan directory recursively
            if cancel_check():
                return
            files = get_files_recursively(self.base_dir)

            # Filter out ignored/metadata paths from the scanned files list
            files = [f for f in files if not self.should_ignore_path(f)]

            if not files:
                logger.info("No files found to organize.")
                return

            # 2. Metadata pass to skip already processed, unchanged files
            if cancel_check():
                return
            bypassed_files = MetadataPass.run(
                self.base_dir, files, self.settings, app_session.db, None, cancel_check
            )

            bypassed_set = set(bypassed_files)
            items_to_sort = [f for f in files if f not in bypassed_set]

            if cancel_check():
                return

            # 3. Process new/changed files and partial fit analyzer
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

                loop.run_until_complete(process_and_fit())

            if cancel_check():
                return

            # 4. Generate sorting plan
            plan = app_session.generate_sorting_plan()
            if not plan:
                logger.info("No sorting actions needed.")
                return

            if cancel_check():
                return

            # 5. Execute silent moves
            summary = app_session.execute_moves(plan)
            logger.info(f"Silent move execution completed successfully: {summary}")
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
