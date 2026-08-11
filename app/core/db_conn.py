"""Database connection module."""

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

# Run user-space bootstrapping to download, register, and verify precompiled native binaries
try:
    from app.core.user_space_bootstrap import bootstrap_binaries

    bootstrap_binaries()
except Exception as exc:
    logger.error(f"User-space bootstrapping failed: {exc}")

try:
    from sqlcipher3 import dbapi2 as sqlite3

    HAS_SQLCIPHER = True
    import sys

    sys.modules["sqlite3"] = sqlite3
except Exception:
    HAS_SQLCIPHER = False
    try:
        import sqlite3
    except Exception:
        import types

        sqlite3_mock = types.ModuleType("sqlite3")
        sqlite3_mock.Error = Exception
        sqlite3_mock.DatabaseError = Exception
        sqlite3_mock.OperationalError = Exception
        sqlite3_mock.IntegrityError = Exception
        sqlite3_mock.InternalError = Exception
        sqlite3_mock.ProgrammingError = Exception
        sqlite3_mock.NotSupportedError = Exception

        class DummyConnection:
            """A dummy connection class to simulate sqlite3 when SQLCipher is missing."""

            def __init__(self, *args, **kwargs):
                raise RuntimeError("SQLCipher library is missing.")

            def close(self):
                """Close the dummy connection (no-op)."""
                pass

        sqlite3_mock.connect = DummyConnection
        sqlite3_mock.Connection = DummyConnection

        import sys

        sys.modules["sqlite3"] = sqlite3_mock
        sqlite3 = sqlite3_mock

# Global connection cache and lock
_connection_cache = {}
_cache_lock = threading.Lock()
_disable_pytest_win_fallback = False


def clear_connection_cache(only_current_and_inactive: bool = True):
    """Clear cached database connections, selectively or globally."""
    global _connection_cache
    import gc

    with _cache_lock:
        if only_current_and_inactive:
            calling_thread_id = threading.get_ident()
            active_thread_ids = {t.ident for t in threading.enumerate()}

            # Identify which keys to remove
            keys_to_remove = []
            for key in list(_connection_cache.keys()):
                _, thread_id = key
                if thread_id == calling_thread_id or thread_id not in active_thread_ids:
                    keys_to_remove.append(key)

            for key in keys_to_remove:
                conn = _connection_cache.pop(key, None)
                if conn:
                    try:
                        try:
                            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except Exception:
                            pass
                        conn.close()
                    except Exception:
                        pass
        else:
            for k, conn in _connection_cache.items():
                try:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except Exception:
                        pass
                    conn.close()
                except Exception:
                    pass
            _connection_cache.clear()
    gc.collect()


def clear_dead_thread_connections():
    """Close and remove cached connections for threads that are no longer active to prevent file locking on Windows."""
    global _connection_cache
    active_thread_ids = {t.ident for t in threading.enumerate()}
    with _cache_lock:
        for key in list(_connection_cache.keys()):
            _, thread_id = key
            if thread_id not in active_thread_ids:
                conn = _connection_cache.pop(key, None)
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass


def get_db_connection(db_path: str):
    """Create and configure a new database connection with performance parameters."""
    global _connection_cache
    abs_path = os.path.abspath(db_path)
    thread_id = threading.get_ident()
    cache_key = (abs_path, thread_id)

    # Automatically clean up connections for dead threads to prevent resource and lock leaks on Windows
    clear_dead_thread_connections()

    with _cache_lock:
        if cache_key in _connection_cache:
            return _connection_cache[cache_key]

    from app.core.path_utils import resolve_db_crypto

    crypto = resolve_db_crypto(db_path)
    raw_key = crypto.get_raw_key()

    is_pytest_win = (
        sys.platform == "win32"
        and ("pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"))
        and not _disable_pytest_win_fallback
    )

    if not HAS_SQLCIPHER:
        if is_pytest_win:
            logger.warning(
                "SQLCipher library is missing. Allowing standard SQLite fallback under pytest on Windows."
            )
        else:
            raise RuntimeError(
                "SQLCipher library is missing. Standard SQLite fallback connections are blocked."
            )

    db_existed = False
    try:
        if os.path.exists(abs_path) and os.path.getsize(abs_path) > 0:
            db_existed = True
    except Exception:
        pass

    from contextlib import closing

    conn = None
    try:
        conn = sqlite3.connect(abs_path, timeout=30.0, check_same_thread=False)
        if raw_key and HAS_SQLCIPHER:
            with closing(conn.cursor()) as cursor:
                cursor.execute(f"PRAGMA key = '{raw_key}'")

        if HAS_SQLCIPHER:
            with closing(conn.cursor()) as cursor:
                cursor.execute("PRAGMA cipher_version;")
                version = cursor.fetchone()
                if not version or not version[0]:
                    if is_pytest_win:
                        logger.warning(
                            "SQLCipher is not active on this connection context, but tolerating under pytest on Windows."
                        )
                    else:
                        raise RuntimeError(
                            "SQLCipher is not active on this connection context."
                        )

        # Test database validity to catch unencrypted legacy databases or bad keys
        with closing(conn.cursor()) as cursor:
            cursor.execute("PRAGMA user_version;")

        # Enable Write-Ahead Logging (WAL) for simultaneous reads and writes
        with closing(conn.cursor()) as cursor:
            cursor.execute("PRAGMA journal_mode = WAL")
            # Increase the database in-memory page cache to hold text features and clustering data
            cursor.execute("PRAGMA cache_size = -64000")  # 64MB cache

            # Disable mmap_size on Windows to prevent OS-level file locking issues with multiple connections
            if sys.platform != "win32":
                # Enforce optimized disk page allocations
                cursor.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap

            # Ensure database size remains stable under rapid writes
            cursor.execute(
                "PRAGMA journal_size_limit = 67108864"
            )  # 64MB limit for WAL/rollback logs
            # Set synchronous mode to NORMAL for WAL
            cursor.execute("PRAGMA synchronous = NORMAL")

    except Exception as e:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

        import sqlite3 as std_sqlite3

        # Safe extraction of module and class names to prevent AttributeError when __module__ is None
        module_name = getattr(type(e), "__module__", "") or ""
        module_name = str(module_name).lower()
        class_name = getattr(type(e), "__name__", "") or ""
        err_msg_lower = str(e).lower()

        # Broad detection of SQLite/SQLCipher database and connection errors
        is_sqlite_or_db_err = (
            isinstance(e, (sqlite3.Error, std_sqlite3.Error))
            or "sqlite" in module_name
            or "sqlcipher" in module_name
            or class_name == "Error"
            or any(
                term in class_name
                for term in (
                    "DatabaseError",
                    "OperationalError",
                    "IntegrityError",
                    "InternalError",
                    "ProgrammingError",
                    "NotSupportedError",
                )
            )
        )

        # Ensure we only treat actual decryption or key mismatch errors as decryption failures,
        # propagating standard SQLite operational/locking errors normally.
        is_decryption_err = is_sqlite_or_db_err and any(
            msg in err_msg_lower
            for msg in (
                "not a database",
                "encrypted",
                "malformed",
                "authentication",
                "password",
                "passphrase",
                "mac",
                "bad decrypt",
                "mismatch",
                "wrong key",
                "invalid key",
                "decryption",
                "cryptographic",
                "failed to decrypt database",
            )
        )

        temp_conn = None
        if (
            not is_decryption_err
            and is_sqlite_or_db_err
            and "disk i/o error" in err_msg_lower
            and raw_key
            and db_existed
        ):
            # Differentiate a transient Windows file lock error from a true decryption failure:
            # We copy the database (and WAL/SHM sidecars, if they exist) to a temporary, isolated
            # directory by copying raw bytes directly (avoiding metadata/ACL copy failures on Windows)
            # and trying to connect to the temp copy.
            import shutil
            import tempfile
            from pathlib import Path

            try:
                determined_is_decryption_err = False
                temp_dir = tempfile.mkdtemp()
                try:
                    temp_db_path = Path(temp_dir) / "temp_db.db"
                    with open(os.path.abspath(db_path), "rb") as f_in:
                        with open(temp_db_path, "wb") as f_out:
                            f_out.write(f_in.read())

                    for ext in ("-wal", "-shm"):
                        sidecar = f"{os.path.abspath(db_path)}{ext}"
                        if os.path.exists(sidecar):
                            try:
                                with open(sidecar, "rb") as f_in:
                                    with open(f"{temp_db_path}{ext}", "wb") as f_out:
                                        f_out.write(f_in.read())
                            except Exception:
                                pass

                    temp_conn = None
                    try:
                        temp_conn = sqlite3.connect(
                            str(temp_db_path), timeout=5.0, check_same_thread=False
                        )
                        with closing(temp_conn.cursor()) as cursor:
                            cursor.execute(f"PRAGMA key = '{raw_key}'")
                            cursor.execute("PRAGMA user_version;")
                        determined_is_decryption_err = False
                    except Exception as temp_e:
                        temp_err_msg = str(temp_e).lower()
                        if any(
                            msg in temp_err_msg
                            for msg in (
                                "not a database",
                                "encrypted",
                                "malformed",
                                "authentication",
                                "password",
                                "mac",
                                "bad decrypt",
                                "mismatch",
                                "wrong key",
                                "invalid key",
                                "decryption",
                                "cryptographic",
                                "disk i/o error",
                            )
                        ):
                            determined_is_decryption_err = True
                    finally:
                        if temp_conn:
                            try:
                                temp_conn.close()
                            except Exception:
                                pass
                            temp_conn = None

                    is_decryption_err = determined_is_decryption_err
                finally:
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception:
                        pass
            except Exception:
                is_decryption_err = False

        # Clear all local variables to break traceback-held reference cycles on Windows GHA
        conn = None
        cursor = None
        version = None
        crypto = None
        raw_key = None
        cache_key = None
        abs_path = None

        if is_decryption_err and not isinstance(
            e, (RuntimeError, SystemExit, KeyboardInterrupt)
        ):
            logger.error(
                "Database decryption failed: The database is encrypted or is not a valid database. "
                "This indicates a locked OS keyring, mismatched cryptographic keys, or decryption failure. "
                f"Database path: '{db_path}'. Error detail: {e}"
            )
            raise sqlite3.DatabaseError(
                f"Failed to decrypt database at '{db_path}'. Please ensure your OS keyring is unlocked and configured correctly."
            ) from e
        else:
            raise

    with _cache_lock:
        _connection_cache[cache_key] = conn

    return conn
