"""Unit tests for database connection module and PRAGMA key single quote escaping."""

from unittest.mock import patch

from app.core.db_conn import (
    clear_connection_cache,
    escape_pragma_key,
    get_db_connection,
)


def test_escape_pragma_key_unit():
    """Verify that escape_pragma_key correctly escapes single quotes in key strings."""
    assert escape_pragma_key("simple_key_123") == "simple_key_123"
    assert escape_pragma_key("key'with'single'quotes") == "key''with''single''quotes"
    assert escape_pragma_key("key''double''quotes") == "key''''double''''quotes"
    assert escape_pragma_key("o'reilly's_key") == "o''reilly''s_key"
    assert escape_pragma_key("") == ""
    assert escape_pragma_key(None) == ""


def test_db_conn_with_single_quote_key(tmp_path):
    """Verify that get_db_connection succeeds when the key contains single quotes."""
    db_file = tmp_path / "test_quote_key.db"
    key_file = tmp_path / "test_quote_key.key"

    key_with_quotes = "secret'key'with'single'quotes"
    key_file.write_text(key_with_quotes, encoding="utf-8")

    from app.core.crypto import SessionCrypto

    crypto = SessionCrypto(key_file, db_file)

    with patch("app.core.path_utils.resolve_db_crypto", return_value=crypto):
        clear_connection_cache(only_current_and_inactive=False)
        conn = get_db_connection(str(db_file))
        assert conn is not None

        cursor = conn.cursor()
        cursor.execute("CREATE TABLE test_quote_data (id INT, val TEXT);")
        cursor.execute("INSERT INTO test_quote_data VALUES (1, 'hello');")
        conn.commit()

        cursor.execute("SELECT val FROM test_quote_data WHERE id = 1;")
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "hello"

        clear_connection_cache(only_current_and_inactive=False)


def test_concurrent_db_conn_access(tmp_path):
    """Verify that get_db_connection handles concurrent cache-miss initialization safely."""
    import concurrent.futures

    db_file = tmp_path / "test_concurrent.db"
    key_file = tmp_path / "test_concurrent.key"
    key_file.write_text("concurrent_secret_key_123", encoding="utf-8")

    from app.core.crypto import SessionCrypto

    crypto = SessionCrypto(key_file, db_file)

    with patch("app.core.path_utils.resolve_db_crypto", return_value=crypto):
        clear_connection_cache(only_current_and_inactive=False)

        def worker():
            conn = get_db_connection(str(db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT 1;")
            return cursor.fetchone()[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker) for _ in range(10)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(results) == 10
        assert all(r == 1 for r in results)
        clear_connection_cache(only_current_and_inactive=False)


def test_concurrent_first_open_of_one_path_is_serialized(tmp_path):
    """Threads opening the same new database never run its setup at the same time.

    Setup switches the database to WAL, which needs an exclusive lock; on Windows
    two threads doing it at once fail with "database is locked" (#741).
    """
    import concurrent.futures
    import threading
    import time

    from app.core import db_conn
    from app.core.crypto import SessionCrypto

    db_file = tmp_path / "serialized.db"
    key_file = tmp_path / "serialized.key"
    key_file.write_text("serialized_secret_key_123", encoding="utf-8")
    crypto = SessionCrypto(key_file, db_file)

    real_connect = db_conn.sqlite3.connect
    active = 0
    peak = 0
    counter_lock = threading.Lock()

    def tracking_connect(*args, **kwargs):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            return real_connect(*args, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    with (
        patch("app.core.path_utils.resolve_db_crypto", return_value=crypto),
        patch.object(db_conn.sqlite3, "connect", side_effect=tracking_connect),
    ):
        clear_connection_cache(only_current_and_inactive=False)
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            return get_db_connection(str(db_file)).execute("SELECT 1").fetchone()[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: worker(), range(8)))

        clear_connection_cache(only_current_and_inactive=False)

    assert results == [1] * 8
    assert peak == 1


def _fail_first_connect_with_disk_io_error(db_conn, real_connect, db_file):
    """Make the first connect to db_file raise "disk I/O error"; others work."""
    target = str(db_file.resolve())
    failed = False

    def connect(path, *args, **kwargs):
        nonlocal failed
        if not failed and str(path) == target:
            failed = True
            raise db_conn.sqlite3.OperationalError("disk I/O error")
        return real_connect(path, *args, **kwargs)

    return connect


def _make_encrypted_db(tmp_path, key: str):
    from app.core.crypto import SessionCrypto

    db_file = tmp_path / "locked.db"
    key_file = tmp_path / "locked.key"
    key_file.write_text(key, encoding="utf-8")
    crypto = SessionCrypto(key_file, db_file)
    with patch("app.core.path_utils.resolve_db_crypto", return_value=crypto):
        clear_connection_cache(only_current_and_inactive=False)
        conn = get_db_connection(str(db_file))
        conn.execute("CREATE TABLE t (id INT)")
        conn.commit()
        clear_connection_cache(only_current_and_inactive=False)
    return db_file


def test_disk_io_error_with_the_right_key_is_not_reported_as_decryption(tmp_path):
    """A disk I/O error on a database the key still opens is a lock, not a bad key.

    The open retries against a private copy; when that copy decrypts, the
    original I/O error is raised unchanged so callers can retry.
    """
    import pytest

    from app.core import db_conn
    from app.core.crypto import SessionCrypto

    if not db_conn.HAS_SQLCIPHER:
        pytest.skip("needs SQLCipher")
    db_file = _make_encrypted_db(tmp_path, "right_key_123")
    crypto = SessionCrypto(tmp_path / "locked.key", db_file)

    with (
        patch("app.core.path_utils.resolve_db_crypto", return_value=crypto),
        patch.object(
            db_conn.sqlite3,
            "connect",
            side_effect=_fail_first_connect_with_disk_io_error(
                db_conn, db_conn.sqlite3.connect, db_file
            ),
        ),
    ):
        clear_connection_cache(only_current_and_inactive=False)
        with pytest.raises(db_conn.sqlite3.OperationalError, match="disk I/O error"):
            get_db_connection(str(db_file))
        clear_connection_cache(only_current_and_inactive=False)


def test_disk_io_error_with_the_wrong_key_is_reported_as_decryption(tmp_path):
    """A disk I/O error on a database the key cannot open is a decryption failure."""
    import pytest

    from app.core import db_conn
    from app.core.crypto import SessionCrypto

    if not db_conn.HAS_SQLCIPHER:
        pytest.skip("needs SQLCipher")
    db_file = _make_encrypted_db(tmp_path, "right_key_123")
    crypto = SessionCrypto(tmp_path / "locked.key", db_file)

    with (
        patch("app.core.path_utils.resolve_db_crypto", return_value=crypto),
        patch.object(crypto, "get_raw_key", return_value="wrong_key_456"),
        patch.object(
            db_conn.sqlite3,
            "connect",
            side_effect=_fail_first_connect_with_disk_io_error(
                db_conn, db_conn.sqlite3.connect, db_file
            ),
        ),
    ):
        clear_connection_cache(only_current_and_inactive=False)
        with pytest.raises(db_conn.sqlite3.DatabaseError, match="Failed to decrypt"):
            get_db_connection(str(db_file))
        clear_connection_cache(only_current_and_inactive=False)
