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

