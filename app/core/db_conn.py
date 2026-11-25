"""Database connection module."""

import base64
import binascii
import contextlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from app.core.cache import BoundedMemoryCache

logger = logging.getLogger(__name__)


def escape_pragma_key(key_str: str) -> str:
    """Sanitize raw key string for PRAGMA queries by doubling single quotes."""
    if not key_str:
        return ""
    return str(key_str).replace("'", "''")


def _fernet_key_to_raw_hex(key_str: str) -> str | None:
    """Decode a canonical 32-byte URL-safe base64 Fernet key to 64 lowercase hex chars."""
    if not isinstance(key_str, str) or len(key_str) != 44:
        return None
    try:
        key_ascii = key_str.encode("ascii")
        raw_bytes = base64.urlsafe_b64decode(key_ascii)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return None
    if len(raw_bytes) != 32 or base64.urlsafe_b64encode(raw_bytes) != key_ascii:
        return None
    return raw_bytes.hex()


def _format_pragma_key(key_str: str) -> str:
    """Format a key string as a SQLCipher raw hex BLOB literal or escaped passphrase."""
    escaped = escape_pragma_key(key_str)
    raw_hex = _fernet_key_to_raw_hex(key_str)
    if raw_hex is not None:
        return f"\"x'{raw_hex}'\""
    return f"'{escaped}'"


# The frozen build leaves out the unencrypted stdlib driver on purpose
# (smart-autosorter.spec), so it may be missing.
try:
    import sqlite3 as _stdlib_sqlite3
except ImportError:
    _stdlib_sqlite3 = None

# sqlcipher3-wheels ships SQLCipher and OpenSSL statically linked into one
# extension module, so the import needs no DLL or library-path setup. Callers
# use this module's ``sqlite3`` handle; ``sys.modules["sqlite3"]`` is left
# alone, so third-party ``import sqlite3`` is unaffected. Without SQLCipher,
# opening a store raises (#711), before the handle is used.
sqlite3: Any
try:
    from sqlcipher3 import dbapi2 as sqlite3

    HAS_SQLCIPHER = True
except ImportError:
    sqlite3 = _stdlib_sqlite3
    HAS_SQLCIPHER = False

# Database errors from either driver; sqlcipher3's classes don't subclass sqlite3's.
DB_ERRORS: tuple[type[Exception], ...] = tuple(
    dict.fromkeys(m.Error for m in (sqlite3, _stdlib_sqlite3) if m is not None)
)

_SUPPRESSED_CONN_ERRORS: tuple[type[Exception], ...] = (
    *DB_ERRORS,
    OSError,
    RuntimeError,
    ValueError,
    AttributeError,
)

_DECRYPTION_ERR_SUBSTRINGS = (
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


def _is_sqlite_or_db_error(err: BaseException) -> bool:
    module_name = str(getattr(type(err), "__module__", "") or "").lower()
    class_name = str(getattr(type(err), "__name__", "") or "")
    return (
        isinstance(err, DB_ERRORS)
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


def _is_decryption_exception(err: BaseException) -> bool:
    return _is_sqlite_or_db_error(err) and any(
        msg in str(err).lower() for msg in _DECRYPTION_ERR_SUBSTRINGS
    )


def _is_db_file_writable(abs_path: str) -> bool:
    try:
        if (os.stat(abs_path).st_mode & 0o222) == 0:
            return False
    except OSError:
        return False
    parent = os.path.dirname(abs_path) or "."
    return os.access(abs_path, os.W_OK) and os.access(parent, os.W_OK)


# Global connection cache and lock
def _close_evicted_connection(key, conn):
    for rk in [k for k, v in _raw_key_to_abs.items() if (v, k[1]) == key]:
        _raw_key_to_abs.pop(rk, None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass


_connection_cache = BoundedMemoryCache(max_size=50, on_evict=_close_evicted_connection)
_raw_key_to_abs: dict[tuple[str, int], str] = {}
_cache_lock = threading.RLock()
_path_init_locks = {}
_path_locks_lock = threading.Lock()
_opening_paths_lock = threading.Lock()
_opening_db_paths: set[str] = set()
_disable_pytest_win_fallback = True


def _is_db_opening(db_path: str | Path) -> bool:
    norm = os.path.normcase(str(Path(os.path.abspath(os.fspath(db_path)))))
    with _opening_paths_lock:
        return norm in _opening_db_paths


def _get_path_init_lock(abs_path: str):
    with _path_locks_lock:
        if abs_path not in _path_init_locks:
            _path_init_locks[abs_path] = threading.RLock()
        return _path_init_locks[abs_path]


def clear_connection_cache(only_current_and_inactive: bool = True):
    """Clear cached database connections, selectively or globally."""
    global _connection_cache
    import gc

    from app.core.crypto import (
        _clear_session_crypto_cache,
        _reset_keyring_probe_cache,
    )

    should_collect = not only_current_and_inactive
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
                    should_collect = True
                    try:
                        conn.close()
                    except Exception:
                        pass
            for rk in [
                k
                for k in _raw_key_to_abs
                if k[1] == calling_thread_id or k[1] not in active_thread_ids
            ]:
                _raw_key_to_abs.pop(rk, None)
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
            _raw_key_to_abs.clear()
            with _path_locks_lock:
                _path_init_locks.clear()
            _clear_session_crypto_cache()
            _reset_keyring_probe_cache()
    if should_collect:
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
        for rk in [k for k in _raw_key_to_abs if k[1] not in active_thread_ids]:
            _raw_key_to_abs.pop(rk, None)


def get_db_connection(db_path: str):
    """Create and configure a new database connection with performance parameters."""
    global _connection_cache
    raw_path = os.fspath(db_path)
    thread_id = threading.get_ident()
    is_abs_raw = isinstance(raw_path, str) and os.path.isabs(raw_path)

    if is_abs_raw:
        with _cache_lock:
            cached_abs = _raw_key_to_abs.get((raw_path, thread_id))
            if cached_abs is not None and (cached_abs, thread_id) in _connection_cache:
                return _connection_cache[(cached_abs, thread_id)]

    abs_path = str(Path(db_path).resolve())
    cache_key = (abs_path, thread_id)

    with _cache_lock:
        if cache_key in _connection_cache:
            if is_abs_raw:
                _raw_key_to_abs[(raw_path, thread_id)] = abs_path
            return _connection_cache[cache_key]

        # Automatically clean up connections for dead threads to prevent resource and lock leaks on Windows
        clear_dead_thread_connections()

    # Open each database path one thread at a time. Opening runs PRAGMA
    # journal_mode = WAL, which needs an exclusive lock while a fresh database
    # converts; threads racing through it fail with "database is locked" on
    # Windows. Cache hits never take this lock.
    with _get_path_init_lock(abs_path):
        with _cache_lock:
            if cache_key in _connection_cache:
                if is_abs_raw:
                    _raw_key_to_abs[(raw_path, thread_id)] = abs_path
                return _connection_cache[cache_key]
        conn = _open_connection(db_path, abs_path, cache_key)
        if is_abs_raw:
            with _cache_lock:
                _raw_key_to_abs[(raw_path, thread_id)] = abs_path
        return conn


def _silence_cipher_logs(cursor) -> None:
    if HAS_SQLCIPHER:
        with contextlib.suppress(*_SUPPRESSED_CONN_ERRORS):
            cursor.execute("PRAGMA cipher_log_level = NONE")


def _probe_or_fallback_sqlcipher(
    conn,
    abs_path: str,
    raw_key: str | None,
    *,
    db_existed: bool,
    rekey_writable: bool,
    timeout: float = 30.0,
    probe_sql: str = "PRAGMA user_version;",
):
    """Probe ``conn`` and fall back to 256,000-iteration PBKDF2 for legacy passphrase files."""
    from contextlib import closing

    assert sqlite3 is not None
    raw_hex = _fernet_key_to_raw_hex(raw_key) if raw_key else None
    try:
        with closing(conn.cursor()) as cursor:
            cursor.execute(probe_sql)
        return conn
    except DB_ERRORS as probe_err:
        if not (
            HAS_SQLCIPHER
            and db_existed
            and raw_key
            and raw_hex is not None
            and _is_decryption_exception(probe_err)
        ):
            raise
        with contextlib.suppress(*_SUPPRESSED_CONN_ERRORS):
            conn.close()
        retry_conn = sqlite3.connect(abs_path, timeout=timeout, check_same_thread=False)
        try:
            with closing(retry_conn.cursor()) as cursor:
                _silence_cipher_logs(cursor)
                escaped_key = escape_pragma_key(raw_key)
                cursor.execute(f"PRAGMA key = '{escaped_key}'")
                cursor.execute("PRAGMA kdf_iter = 256000")
                cursor.execute(probe_sql)
                if rekey_writable and _is_db_file_writable(abs_path):
                    with _get_path_init_lock(abs_path):
                        try:
                            cursor.execute("PRAGMA journal_mode = DELETE")
                            cursor.execute(f"PRAGMA rekey = \"x'{raw_hex}'\"")
                        except (*DB_ERRORS, OSError) as rekey_err:
                            logger.debug(
                                f"Skipping raw-hex rekey on '{abs_path}': {rekey_err}"
                            )
            return retry_conn
        except BaseException:
            with contextlib.suppress(*_SUPPRESSED_CONN_ERRORS):
                retry_conn.close()
            raise


def _rekey_sqlcipher_database(
    db_path: str | Path, current_raw: str, new_raw: str
) -> None:
    """Serialize and rekey a SQLCipher database from ``current_raw`` to ``new_raw``."""
    from contextlib import closing

    if not HAS_SQLCIPHER:
        return
    assert sqlite3 is not None
    abs_db = str(Path(db_path).resolve())
    current_pragma = _format_pragma_key(current_raw)
    new_pragma = _format_pragma_key(new_raw)

    with _get_path_init_lock(abs_db):
        clear_connection_cache(only_current_and_inactive=False)
        conn = sqlite3.connect(abs_db, timeout=30.0, check_same_thread=False)
        try:
            with closing(conn.cursor()) as cursor:
                _silence_cipher_logs(cursor)
                cursor.execute(f"PRAGMA key = {current_pragma}")
            conn = _probe_or_fallback_sqlcipher(
                conn,
                abs_db,
                current_raw,
                db_existed=True,
                rekey_writable=False,
                probe_sql="SELECT count(*) FROM sqlite_master",
            )
            with closing(conn.cursor()) as cursor:
                cursor.execute("PRAGMA journal_mode = DELETE")
                cursor.execute(f"PRAGMA rekey = {new_pragma}")
                cursor.execute("PRAGMA journal_mode = WAL")
        finally:
            with contextlib.suppress(*_SUPPRESSED_CONN_ERRORS):
                conn.close()
        clear_connection_cache(only_current_and_inactive=False)


def _open_connection(db_path: str, abs_path: str, cache_key):
    """Open, key and configure a connection, then cache it under cache_key."""
    norm_abs = os.path.normcase(abs_path)
    with _opening_paths_lock:
        _opening_db_paths.add(norm_abs)
    try:
        return _open_connection_unlocked(db_path, abs_path, cache_key)
    finally:
        with _opening_paths_lock:
            _opening_db_paths.discard(norm_abs)


def _open_connection_unlocked(db_path: str, abs_path: str, cache_key):
    from app.core.path_utils import resolve_db_crypto

    crypto = resolve_db_crypto(db_path)
    raw_key = crypto.get_raw_key()

    if not HAS_SQLCIPHER:
        if _disable_pytest_win_fallback:
            raise RuntimeError(
                "SQLCipher library is missing, so the encrypted store can't be opened. "
                "Reinstall Sortify, or run `pip install sqlcipher3-wheels`."
            )
        logger.warning(
            "SECURITY WARNING: SQLCipher encryption driver is missing or unverified. "
            "Operating in standard SQLite fallback connection mode. "
            "Database page-level encryption is inactive."
        )

    db_existed = False
    try:
        if os.path.exists(abs_path) and os.path.getsize(abs_path) > 0:
            db_existed = True
    except Exception:
        pass

    from contextlib import closing

    conn = None
    assert sqlite3 is not None
    try:
        conn = sqlite3.connect(abs_path, timeout=30.0, check_same_thread=False)
        if HAS_SQLCIPHER:
            with closing(conn.cursor()) as cursor:
                _silence_cipher_logs(cursor)
            if raw_key:
                try:
                    with closing(conn.cursor()) as cursor:
                        pragma_val = _format_pragma_key(raw_key)
                        cursor.execute(f"PRAGMA key = {pragma_val}")
                except Exception as pragma_err:
                    logger.warning(
                        f"PRAGMA key configuration encountered error in fallback context: {pragma_err}"
                    )

        if HAS_SQLCIPHER:
            try:
                with closing(conn.cursor()) as cursor:
                    cursor.execute("PRAGMA cipher_version;")
                    version = cursor.fetchone()
                    if not version or not version[0]:
                        if _disable_pytest_win_fallback:
                            raise RuntimeError(
                                "SQLCipher is not active on this connection context."
                            )
                        logger.warning(
                            "SECURITY WARNING: SQLCipher is not active on this connection context. "
                            "Operating in standard SQLite fallback mode."
                        )
            except Exception as cipher_err:
                if _disable_pytest_win_fallback:
                    raise RuntimeError(
                        "SQLCipher is not active on this connection context."
                    ) from cipher_err
                logger.warning(
                    f"SECURITY WARNING: PRAGMA cipher_version check failed ({cipher_err}). "
                    "Operating in standard SQLite fallback mode."
                )

        # Test database validity to catch legacy passphrase files, unencrypted DBs, or bad keys
        conn = _probe_or_fallback_sqlcipher(
            conn,
            abs_path,
            raw_key,
            db_existed=db_existed,
            rekey_writable=True,
        )

        # Enable Write-Ahead Logging (WAL) for simultaneous reads and writes
        writable = not db_existed or _is_db_file_writable(abs_path)
        with closing(conn.cursor()) as cursor:
            try:
                cursor.execute("PRAGMA journal_mode = WAL")
            except DB_ERRORS as wal_err:
                if "readonly" not in str(wal_err).lower() and writable:
                    raise
            # Increase the database in-memory page cache to hold text features and clustering data
            cursor.execute("PRAGMA cache_size = -64000")  # 64MB cache

            # Disable mmap_size on Windows to prevent OS-level file locking issues with multiple connections
            if sys.platform != "win32":
                # Enforce optimized disk page allocations
                cursor.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap

            with contextlib.suppress(*DB_ERRORS):
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

        err_msg_lower = str(e).lower()
        is_sqlite_or_db_err = _is_sqlite_or_db_error(e)
        is_decryption_err = _is_decryption_exception(e)

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
            import tempfile

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
                    assert sqlite3 is not None
                    try:
                        temp_conn = sqlite3.connect(
                            str(temp_db_path), timeout=5.0, check_same_thread=False
                        )
                        with closing(temp_conn.cursor()) as cursor:
                            _silence_cipher_logs(cursor)
                            pragma_val = _format_pragma_key(raw_key)
                            cursor.execute(f"PRAGMA key = {pragma_val}")
                        temp_conn = _probe_or_fallback_sqlcipher(
                            temp_conn,
                            str(temp_db_path),
                            raw_key,
                            db_existed=True,
                            rekey_writable=False,
                            timeout=5.0,
                        )
                        determined_is_decryption_err = False
                    except Exception as temp_e:
                        temp_err_msg = str(temp_e).lower()
                        if _is_decryption_exception(temp_e) or (
                            "disk i/o error" in temp_err_msg
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
                        from app.core.resilient_file_ops import resilient_rmtree

                        resilient_rmtree(temp_dir, ignore_errors=True)
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
        abs_path = ""

        if is_decryption_err and not isinstance(
            e, (RuntimeError, SystemExit, KeyboardInterrupt)
        ):
            logger.error(
                "Database decryption failed: The database is encrypted or is not a valid database. "
                "This indicates a locked OS keyring, mismatched cryptographic keys, or decryption failure. "
                f"Database path: '{db_path}'. Error detail: {e}"
            )
            assert sqlite3 is not None
            raise sqlite3.DatabaseError(
                f"Failed to decrypt database at '{db_path}'. Please ensure your OS keyring is unlocked and configured correctly."
            ) from e
        else:
            raise

    with _cache_lock:
        if cache_key in _connection_cache:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            return _connection_cache[cache_key]
        _connection_cache[cache_key] = conn
        return conn
