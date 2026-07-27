"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI or CLI demo.
"""

import os
import sys

# Dynamic Windows DLL Path Injection
if sys.platform == "win32" and getattr(sys, "frozen", False):
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # Locate the folder where PyInstaller bundles the sqlcipher3 binaries
    sqlcipher_dir = os.path.join(base_dir, "sqlcipher3")
    if os.path.isdir(sqlcipher_dir):
        try:
            os.add_dll_directory(sqlcipher_dir)
        except Exception:
            # Log or ignore if already added
            pass

import argparse
import logging
from pathlib import Path

from app.config import AppSettings
from app.log_filter import LogScrubbingFilter


def run_smoke_test():
    """Run a complete database smoke test to verify SQLCipher encryption and connectivity."""
    print("Starting automated database connection and encryption smoke test...")
    import shutil
    import tempfile
    
    # Create a temporary directory for testing to avoid side effects
    temp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(temp_dir, "smoke_test.db")
        print(f"Temporary database path: {db_path}")
        
        # Connect to the database using our actual connection function
        from app.core.db_conn import HAS_SQLCIPHER, get_db_connection
        
        if not HAS_SQLCIPHER:
            print("Error: SQLCipher driver is missing from runtime environment!")
            sys.exit(1)
            
        conn = get_db_connection(db_path)
        print("Successfully opened connection and verified SQLCipher driver.")
        
        # Create a test table, insert and read values
        with conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE test_smoke (id INTEGER PRIMARY KEY, secret_val TEXT)")
            cursor.execute("INSERT INTO test_smoke (secret_val) VALUES (?)", ("SuperSecretData",))
            
            cursor.execute("SELECT secret_val FROM test_smoke WHERE id = 1")
            row = cursor.fetchone()
            if not row or row[0] != "SuperSecretData":
                print("Error: Data validation failed inside the encrypted database!")
                sys.exit(1)
                
            # Double check cipher version via PRAGMA
            cursor.execute("PRAGMA cipher_version;")
            ver = cursor.fetchone()
            if not ver or not ver[0]:
                print("Error: PRAGMA cipher_version is empty! SQLCipher is not active.")
                sys.exit(1)
            print(f"Verified SQLCipher active version: {ver[0]}")
            
        print("Smoke test successfully completed. Encryption is active and verified!")
        sys.exit(0)
    except Exception as e:
        import traceback
        print(f"Smoke test failed with exception: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
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
        "--smoke-test", action="store_true", help="Run automated database smoke test and exit"
    )
    parser.add_argument(
        "directory", nargs="?", default=None, help="Directory to analyze automatically"
    )

    args = parser.parse_args()

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
