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
    import shutil
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
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def apply_config_overrides(settings: AppSettings, args: argparse.Namespace):
    """Apply command-line argument overrides to AppSettings."""
    if getattr(args, "max_folders", None) is not None:
        settings.MAX_FOLDERS = args.max_folders
    if getattr(args, "strategy", None) is not None:
        settings.SORTING_STRATEGY = args.strategy
    if getattr(args, "conflict_policy", None) is not None:
        settings.CONFLICT_POLICY = args.conflict_policy
    if getattr(args, "contextual_renaming", None) is not None:
        settings.CONTEXTUAL_RENAMING = args.contextual_renaming


def handle_sort_command(args: argparse.Namespace, settings: AppSettings):
    """Execute non-interactive document batch sorting."""
    import json
    from pathlib import Path

    target_path = Path(args.directory).resolve()
    if not target_path.exists() or not target_path.is_dir():
        print(
            f"Error: Target directory '{args.directory}' does not exist or is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)

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

        dest_dir = getattr(args, "dest_dir", None)
        dry_run = getattr(args, "dry_run", False)
        json_output = getattr(args, "json", False)

        if dest_dir:
            dest_base = Path(dest_dir).resolve()
            dest_base.mkdir(parents=True, exist_ok=True)
            re_rooted_plan = {}
            for k, v in plan.items():
                if os.path.isabs(k):
                    re_rooted_plan[k] = v
                else:
                    new_key = str(dest_base / k)
                    re_rooted_plan[new_key] = v
            plan = re_rooted_plan

        if dry_run:
            result = {
                "status": "success",
                "dry_run": True,
                "target_directory": str(target_path),
                "destination_directory": (
                    str(Path(dest_dir).resolve())
                    if dest_dir
                    else str(target_path)
                ),
                "plan": plan,
            }
        else:
            summary = session.execute_moves(plan)
            result = {
                "status": "success",
                "dry_run": False,
                "target_directory": str(target_path),
                "destination_directory": (
                    str(Path(dest_dir).resolve())
                    if dest_dir
                    else str(target_path)
                ),
                "plan": plan,
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
            print(json.dumps(plan, indent=2))

        sys.exit(0)
    except Exception as e:
        print(f"Error during sorting operation: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if session is not None:
            session.close()


def handle_scan_command(args: argparse.Namespace, settings: AppSettings):
    """Execute directory scanning and sorting analysis without moving files."""
    import json
    from pathlib import Path

    target_path = Path(args.directory).resolve()
    if not target_path.exists() or not target_path.is_dir():
        print(
            f"Error: Target directory '{args.directory}' does not exist or is not a directory.",
            file=sys.stderr,
        )
        sys.exit(1)

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

        result = {
            "status": "success",
            "target_directory": str(target_path),
            "files_scanned": len(files),
            "plan": plan,
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
            print(json.dumps(plan, indent=2))

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
    """Launch persistent directory-watching daemon."""
    from pathlib import Path

    from app.core.daemon import start_daemon

    apply_config_overrides(settings, args)

    target_dir = args.directory
    if target_dir:
        target_path = Path(target_dir).resolve()
        if not target_path.exists() or not target_path.is_dir():
            print(
                f"Error: Target directory '{target_dir}' does not exist or is not a directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        target_dir = str(target_path)

    start_daemon(settings, target_dir)


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
        "--gui",
        action="store_true",
        help="Force launch graphical web interface (Deprecated: launches terminal interface)",
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

    # Subcommand: sort
    parser_sort = subparsers.add_parser(
        "sort", help="Run document sorting in headless batch processing mode"
    )
    parser_sort.add_argument("directory", type=str, help="Target directory to sort")
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
    parser_scan.add_argument("directory", type=str, help="Target directory to scan")
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
        "daemon", help="Launch the persistent directory-watching daemon"
    )
    parser_daemon.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="Directory to watch",
    )
    add_common_override_args(parser_daemon)

    return parser


def main():
    """Execute the main application GUI or Demo."""
    import multiprocessing
    import sys

    multiprocessing.freeze_support()

    parser = build_parser()

    legacy_directory = None
    if (
        len(sys.argv) > 1
        and not sys.argv[1].startswith("-")
        and sys.argv[1] not in ("sort", "scan", "config", "daemon")
    ):
        legacy_directory = sys.argv.pop(1)

    args = parser.parse_args()
    if legacy_directory and not getattr(args, "directory", None):
        args.directory = legacy_directory

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
    elif (
        sys.stdin is not None
        and hasattr(sys.stdin, "isatty")
        and not sys.stdin.isatty()
        and getattr(args, "directory", None)
        and not getattr(args, "tui", False)
    ):
        handle_sort_command(args, settings)
    else:
        from app.ui.tui import run_tui

        if getattr(args, "gui", False) or os.environ.get("FORCE_GUI"):
            print(
                "Notice: Web GUI interface is deprecated. Launching native terminal TUI interface...",
                file=sys.stderr,
            )

        run_tui(settings, args.directory)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
