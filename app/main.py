"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI or CLI demo.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from app.config import AppSettings
from app.core.domain_contracts import _make_json_serializable

# Dynamic Windows DLL Path Injection
from app.core.path_utils import is_packaged
from app.log_filter import LogScrubbingFilter


class NullWriter:
    """A helper class that discards any written output to mimic a stream."""

    encoding = "utf-8"
    errors = "replace"

    def write(self, text):
        """Discard written text."""
        return len(text) if text else 0

    def writelines(self, lines):
        """Discard written lines."""
        pass

    def flush(self):
        """No-op flush to satisfy the stream interface."""
        pass

    def isatty(self):
        """Return False for null stream."""
        return False

    def fileno(self):
        """Raise OSError for missing file descriptor."""
        raise OSError("NullWriter has no file descriptor")

    def readable(self):
        """Return False for write-only null stream."""
        return False

    def writable(self):
        """Return True for null writer stream."""
        return True

    def seekable(self):
        """Return False for null stream."""
        return False

    @property
    def closed(self):
        """Return False for active null stream."""
        return False


class NullReader:
    """A helper class that discards input stream calls."""

    encoding = "utf-8"
    errors = "replace"

    def read(self, *args, **kwargs):
        """Return empty string."""
        return ""

    def readline(self, *args, **kwargs):
        """Return empty string."""
        return ""

    def readlines(self, *args, **kwargs):
        """Return empty list."""
        return []

    def isatty(self):
        """Return False for null stream."""
        return False

    def fileno(self):
        """Raise OSError for missing file descriptor."""
        raise OSError("NullReader has no file descriptor")

    def readable(self):
        """Return True for null reader stream."""
        return True

    def writable(self):
        """Return False for null reader stream."""
        return False

    def seekable(self):
        """Return False for null stream."""
        return False

    @property
    def closed(self):
        """Return False for active null stream."""
        return False


if sys.platform == "win32" and is_packaged():
    # Attempt to allocate a console for windowed executables (console=False)
    if sys.stdout is None or sys.stderr is None or sys.stdin is None:
        try:
            import ctypes

            if ctypes.windll.kernel32.AllocConsole():
                try:
                    sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                except Exception:
                    pass
                try:
                    sys.stderr = open("CONERR$", "w", encoding="utf-8")
                except Exception:
                    pass
                try:
                    sys.stdin = open("CONIN$", "r", encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            pass

    if sys.stdout is None:
        sys.stdout = NullWriter()
    if sys.stderr is None:
        sys.stderr = NullWriter()
    if sys.stdin is None:
        sys.stdin = NullReader()

    base_dir = getattr(sys, "_MEIPASS", None)
    if base_dir:
        base_dir = os.path.abspath(base_dir)
        try:
            os.add_dll_directory(base_dir)
        except Exception:
            pass

        # In PyInstaller 6+, modules and libraries are under the _internal folder
        internal_dir = os.path.abspath(os.path.join(base_dir, "_internal"))
        if os.path.isdir(internal_dir):
            try:
                os.add_dll_directory(internal_dir)
            except Exception:
                pass

        sqlcipher_dirs = [
            os.path.abspath(os.path.join(base_dir, "sqlcipher3")),
            os.path.abspath(os.path.join(base_dir, "_internal", "sqlcipher3")),
            os.path.abspath(
                os.path.join(base_dir, "app", "binaries", "windows", "sqlcipher3")
            ),
            os.path.abspath(
                os.path.join(
                    base_dir, "_internal", "app", "binaries", "windows", "sqlcipher3"
                )
            ),
        ]
        for sqlcipher_dir in sqlcipher_dirs:
            if os.path.isdir(sqlcipher_dir):
                try:
                    os.add_dll_directory(sqlcipher_dir)
                except Exception:
                    pass
                # Recursively add all subdirectories of sqlcipher_dir to search path as well
                for root, dirs, _ in os.walk(sqlcipher_dir):
                    for d in dirs:
                        try:
                            os.add_dll_directory(os.path.abspath(os.path.join(root, d)))
                        except Exception:
                            pass

    exe_dir = os.path.dirname(sys.executable)
    if exe_dir:
        exe_dir = os.path.abspath(exe_dir)
        try:
            os.add_dll_directory(exe_dir)
        except Exception:
            pass
        exe_internal = os.path.abspath(os.path.join(exe_dir, "_internal"))
        if os.path.isdir(exe_internal):
            try:
                os.add_dll_directory(exe_internal)
            except Exception:
                pass

    # Prepend all resolved directories to the PATH environment variable to guarantee OS-level DLL resolution
    if base_dir:
        paths_to_add = [base_dir]
        if os.path.isdir(internal_dir):
            paths_to_add.append(internal_dir)
        for sqlcipher_dir in sqlcipher_dirs:
            if os.path.isdir(sqlcipher_dir):
                paths_to_add.append(sqlcipher_dir)
                for root, dirs, _ in os.walk(sqlcipher_dir):
                    for d in dirs:
                        paths_to_add.append(os.path.abspath(os.path.join(root, d)))
        if exe_dir:
            paths_to_add.append(exe_dir)
            if os.path.isdir(exe_internal):
                paths_to_add.append(exe_internal)

        unique_paths = []
        for p in paths_to_add:
            abs_p = os.path.abspath(p)
            if abs_p not in unique_paths and os.path.isdir(abs_p):
                unique_paths.append(abs_p)

        os.environ["PATH"] = ";".join(unique_paths) + ";" + os.environ.get("PATH", "")

ANSI_ESCAPE_REGEX = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape sequences from text string."""
    if not text:
        return text
    return ANSI_ESCAPE_REGEX.sub("", text)


class ANSIStrippingWriter:
    """Stream wrapper that strips ANSI escape sequences before writing."""

    def __init__(self, stream):
        """Initialize stream wrapper."""
        self.stream = stream

    def write(self, text):
        """Strip ANSI codes and write text to wrapped stream."""
        if text:
            self.stream.write(strip_ansi_codes(text))

    def flush(self):
        """Flush the wrapped stream if flush method is available."""
        if hasattr(self.stream, "flush"):
            self.stream.flush()

    def isatty(self):
        """Return isatty status of wrapped stream."""
        return getattr(self.stream, "isatty", lambda: False)()

    def __getattr__(self, attr):
        """Delegate missing stream attributes to wrapped stream."""
        return getattr(self.stream, attr)


def write_smoke_test_error(message, include_traceback=False):
    """Write smoke test diagnostic error message and traceback to file."""
    import logging
    import tempfile
    import traceback
    from pathlib import Path

    from app.config import get_app_dir
    from app.log_filter import scrub_diagnostic_text

    logger = logging.getLogger("app.main")

    err_str = message
    if include_traceback:
        err_str += "\n" + traceback.format_exc()

    # Perform inline synchronous scrubbing of diagnostic error text prior to disk output
    err_str = scrub_diagnostic_text(err_str)

    # Define primary locations
    primary_paths = []
    primary_paths.append(("current working directory", Path("smoke_test_error.txt")))
    if is_packaged():
        exe_dir = os.path.dirname(sys.executable)
        if exe_dir:
            primary_paths.append(
                ("executable directory", Path(exe_dir) / "smoke_test_error.txt")
            )

    # Try writing to primary paths
    primary_success = False
    for desc, path in primary_paths:
        try:
            abs_path = path.resolve()
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(err_str)
            primary_success = True
            logger.info(
                f"Successfully wrote diagnostic report to primary location ({desc}): {abs_path}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to write diagnostic report to primary location ({desc}) at {path}: {e}"
            )

    # Fallback writing sequence
    if not primary_success:
        fallback_paths = []
        try:
            app_dir = get_app_dir()
            fallback_paths.append(
                ("user home configuration directory", app_dir / "smoke_test_error.txt")
            )
        except Exception as e:
            logger.warning(
                f"Could not resolve user home configuration directory for fallback: {e}"
            )

        try:
            sys_temp_dir = Path(tempfile.gettempdir())
            fallback_paths.append(
                ("system temporary directory", sys_temp_dir / "smoke_test_error.txt")
            )
        except Exception as e:
            logger.warning(
                f"Could not resolve system temporary directory for fallback: {e}"
            )

        fallback_success = False
        for desc, path in fallback_paths:
            try:
                abs_path = path.resolve()
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(err_str)
                fallback_success = True
                # Log the fallback diagnostic log location to the system logger
                logger.warning(
                    f"Diagnostic log fallback write succeeded. Saved to: {abs_path}"
                )
                break
            except Exception as e:
                logger.warning(
                    f"Failed to write fallback diagnostic report to {desc} at {path}: {e}"
                )

        if not fallback_success:
            logger.error("All diagnostic log write options failed.")


def run_smoke_test():
    """Run a complete database smoke test to verify SQLCipher encryption and connectivity."""
    print("Starting automated database connection and encryption smoke test...")
    import tempfile

    # Create a temporary directory for testing to avoid side effects
    temp_dir = tempfile.mkdtemp()
    conn = None
    try:
        from app.core.db_conn import clear_connection_cache

        # Pre-flight check: try importing sqlcipher3 directly to log any specific DLL load failures
        try:
            from sqlcipher3 import dbapi2 as sqlite3_direct  # noqa: F401

            print("Direct sqlcipher3 import successful.")
        except Exception as import_err:
            import traceback  # noqa: F401

            write_smoke_test_error(
                f"Pre-flight import of sqlcipher3 failed with exception: {import_err}",
                include_traceback=True,
            )

        db_path = os.path.join(temp_dir, "smoke_test.db")
        print(f"Temporary database path: {db_path}")

        # Connect to the database using our actual connection function
        from app.core.db_conn import HAS_SQLCIPHER, get_db_connection

        if not HAS_SQLCIPHER:
            err_msg = "Error: SQLCipher driver is missing from runtime environment!"
            print(err_msg)
            write_smoke_test_error(err_msg, include_traceback=False)
            sys.exit(1)

        conn = get_db_connection(db_path)
        print("Successfully opened connection and verified SQLCipher driver.")

        # Create a test table, insert and read values
        with conn:
            cursor = conn.cursor()
            cursor.execute(
                "CREATE TABLE test_smoke (id INTEGER PRIMARY KEY, secret_val TEXT)"
            )
            cursor.execute(
                "INSERT INTO test_smoke (secret_val) VALUES (?)", ("SuperSecretData",)
            )

            cursor.execute("SELECT secret_val FROM test_smoke WHERE id = 1")
            row = cursor.fetchone()
            if not row or row[0] != "SuperSecretData":
                err_msg = "Error: Data validation failed inside the encrypted database!"
                print(err_msg)
                write_smoke_test_error(err_msg, include_traceback=False)
                sys.exit(1)

            # Double check cipher version via PRAGMA
            cursor.execute("PRAGMA cipher_version;")
            ver = cursor.fetchone()
            if not ver or not ver[0]:
                err_msg = (
                    "Error: PRAGMA cipher_version is empty! SQLCipher is not active."
                )
                print(err_msg)
                write_smoke_test_error(err_msg, include_traceback=False)
                sys.exit(1)
            print(f"Verified SQLCipher active version: {ver[0]}")

        print("Smoke test successfully completed. Encryption is active and verified!")
        sys.exit(0)
    except Exception as e:
        err_msg = f"Smoke test failed with exception: {e}"
        print(err_msg)
        write_smoke_test_error(err_msg, include_traceback=True)
        sys.exit(1)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
        try:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)
        except Exception:
            pass
        try:
            from app.core.resilient_file_ops import resilient_rmtree

            resilient_rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def _extract_bool_arg(args: argparse.Namespace, attr: str, default: bool = False) -> bool:
    """Safely extract boolean flag from CLI args namespace, handling MagicMock objects in tests."""
    from unittest.mock import MagicMock

    val = getattr(args, attr, default)
    if isinstance(val, MagicMock):
        return default
    return bool(val)


def apply_config_overrides(settings: AppSettings, args: argparse.Namespace):
    """Apply command-line argument overrides to AppSettings."""
    from unittest.mock import MagicMock

    max_folders = getattr(args, "max_folders", None)
    if max_folders is not None and not isinstance(max_folders, MagicMock):
        settings.MAX_FOLDERS = max_folders

    strategy = getattr(args, "strategy", None)
    if strategy is not None and not isinstance(strategy, MagicMock):
        settings.SORTING_STRATEGY = strategy

    conflict_policy = getattr(args, "conflict_policy", None)
    if conflict_policy is not None and not isinstance(conflict_policy, MagicMock):
        settings.CONFLICT_POLICY = conflict_policy

    contextual_renaming = getattr(args, "contextual_renaming", None)
    if contextual_renaming is not None and not isinstance(contextual_renaming, MagicMock):
        settings.CONTEXTUAL_RENAMING = contextual_renaming

    # Handle AI consent flags and environment variables
    accept_consent = _extract_bool_arg(args, "accept_ai_consent")
    decline_consent = _extract_bool_arg(args, "decline_ai_consent")
    skip_wizard = _extract_bool_arg(args, "skip_wizard")
    non_interactive_flag = _extract_bool_arg(args, "non_interactive")

    env_consent = os.environ.get("SORTIFY_AI_CONSENT")
    env_non_interactive = os.environ.get("NON_INTERACTIVE")

    # CLI consent flags take precedence over saved settings and env vars
    if accept_consent:
        settings.AI_CONSENT_GRANTED = True
    elif decline_consent:
        settings.AI_CONSENT_GRANTED = False
    elif env_consent is not None:
        env_consent_clean = env_consent.strip().lower()
        if env_consent_clean in ("1", "true", "yes", "on"):
            settings.AI_CONSENT_GRANTED = True
        elif env_consent_clean in ("0", "false", "no", "off"):
            settings.AI_CONSENT_GRANTED = False

    # Check for non-interactive mode or wizard bypass
    is_non_interactive_env = bool(
        env_non_interactive
        and env_non_interactive.strip().lower() in ("1", "true", "yes", "on")
    )
    if (
        skip_wizard
        or non_interactive_flag
        or is_non_interactive_env
        or accept_consent
        or decline_consent
    ):
        setattr(settings, "_skip_wizard", True)
        setattr(settings, "_non_interactive", True)
        if settings.AI_CONSENT_GRANTED is None:
            settings.AI_CONSENT_GRANTED = False


def resolve_preset_and_directory(args: argparse.Namespace) -> Path:
    """Resolve preset choice or target directory path from CLI arguments."""
    preset = getattr(args, "preset", None)
    directory = getattr(args, "directory", None)

    if preset:
        preset_str = str(preset).lower()
        if preset_str == "demo":
            target = Path("sandbox/demo_workspace").resolve()
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
            return target
        elif preset_str == "downloads":
            return (Path.home() / "Downloads").resolve()
        elif preset_str == "documents":
            return (Path.home() / "Documents").resolve()
        else:
            raise ValueError(
                f"Unknown preset choice '{preset}'. Choices are: demo, downloads, documents."
            )

    if directory:
        return Path(directory).resolve()

    raise ValueError("Either target directory or --preset must be specified.")


def find_all_history_sessions() -> list:
    """Scan all session directories and configuration directories for history databases."""
    from app.config import get_app_dir
    from app.core.db_conn import get_db_connection
    from app.core.path_utils import get_session_base_dir

    search_roots = [get_session_base_dir(), get_app_dir() / "sessions", get_app_dir()]
    history_db_paths = set()

    for root in search_roots:
        if not root.exists():
            continue
        db_file = root / "history.db"
        if db_file.exists():
            history_db_paths.add(db_file.resolve())
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir():
                    child_db = child / "history.db"
                    if child_db.exists():
                        history_db_paths.add(child_db.resolve())

    all_sessions = []
    seen_sids = set()

    for db_path in history_db_paths:
        try:
            conn = get_db_connection(str(db_path))
            with conn:
                cur = conn.execute(
                    "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
                )
                for row in cur.fetchall():
                    sid, ts, base_dir, status = row
                    if sid not in seen_sids:
                        seen_sids.add(sid)
                        all_sessions.append(
                            {
                                "session_id": sid,
                                "timestamp": ts,
                                "base_dir": base_dir,
                                "status": status,
                                "history_db_path": str(db_path),
                                "session_dir": str(db_path.parent),
                            }
                        )
        except Exception:
            pass

    all_sessions.sort(key=lambda s: s.get("timestamp") or 0.0, reverse=True)
    return all_sessions


def handle_sort_command(args: argparse.Namespace, settings: AppSettings):
    """Execute document batch sorting or launch interactive TUI."""
    import json
    from pathlib import Path

    apply_config_overrides(settings, args)

    if getattr(args, "tui", False) or getattr(args, "interactive", False):
        from app.ui.tui import run_tui

        run_tui(
            settings,
            getattr(args, "directory", None),
            skip_wizard=_extract_bool_arg(args, "skip_wizard"),
            non_interactive=_extract_bool_arg(args, "non_interactive"),
        )
        sys.exit(0)

    try:
        target_path = resolve_preset_and_directory(args)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    if not target_path.exists() or not target_path.is_dir():
        print(
            f"Error: Target directory '{target_path}' does not exist or is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    args.directory = str(target_path)

    session = None
    try:
        from app.core.extractor import build_corpus_generator
        from app.core.scanner import get_files_recursively
        from app.core.session import AppSession

        session = AppSession(settings, base_dir=str(target_path))
        files = get_files_recursively(str(target_path))

        def progress_cb(info=None):
            pass

        generator = build_corpus_generator(
            base_dir=str(target_path),
            items_to_sort=files,
            progress_callback=progress_cb,
            max_workers=settings.MAX_WORKERS,
            db=session.db,
            chunk_size=50,
            settings=settings,
        )

        for chunk in generator:
            session.partial_fit(chunk)

        plan = session.generate_sorting_plan()

        dest_dir = getattr(args, "dest_dir", None)
        dry_run = getattr(args, "dry_run", False)
        json_output = getattr(args, "json", False)

        if dest_dir:
            dest_base = Path(dest_dir).resolve()
            dest_base.mkdir(parents=True, exist_ok=True)
            re_rooted_plan = {}
            plan_items = (
                plan.plan.items()
                if hasattr(plan, "plan") and isinstance(plan.plan, dict)
                else plan.items()
            )
            for k, v in plan_items:
                if os.path.isabs(k):
                    re_rooted_plan[k] = v
                else:
                    new_key = str(dest_base / k)
                    re_rooted_plan[new_key] = v
            plan = re_rooted_plan

        serializable_plan = _make_json_serializable(plan)
        if dry_run:
            result = {
                "status": "success",
                "dry_run": True,
                "target_directory": str(target_path),
                "destination_directory": (
                    str(Path(dest_dir).resolve()) if dest_dir else str(target_path)
                ),
                "plan": serializable_plan,
            }
        else:
            summary = session.execute_moves(plan)
            result = {
                "status": "success",
                "dry_run": False,
                "target_directory": str(target_path),
                "destination_directory": (
                    str(Path(dest_dir).resolve()) if dest_dir else str(target_path)
                ),
                "plan": serializable_plan,
                "summary": summary,
            }

        quiet = getattr(args, "quiet", False)
        if json_output:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not quiet:
                print(
                    f"Batch sorting completed successfully for '{target_path}'.",
                    file=sys.stderr,
                )
                if dry_run:
                    print("Dry-run mode: no files were moved.", file=sys.stderr)
            print(json.dumps(serializable_plan, indent=2))

        sys.exit(0)
    except Exception as e:
        print(f"Error during sorting operation: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if session is not None:
            session.close()


def handle_scan_command(args: argparse.Namespace, settings: AppSettings):
    """Execute directory scanning and sorting analysis or launch interactive TUI."""
    import json

    apply_config_overrides(settings, args)

    if getattr(args, "tui", False) or getattr(args, "interactive", False):
        from app.ui.tui import run_tui

        run_tui(
            settings,
            getattr(args, "directory", None),
            skip_wizard=_extract_bool_arg(args, "skip_wizard"),
            non_interactive=_extract_bool_arg(args, "non_interactive"),
        )
        sys.exit(0)

    try:
        target_path = resolve_preset_and_directory(args)
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    if not target_path.exists() or not target_path.is_dir():
        print(
            f"Error: Target directory '{target_path}' does not exist or is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)

    args.directory = str(target_path)

    apply_config_overrides(settings, args)

    session = None
    try:
        from app.core.extractor import build_corpus_generator
        from app.core.scanner import get_files_recursively
        from app.core.session import AppSession

        session = AppSession(settings, base_dir=str(target_path))
        files = get_files_recursively(str(target_path))

        def progress_cb(info=None):
            pass

        generator = build_corpus_generator(
            base_dir=str(target_path),
            items_to_sort=files,
            progress_callback=progress_cb,
            max_workers=settings.MAX_WORKERS,
            db=session.db,
            chunk_size=50,
            settings=settings,
        )

        for chunk in generator:
            session.partial_fit(chunk)

        plan = session.generate_sorting_plan()
        serializable_plan = _make_json_serializable(plan)

        result = {
            "status": "success",
            "target_directory": str(target_path),
            "files_scanned": len(files),
            "plan": serializable_plan,
        }

        quiet = getattr(args, "quiet", False)
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not quiet:
                print(
                    f"Scan analysis completed for '{target_path}'. Scanned {len(files)} files.",
                    file=sys.stderr,
                )
            print(json.dumps(serializable_plan, indent=2))

        sys.exit(0)
    except Exception as e:
        print(f"Error during scan operation: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if session is not None:
            session.close()


def handle_config_command(args: argparse.Namespace, settings: AppSettings):
    """View or update application settings."""
    import json

    apply_config_overrides(settings, args)

    if getattr(args, "set", None):
        for key, value in args.set:
            key_upper = key.upper()
            if hasattr(settings._settings_model, key_upper):
                curr_val = getattr(settings, key_upper)
                try:
                    if isinstance(curr_val, bool):
                        new_val = value.lower() in ("true", "1", "yes")
                    elif isinstance(curr_val, int):
                        new_val = int(value)
                    elif isinstance(curr_val, float):
                        new_val = float(value)
                    else:
                        new_val = value
                    setattr(settings, key_upper, new_val)
                except Exception as ex:
                    print(
                        f"Error: Failed to set configuration key '{key_upper}' to '{value}': {ex}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            else:
                print(
                    f"Error: Unknown configuration key '{key}'.",
                    file=sys.stderr,
                )
                sys.exit(1)

    quiet = getattr(args, "quiet", False)
    settings_dict = settings._settings_model.model_dump(mode="json")
    if args.json:
        sys.stdout.write(json.dumps(settings_dict, indent=2) + "\n")
        sys.stdout.flush()
    else:
        if not quiet:
            print("Application Settings:", file=sys.stderr)
        for k, v in settings_dict.items():
            print(f"  {k}: {v}")

    sys.exit(0)


def handle_daemon_command(args: argparse.Namespace, settings: AppSettings):
    """Launch or send control commands to persistent directory-watching daemon."""
    import asyncio
    import json
    import time
    from pathlib import Path

    from app.core.daemon import start_daemon
    from app.core.ipc import DaemonIPCClient

    apply_config_overrides(settings, args)

    known_actions = {"status", "health", "metrics", "pause", "resume", "stop", "start"}

    first_arg = getattr(args, "action", None)
    second_arg = getattr(args, "directory", None)

    if first_arg in known_actions:
        action = first_arg
        target_dir = second_arg or os.getcwd()
    elif first_arg is not None:
        action = "start"
        target_dir = first_arg
    else:
        action = "start"
        target_dir = second_arg or os.getcwd()

    target_path = Path(target_dir).resolve()

    if action == "start":
        if not target_path.exists() or not target_path.is_dir():
            print(
                f"Error: Target directory '{target_dir}' does not exist or is not a directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        start_daemon(settings, str(target_path))
    else:
        client = DaemonIPCClient(str(target_path))
        is_watch = getattr(args, "watch", False)

        if action == "status" and is_watch:
            print(
                f"Monitoring daemon status telemetry for '{target_path}' (Press Ctrl+C to exit)..."
            )
            try:
                while True:
                    try:
                        res = asyncio.run(client.send_command("status"))
                        status_str = str(res.get("status", "unknown")).upper()
                        pid = res.get("pid", "unknown")
                        qd = res.get("queue_depth", 0)
                        max_cap = res.get("max_queue_capacity", 1000)
                        workers = res.get("active_workers", 0)
                        uptime = res.get("uptime_seconds", 0)
                        triages = res.get("active_triage_paths", [])
                        print(
                            f"[{time.strftime('%H:%M:%S')}] Status: {status_str} (PID {pid}) | Queue: {qd}/{max_cap} | Workers: {workers} | Uptime: {uptime}s | Active Triage: {len(triages)}"
                        )
                    except Exception as err:
                        print(f"Error querying daemon status: {err}", file=sys.stderr)
                    time.sleep(1.0)
            except KeyboardInterrupt:
                print("\nStopped watch monitoring.")
                sys.exit(0)

        try:
            res = asyncio.run(client.send_command(action))
            if action == "status":
                print(f"Daemon Status (PID: {res.get('pid')}):")
                print(f"  Status: {str(res.get('status', '')).upper()}")
                print(f"  Base Directory: {res.get('base_dir')}")
                print(f"  Uptime: {res.get('uptime_seconds')}s")
                print(
                    f"  Queue Depth: {res.get('queue_depth')} / {res.get('max_queue_capacity')}"
                )
                print(f"  Active Workers: {res.get('active_workers')}")
                print(f"  Relocating Files: {res.get('is_moving')}")
                print(
                    f"  Active Triage Paths: {res.get('active_triage_paths') or 'None'}"
                )
            elif action == "health":
                print("Daemon Health Check:")
                print(f"  Health Status: {str(res.get('status', '')).upper()}")
                print(f"  PID: {res.get('pid')}")
                print(f"  Queue Depth: {res.get('queue_depth')}")
                print(f"  Queue Full: {res.get('queue_full')}")
                print(f"  Uptime: {res.get('uptime_seconds')}s")
            elif action == "metrics":
                print("Daemon Metrics:")
                print(f"  Events Enqueued: {res.get('events_enqueued')}")
                print(f"  Events Processed: {res.get('events_processed')}")
                print(f"  Queue Depth: {res.get('queue_depth')}")
                print(f"  Active Workers: {res.get('active_workers')}")
                print(f"  Uptime: {res.get('uptime_seconds')}s")
            elif action == "pause":
                print(f"Daemon paused successfully ({res.get('message')}).")
            elif action == "resume":
                print(f"Daemon resumed successfully ({res.get('message')}).")
            elif action == "stop":
                print(f"Daemon shutdown initiated ({res.get('message')}).")
            else:
                print(json.dumps(res, indent=2))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


def handle_undo_command(args: argparse.Namespace, settings: AppSettings):
    """Execute undo subcommand for session rollback or listing history records."""
    import json
    import time
    from pathlib import Path

    apply_config_overrides(settings, args)

    list_flag = getattr(args, "list", False)
    latest_flag = getattr(args, "latest", False)
    session_id_arg = getattr(args, "session_id", None)
    force_flag = getattr(args, "force", False)
    json_output = getattr(args, "json", False)

    # Constraint: Default to --latest when no session ID or subcommand flag specified
    if not list_flag and not latest_flag and not session_id_arg:
        latest_flag = True

    all_sessions = find_all_history_sessions()

    if list_flag:
        if json_output:
            sys.stdout.write(json.dumps(all_sessions, indent=2) + "\n")
            sys.stdout.flush()
        else:
            if not all_sessions:
                print("No historical sorting sessions found.", file=sys.stderr)
            else:
                quiet = getattr(args, "quiet", False)
                if not quiet:
                    print("Historical Sorting Sessions:", file=sys.stderr)
                for s in all_sessions:
                    ts = s.get("timestamp")
                    ts_str = (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                        if ts
                        else "N/A"
                    )
                    print(
                        f"  Session ID: {s['session_id']} | Base: {s.get('base_dir', 'N/A')} | Status: {s.get('status', 'N/A')} | Time: {ts_str}"
                    )
        sys.exit(0)

    target_session = None
    if session_id_arg:
        for s in all_sessions:
            if s["session_id"] == session_id_arg:
                target_session = s
                break
        if not target_session:
            target_session = {
                "session_id": session_id_arg,
                "history_db_path": None,
                "base_dir": None,
            }
    elif latest_flag:
        if all_sessions:
            target_session = all_sessions[0]
        else:
            msg = "Error: No historical sorting sessions available to undo."
            if json_output:
                sys.stdout.write(
                    json.dumps({"status": "error", "message": msg}, indent=2) + "\n"
                )
                sys.stdout.flush()
            else:
                print(msg, file=sys.stderr)
            sys.exit(1)

    target_session_id = target_session["session_id"]
    db_path = target_session.get("history_db_path")
    base_dir = target_session.get("base_dir")

    session_obj = None
    try:
        from app.core.cache import CacheManager
        from app.core.db import Database
        from app.core.db_worker import DBWorker
        from app.core.history import HistoryManager

        if db_path and Path(db_path).exists():
            session_dir = Path(db_path).parent
            db_worker = DBWorker()
            db = Database(session_dir / "autosorter.db", db_worker)
            cache_mgr = CacheManager(str(session_dir / "cache.db"), db_worker)
            history_mgr = HistoryManager(db, cache_mgr, str(db_path))
        else:
            from app.core.session import AppSession

            session_obj = AppSession(settings, base_dir=base_dir)
            history_mgr = session_obj.history_manager

        history_mgr.rollback(target_session_id, ignore_missing=force_flag)

        result = {
            "status": "success",
            "session_id": target_session_id,
            "message": f"Successfully rolled back sorting session '{target_session_id}'.",
        }

        if json_output:
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            sys.stdout.flush()
        else:
            quiet = getattr(args, "quiet", False)
            if not quiet:
                print(
                    f"Rollback completed successfully for session '{target_session_id}'.",
                    file=sys.stderr,
                )

        sys.exit(0)
    except Exception as ex:
        err_msg = f"Rollback failed for session '{target_session_id}': {ex}"
        if json_output:
            sys.stdout.write(
                json.dumps(
                    {
                        "status": "error",
                        "session_id": target_session_id,
                        "message": str(ex),
                    },
                    indent=2,
                )
                + "\n"
            )
            sys.stdout.flush()
        else:
            print(err_msg, file=sys.stderr)
        sys.exit(1)
    finally:
        if session_obj is not None:
            session_obj.close()


def build_parser(prog: str | None = "app/main.py") -> argparse.ArgumentParser:
    """Build and return the main command-line argument parser for Smart AutoSorter AI Pro."""
    parser = argparse.ArgumentParser(prog=prog, description="Smart AutoSorter AI Pro")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        dest="quiet",
        help="Suppress informational prints and non-essential progress output",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        dest="no_color",
        help="Disable ANSI color and style formatting",
    )
    parser.add_argument(
        "--demo", action="store_true", help="Run interactive CLI demo mode"
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run automated database smoke test and exit",
    )
    parser.add_argument(
        "--update-snapshots",
        action="store_true",
        help="Regenerate reference baseline snapshots across all covered views",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Launch the persistent directory-watching daemon",
    )
    parser.add_argument(
        "--debug-layout",
        action="store_true",
        help="Enable visual debug outlines for UI elements in dev mode",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch full-screen Textual TUI interface",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Launch full-screen interactive TUI mode",
    )
    ai_consent_group = parser.add_mutually_exclusive_group()
    ai_consent_group.add_argument(
        "--accept-ai-consent",
        action="store_true",
        default=False,
        help="Pre-configure AI consent as granted and bypass onboarding wizard",
    )
    ai_consent_group.add_argument(
        "--decline-ai-consent",
        action="store_true",
        default=False,
        help="Pre-configure AI consent as declined and bypass onboarding wizard",
    )
    parser.add_argument(
        "--skip-wizard",
        action="store_true",
        default=False,
        help="Bypass onboarding wizard modal during startup",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Run in non-interactive mode and bypass interactive modal dialogs",
    )

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    def add_common_override_args(subparser):
        subparser.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            dest="quiet",
            default=argparse.SUPPRESS,
            help="Suppress informational prints and non-essential progress output",
        )
        subparser.add_argument(
            "--no-color",
            action="store_true",
            dest="no_color",
            default=argparse.SUPPRESS,
            help="Disable ANSI color and style formatting",
        )
        subparser.add_argument(
            "--max-folders",
            type=int,
            default=None,
            help="Maximum number of generated subfolders",
        )
        subparser.add_argument(
            "--strategy",
            type=str,
            choices=["default", "generative", "clinical_tmf", "clinical_isf"],
            default=None,
            help="Sorting strategy",
        )
        subparser.add_argument(
            "--conflict-policy",
            type=str,
            choices=["skip", "rename"],
            default=None,
            help="Conflict resolution policy",
        )
        subparser.add_argument(
            "--contextual-renaming",
            action="store_true",
            default=None,
            help="Enable AI contextual renaming",
        )
        subparser.add_argument(
            "--no-contextual-renaming",
            action="store_false",
            dest="contextual_renaming",
            help="Disable AI contextual renaming",
        )
        subparser.add_argument(
            "--tui",
            action="store_true",
            default=False,
            help="Launch full-screen Textual TUI interface",
        )
        subparser.add_argument(
            "--interactive",
            action="store_true",
            default=False,
            help="Launch full-screen interactive TUI mode",
        )
        sub_ai_consent = subparser.add_mutually_exclusive_group()
        sub_ai_consent.add_argument(
            "--accept-ai-consent",
            action="store_true",
            default=False,
            help="Pre-configure AI consent as granted and bypass onboarding wizard",
        )
        sub_ai_consent.add_argument(
            "--decline-ai-consent",
            action="store_true",
            default=False,
            help="Pre-configure AI consent as declined and bypass onboarding wizard",
        )
        subparser.add_argument(
            "--skip-wizard",
            action="store_true",
            default=False,
            help="Bypass onboarding wizard modal during startup",
        )
        subparser.add_argument(
            "--non-interactive",
            action="store_true",
            default=False,
            help="Run in non-interactive mode and bypass interactive modal dialogs",
        )

    # Subcommand: sort
    parser_sort = subparsers.add_parser(
        "sort", help="Run document sorting in headless batch processing mode"
    )
    parser_sort.add_argument(
        "directory", nargs="?", default=None, type=str, help="Target directory to sort"
    )
    parser_sort.add_argument(
        "--preset",
        type=str,
        choices=["demo", "downloads", "documents"],
        default=None,
        help="Use standard workspace preset directory (demo, downloads, documents)",
    )
    parser_sort.add_argument(
        "--json",
        action="store_true",
        help="Output result in structured JSON format",
    )
    parser_sort.add_argument(
        "--dest-dir",
        type=str,
        default=None,
        help="Destination directory for sorted files",
    )
    parser_sort.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform dry run analysis without executing physical moves",
    )
    add_common_override_args(parser_sort)

    # Subcommand: scan
    parser_scan = subparsers.add_parser(
        "scan", help="Run directory scanning and analysis without moving files"
    )
    parser_scan.add_argument(
        "directory", nargs="?", default=None, type=str, help="Target directory to scan"
    )
    parser_scan.add_argument(
        "--preset",
        type=str,
        choices=["demo", "downloads", "documents"],
        default=None,
        help="Use standard workspace preset directory (demo, downloads, documents)",
    )
    parser_scan.add_argument(
        "--json",
        action="store_true",
        help="Output scan plan in structured JSON format",
    )
    add_common_override_args(parser_scan)

    # Subcommand: config
    parser_config = subparsers.add_parser(
        "config", help="View or update application configuration settings"
    )
    parser_config.add_argument(
        "--show",
        action="store_true",
        help="Display current configuration settings",
    )
    parser_config.add_argument(
        "--json",
        action="store_true",
        help="Output configuration as JSON",
    )
    parser_config.add_argument(
        "--set",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="Set configuration KEY to VALUE",
    )
    add_common_override_args(parser_config)

    # Subcommand: daemon
    parser_daemon = subparsers.add_parser(
        "daemon", help="Launch or control persistent directory-watching daemon"
    )
    parser_daemon.add_argument(
        "action",
        nargs="?",
        default=None,
        help="Action (start, status, health, metrics, pause, resume, stop) or directory to watch",
    )
    parser_daemon.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="Directory to watch or control",
    )
    parser_daemon.add_argument(
        "--watch",
        action="store_true",
        help="Continuously monitor status telemetry in real time",
    )
    add_common_override_args(parser_daemon)

    # Subcommand: undo
    parser_undo = subparsers.add_parser(
        "undo", help="Rollback sorting operations and manage history sessions"
    )
    parser_undo.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Specific historical session ID UUID to rollback",
    )
    parser_undo.add_argument(
        "--list",
        action="store_true",
        help="List active and completed historical sorting sessions",
    )
    parser_undo.add_argument(
        "--latest",
        action="store_true",
        help="Rollback the latest historical sorting session",
    )
    parser_undo.add_argument(
        "--force",
        action="store_true",
        help="Force rollback even if original files are missing",
    )
    parser_undo.add_argument(
        "--json",
        action="store_true",
        help="Output session list or rollback status in structured JSON format",
    )
    add_common_override_args(parser_undo)

    # Register modular domain subcommands
    from app.cli import register_cli_subcommands

    register_cli_subcommands(subparsers)

    return parser


def main():
    """Execute the main application GUI or Demo."""
    import multiprocessing
    import sys

    multiprocessing.freeze_support()

    parser = build_parser()

    known_subcommands = (
        "sort",
        "scan",
        "config",
        "daemon",
        "ledger",
        "quarantine",
        "undo",
    )
    legacy_directory = None
    if (
        len(sys.argv) > 1
        and not sys.argv[1].startswith("-")
        and sys.argv[1] not in known_subcommands
    ):
        legacy_directory = sys.argv.pop(1)

    args = parser.parse_args()
    if legacy_directory and not getattr(args, "directory", None):
        args.directory = legacy_directory
    if not hasattr(args, "directory"):
        args.directory = None

    if not hasattr(args, "quiet"):
        args.quiet = False
    if not hasattr(args, "no_color"):
        args.no_color = False

    no_color = getattr(args, "no_color", False) or bool(os.environ.get("NO_COLOR"))
    if no_color:
        if sys.stdout is not None:
            sys.stdout = ANSIStrippingWriter(sys.stdout)
        if sys.stderr is not None:
            sys.stderr = ANSIStrippingWriter(sys.stderr)

    if getattr(args, "update_snapshots", False) is True:
        import pytest

        print("Regenerating baseline snapshots across all covered views...")
        os.environ["UPDATE_SNAPSHOTS"] = "1"
        exit_code = pytest.main(["tests/test_visual_snapshots.py"])
        sys.exit(exit_code)

    if getattr(args, "smoke_test", False) is True:
        run_smoke_test()

    settings = AppSettings()
    apply_config_overrides(settings, args)

    # Configure Centralized Logger
    logging.basicConfig(
        filename=settings.LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    )

    # Create and add the log scrubbing filter to the root logger
    root_logger = logging.getLogger()

    # Also apply to handlers to ensure child loggers are filtered
    scrubber = LogScrubbingFilter(str(Path.home()))
    root_logger.addFilter(scrubber)
    for handler in root_logger.handlers:
        handler.addFilter(scrubber)

    # Direct subcommand execution
    if getattr(args, "subcommand", None) == "sort":
        handle_sort_command(args, settings)
    elif getattr(args, "subcommand", None) == "scan":
        handle_scan_command(args, settings)
    elif getattr(args, "subcommand", None) == "config":
        handle_config_command(args, settings)
    elif getattr(args, "subcommand", None) == "daemon":
        handle_daemon_command(args, settings)
    elif getattr(args, "subcommand", None) == "undo":
        handle_undo_command(args, settings)
    else:
        from app.cli import handle_cli_command

        if handle_cli_command(args, settings):
            return

    # Explicit Headless Guard for non-interactive streams without subcommands
    is_interactive = sys.stdin is not None and sys.stdin.isatty()
    is_explicit_ui = (
        getattr(args, "tui", False)
        or getattr(args, "interactive", False)
        or getattr(args, "demo", False)
        or getattr(args, "daemon", False)
    )

    if not is_interactive and not is_explicit_ui:
        if getattr(args, "directory", None):
            handle_sort_command(args, settings)
        else:
            parser.print_help(sys.stderr)
            sys.exit(2)

    # Verify embedded model integrity upfront if packaged / sandboxed
    if is_packaged():
        print("Verifying integrity of embedded model weights...", file=sys.stderr)
        try:
            from app.core.verifier import check_ai_status

            check_ai_status(settings)
            print("Model weights integrity verified successfully.", file=sys.stderr)
        except Exception as e:
            print(f"Startup verification failed: {e}", file=sys.stderr)
            write_smoke_test_error(
                f"Startup verification failed: {e}", include_traceback=True
            )
            sys.exit(1)

    # Automated headless startup reconciliation scan for incomplete file relocations
    try:
        from app.core.ledger import TransactionLedger

        ledger = TransactionLedger()
        ledger.reconcile_incomplete_transactions()
    except Exception as exc:
        logging.warning(
            f"Headless transaction ledger reconciliation on startup failed: {exc}"
        )

    if getattr(args, "daemon", False) is True:
        from app.core.daemon import start_daemon

        start_daemon(settings, args.directory)
    elif args.demo:
        from app.demo import run_demo

        run_demo(settings)
    else:
        from app.ui.tui import run_tui

        run_tui(
            settings,
            args.directory,
            skip_wizard=_extract_bool_arg(args, "skip_wizard"),
            non_interactive=_extract_bool_arg(args, "non_interactive"),
        )


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
