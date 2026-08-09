"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI or CLI demo.
"""

import os
import sys

# Dynamic Windows DLL Path Injection
from app.core.path_utils import is_packaged

if sys.platform == "win32" and is_packaged():
    # Safeguard standard streams to prevent crash on print when sys.stdout/err are None
    class NullWriter:
        """A helper class that discards any written output to mimic a stream."""

        def write(self, text):
            """Discard written text.

            Parameters
            ----------
            text : str
                The text to write.
            """
            pass

        def flush(self):
            """No-op flush to satisfy the stream interface."""
            pass

    if sys.stdout is None:
        sys.stdout = NullWriter()
    if sys.stderr is None:
        sys.stderr = NullWriter()

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

# Early import of db_conn to run user-space bootstrapping and register SQLCipher/sqlite3 overrides immediately
import argparse
import logging
from pathlib import Path

from app.config import AppSettings
from app.core import db_conn  # noqa: F401
from app.log_filter import LogScrubbingFilter


def write_smoke_test_error(message, include_traceback=False):
    """Write smoke test diagnostic error message and traceback to file."""
    import logging
    import tempfile
    import traceback
    from pathlib import Path

    from app.config import get_app_dir

    logger = logging.getLogger("app.main")

    err_str = message
    if include_traceback:
        err_str += "\n" + traceback.format_exc()

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

    from app.core.db_conn import clear_connection_cache

    # Create a temporary directory for testing to avoid side effects
    temp_dir = tempfile.mkdtemp()
    conn = None
    try:
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
            clear_connection_cache()
        except Exception:
            pass
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def main():
    """Execute the main application GUI or Demo."""
    import multiprocessing

    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(description="Smart AutoSorter AI Pro")
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
        "directory", nargs="?", default=None, help="Directory to analyze automatically"
    )

    args = parser.parse_args()

    if getattr(args, "update_snapshots", False) is True:
        import os
        import sys

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
        level=logging.ERROR,
        format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    )

    # Create and add the log scrubbing filter to the root logger
    root_logger = logging.getLogger()

    # Also apply to handlers to ensure child loggers are filtered
    scrubber = LogScrubbingFilter(str(Path.home()))
    root_logger.addFilter(scrubber)
    for handler in root_logger.handlers:
        handler.addFilter(scrubber)

    if args.demo:
        from app.demo import run_demo

        run_demo(settings)
    else:
        from app.ui.app import run_app

        run_app(settings, args.directory)


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    main()
