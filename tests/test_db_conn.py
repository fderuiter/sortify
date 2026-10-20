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


def test_import_leaves_stdlib_sqlite3_alone():
    import subprocess
    import sys

    code = (
        "import sqlite3, sys, app.core.db_conn as d; "
        "assert sys.modules['sqlite3'] is sqlite3; "
        "assert sqlite3.__name__ == 'sqlite3'; "
        "assert sqlite3.Error in d.DB_ERRORS"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_import_works_without_stdlib_sqlite3():
    # The frozen build excludes the stdlib driver (smart-autosorter.spec).
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['sqlite3'] = None; "
        "import app.core.db_conn as d; "
        "assert d.HAS_SQLCIPHER == (d.sqlite3 is not None); "
        "assert all(isinstance(e, type) for e in d.DB_ERRORS)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
