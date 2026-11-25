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


def test_fernet_key_to_raw_hex_and_format_pragma_key():
    """Canonical 32-byte Fernet keys format as raw hex BLOB literals; non-Fernet strings escape quotes."""
    import base64

    from cryptography.fernet import Fernet

    from app.core.db_conn import _fernet_key_to_raw_hex, _format_pragma_key

    fernet_val = Fernet.generate_key().decode("ascii")
    expected_hex = base64.urlsafe_b64decode(fernet_val.encode("ascii")).hex()
    assert _fernet_key_to_raw_hex(fernet_val) == expected_hex
    assert len(expected_hex) == 64
    assert _format_pragma_key(fernet_val) == f"\"x'{expected_hex}'\""

    # Non-Fernet strings (wrong length, non-canonical base64, quotes, non-ASCII) stay passphrases
    assert _fernet_key_to_raw_hex("short_val") is None
    assert _fernet_key_to_raw_hex("a" * 44) is None
    quoted_sample = "alpha'beta'" * 4
    assert len(quoted_sample) == 44
    assert _fernet_key_to_raw_hex(quoted_sample) is None
    assert _format_pragma_key(quoted_sample) == f"'{escape_pragma_key(quoted_sample)}'"
    assert _fernet_key_to_raw_hex("é" * 44) is None


def test_raw_hex_open_latency_and_zero_c_stderr(tmp_path):
    """Fresh SQLCipher databases open via raw hex without C stderr warnings and under PBKDF2 latency."""
    import contextlib
    import os
    import statistics
    import sys
    import time

    from app.core import db_conn
    from app.core.path_utils import resolve_db_crypto

    if not db_conn.HAS_SQLCIPHER:
        return

    stderr_capture = tmp_path / "c_stderr.log"
    db_paths = [tmp_path / f"fast_{i}.db" for i in range(9)]
    for p in db_paths:
        resolve_db_crypto(p).get_cipher()

    legacy_db = tmp_path / "legacy_probe.db"
    legacy_crypto = resolve_db_crypto(legacy_db)
    legacy_crypto.get_cipher()
    legacy_raw = legacy_crypto.get_raw_key()
    assert legacy_raw is not None
    with contextlib.closing(
        db_conn.sqlite3.connect(str(legacy_db), check_same_thread=False)
    ) as raw_conn:
        raw_conn.execute(f"PRAGMA key = '{escape_pragma_key(legacy_raw)}'")
        raw_conn.execute("PRAGMA kdf_iter = 256000")
        raw_conn.execute("CREATE TABLE t (v TEXT)")
        raw_conn.execute("INSERT INTO t VALUES ('ok')")
        raw_conn.commit()

    clear_connection_cache(only_current_and_inactive=False)
    for p in db_paths:
        c = resolve_db_crypto(p)
        c.get_cipher()
        c.get_raw_key()
    resolve_db_crypto(legacy_db).get_raw_key()

    # Create encrypted SQLCipher files, then clear connection cache while retaining SessionCrypto
    for p in db_paths:
        conn = get_db_connection(str(p))
        with conn:
            conn.execute("CREATE TABLE t (v INT)")
    clear_connection_cache(only_current_and_inactive=True)

    saved_stderr = os.dup(2)
    timings_ms: list[float] = []
    try:
        with open(stderr_capture, "wb") as err_fh:
            os.dup2(err_fh.fileno(), 2)
            for p in db_paths:
                t0 = time.perf_counter()
                conn = get_db_connection(str(p))
                timings_ms.append((time.perf_counter() - t0) * 1000.0)
                assert conn.execute("SELECT 1").fetchone() == (1,)
            upgraded = get_db_connection(str(legacy_db))
            assert upgraded.execute("SELECT v FROM t").fetchone() == ("ok",)
            err_fh.flush()
    finally:
        os.dup2(saved_stderr, 2)
        os.close(saved_stderr)
        clear_connection_cache(only_current_and_inactive=False)

    assert stderr_capture.read_bytes() == b""
    median_ms = statistics.median(timings_ms)
    # Raw hex skips 256,000 PBKDF2 iterations (~430ms); allow small headroom if branch coverage tracing is active.
    limit_ms = (
        15.0 if os.environ.get("COV_CORE_SOURCE") or sys.gettrace() is not None else 2.0
    )
    assert median_ms < limit_ms, (
        f"Median raw-hex open took {median_ms:.2f}ms (timings={timings_ms})"
    )


def test_legacy_passphrase_databases_upgrade_to_raw_hex_and_preserve_rows(tmp_path):
    """Legacy passphrase-encrypted databases open, rekey to raw hex, restore WAL, and retain rows."""
    import contextlib

    from app.core import db_conn
    from app.core.db_conn import _fernet_key_to_raw_hex
    from app.core.path_utils import resolve_db_crypto

    if not db_conn.HAS_SQLCIPHER:
        return

    filenames = ("sortify.db", "transaction_ledger.db", "autosorter.db", "history.db")
    for name in filenames:
        sub = tmp_path / name.replace(".", "_")
        sub.mkdir()
        db_file = sub / name
        crypto = resolve_db_crypto(db_file)
        crypto.get_cipher()
        raw_val = crypto.get_raw_key()
        assert raw_val is not None
        raw_hex = _fernet_key_to_raw_hex(raw_val)
        assert raw_hex is not None

        with contextlib.closing(
            db_conn.sqlite3.connect(str(db_file), check_same_thread=False)
        ) as legacy_conn:
            legacy_conn.execute(f"PRAGMA key = '{escape_pragma_key(raw_val)}'")
            legacy_conn.execute("PRAGMA kdf_iter = 256000")
            legacy_conn.execute("PRAGMA journal_mode = WAL")
            legacy_conn.execute(
                "CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)"
            )
            legacy_conn.execute(
                "INSERT INTO items (label) VALUES (?), (?)",
                (f"{name}-row-1", f"{name}-row-2"),
            )
            legacy_conn.commit()

        conn = get_db_connection(str(db_file))
        rows = conn.execute("SELECT label FROM items ORDER BY id").fetchall()
        assert rows == [(f"{name}-row-1",), (f"{name}-row-2",)]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        clear_connection_cache(only_current_and_inactive=False)

        # Verify on-disk database now opens directly via raw hex key without PBKDF2 fallback
        with contextlib.closing(
            db_conn.sqlite3.connect(str(db_file), check_same_thread=False)
        ) as verify_conn:
            verify_conn.execute(f"PRAGMA key = \"x'{raw_hex}'\"")
            assert verify_conn.execute(
                "SELECT label FROM items ORDER BY id"
            ).fetchall() == [
                (f"{name}-row-1",),
                (f"{name}-row-2",),
            ]


def test_readonly_legacy_and_raw_hex_databases_stay_readable(tmp_path):
    """Read-only legacy passphrase and raw-hex databases remain readable without rekey/WAL errors."""
    import contextlib

    from app.core import db_conn
    from app.core.path_utils import resolve_db_crypto

    if not db_conn.HAS_SQLCIPHER:
        return

    legacy_ro = tmp_path / "readonly_legacy.db"
    crypto_legacy = resolve_db_crypto(legacy_ro)
    crypto_legacy.get_cipher()
    legacy_raw = crypto_legacy.get_raw_key()
    assert legacy_raw is not None
    with contextlib.closing(
        db_conn.sqlite3.connect(str(legacy_ro), check_same_thread=False)
    ) as conn_setup:
        conn_setup.execute(f"PRAGMA key = '{escape_pragma_key(legacy_raw)}'")
        conn_setup.execute("PRAGMA kdf_iter = 256000")
        conn_setup.execute("CREATE TABLE notes (body TEXT)")
        conn_setup.execute("INSERT INTO notes VALUES ('legacy-readonly')")
        conn_setup.commit()

    hex_ro = tmp_path / "readonly_hex.db"
    conn_hex = get_db_connection(str(hex_ro))
    with conn_hex:
        conn_hex.execute("CREATE TABLE notes (body TEXT)")
        conn_hex.execute("INSERT INTO notes VALUES ('hex-readonly')")
    conn_hex.execute("PRAGMA journal_mode = DELETE")
    clear_connection_cache(only_current_and_inactive=False)

    legacy_ro.chmod(0o444)
    hex_ro.chmod(0o444)
    try:
        c1 = get_db_connection(str(legacy_ro))
        assert c1.execute("SELECT body FROM notes").fetchone() == ("legacy-readonly",)
        c2 = get_db_connection(str(hex_ro))
        assert c2.execute("SELECT body FROM notes").fetchone() == ("hex-readonly",)
    finally:
        clear_connection_cache(only_current_and_inactive=False)
        legacy_ro.chmod(0o600)
        hex_ro.chmod(0o600)


def test_rotate_key_supports_legacy_passphrase_db_and_custom_passphrase(tmp_path):
    """SessionCrypto.rotate_key upgrades legacy passphrase DBs and preserves custom non-Fernet passphrases."""
    import contextlib

    from app.core import db_conn
    from app.core.db_conn import _fernet_key_to_raw_hex
    from app.core.path_utils import resolve_db_crypto

    if not db_conn.HAS_SQLCIPHER:
        return

    db_file = tmp_path / "rotate_target.db"
    crypto = resolve_db_crypto(db_file)
    crypto.get_cipher()
    initial_val = crypto.get_raw_key()
    assert initial_val is not None

    with contextlib.closing(
        db_conn.sqlite3.connect(str(db_file), check_same_thread=False)
    ) as setup_conn:
        setup_conn.execute(f"PRAGMA key = '{escape_pragma_key(initial_val)}'")
        setup_conn.execute("PRAGMA kdf_iter = 256000")
        setup_conn.execute("PRAGMA journal_mode = WAL")
        setup_conn.execute("CREATE TABLE items (val TEXT)")
        setup_conn.execute("INSERT INTO items VALUES ('kept-across-rotations')")
        setup_conn.commit()

    custom_phrase = "'".join(("custom", "non-fernet", "phrase!2026"))
    rotated_custom = crypto.rotate_key(custom_phrase)
    assert rotated_custom == custom_phrase

    conn_custom = get_db_connection(str(db_file))
    assert conn_custom.execute("SELECT val FROM items").fetchone() == (
        "kept-across-rotations",
    )
    clear_connection_cache(only_current_and_inactive=False)

    crypto_after = resolve_db_crypto(db_file)
    rotated_fernet = crypto_after.rotate_key()
    raw_hex = _fernet_key_to_raw_hex(rotated_fernet)
    assert raw_hex is not None

    with contextlib.closing(
        db_conn.sqlite3.connect(str(db_file), check_same_thread=False)
    ) as verify_conn:
        verify_conn.execute(f"PRAGMA key = \"x'{raw_hex}'\"")
        assert verify_conn.execute("SELECT val FROM items").fetchone() == (
            "kept-across-rotations",
        )


def test_windows_disk_io_temp_copy_probe_handles_raw_hex_and_bad_key(
    tmp_path, monkeypatch
):
    """Windows disk i/o error probe opens temp copies with raw hex (not raising decryption error) and flags bad keys."""
    import pytest

    from app.core import db_conn
    from app.core.path_utils import resolve_db_crypto

    if not db_conn.HAS_SQLCIPHER:
        return

    db_file = tmp_path / "win_lock_probe.db"
    conn = get_db_connection(str(db_file))
    with conn:
        conn.execute("CREATE TABLE t (x INT)")
        conn.execute("INSERT INTO t VALUES (7)")
    clear_connection_cache(only_current_and_inactive=False)

    real_probe = db_conn._probe_or_fallback_sqlcipher
    target_abs = str(db_file.resolve())

    def _fail_primary_with_disk_io(c, path_arg, *args, **kwargs):
        if path_arg == target_abs:
            c.close()
            raise db_conn.sqlite3.OperationalError("disk I/O error")
        return real_probe(c, path_arg, *args, **kwargs)

    monkeypatch.setattr(
        db_conn, "_probe_or_fallback_sqlcipher", _fail_primary_with_disk_io
    )
    with pytest.raises(db_conn.sqlite3.OperationalError, match="disk I/O error"):
        get_db_connection(str(db_file))

    # Now replace stored key with a different Fernet key so the temp-copy probe fails decryption
    from cryptography.fernet import Fernet

    wrong_crypto = resolve_db_crypto(tmp_path / "other.db")
    wrong_crypto._key = Fernet.generate_key()
    monkeypatch.setattr(
        "app.core.path_utils.resolve_db_crypto", lambda _p: wrong_crypto
    )
    with pytest.raises(
        db_conn.sqlite3.DatabaseError, match="Failed to decrypt database"
    ):
        get_db_connection(str(db_file))
