import os
import shutil
import sqlite3
from contextlib import closing
from unittest.mock import MagicMock

import keyring
import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SessionCrypto


def test_key_generation_keyring(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"

    crypto = SessionCrypto(key_path, db_path)

    # Trigger key generation
    cipher = crypto.get_cipher()
    assert cipher is not None

    assert not key_path.exists()
    assert not crypto.isolated_key_path.exists()

    # Check keyring
    key = keyring.get_password(crypto.keyring_service, crypto.keyring_account)
    assert key is not None


def test_key_generation_fallback(tmp_path, monkeypatch):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    # Force keyring failure
    def mock_set_password(*args, **kwargs):
        raise Exception("Keyring unavailable")

    monkeypatch.setattr(keyring, "set_password", mock_set_password)

    # Trigger key generation
    cipher = crypto.get_cipher()
    assert cipher is not None

    assert not key_path.exists()
    assert crypto.isolated_key_path.exists()

    # Check strict permissions on isolated dir and key file
    if os.name != "nt":
        dir_stat = os.stat(crypto.isolated_dir)
        assert (dir_stat.st_mode & 0o777) == 0o700

        file_stat = os.stat(crypto.isolated_key_path)
        assert (file_stat.st_mode & 0o777) == 0o600


def test_legacy_key_migration(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    legacy_key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(legacy_key)

    cipher = crypto.get_cipher()
    assert cipher is not None

    # Should NOT be deleted immediately
    assert key_path.exists()

    # Should be copied to isolated fallback key path
    assert crypto.isolated_key_path.exists()
    with open(crypto.isolated_key_path, "rb") as f:
        copied_key = f.read()
    assert copied_key == legacy_key

    # Should be in keyring
    key = keyring.get_password(crypto.keyring_service, crypto.keyring_account)
    assert key == legacy_key.decode("utf-8")


def test_missing_key_with_existing_db(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"

    # Create fake DB with documents table and some data
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO documents (id) VALUES (1)")

    crypto = SessionCrypto(key_path, db_path)

    # Attempting to get cipher should now fail because key is missing but DB has data
    with pytest.raises(
        RuntimeError, match="Database accessed but key file is missing."
    ):
        crypto.get_cipher()


def test_missing_key_with_empty_db(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"

    # Create fake DB with NO data
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")

    crypto = SessionCrypto(key_path, db_path)

    # Should automatically generate key without error
    cipher = crypto.get_cipher()
    assert cipher is not None
    assert not key_path.exists()
    assert (
        keyring.get_password(crypto.keyring_service, crypto.keyring_account) is not None
    )


def test_encryption_decryption(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    original_text = "This is a sensitive document."
    enc_text = crypto.encrypt_text(original_text)
    assert enc_text != original_text.encode("utf-8")
    assert crypto.decrypt_text(enc_text) == original_text


def test_invalid_key(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    # Put invalid key in keyring directly to test invalid key behavior
    keyring.set_password(
        crypto.keyring_service,
        crypto.keyring_account,
        "invalid_key_data_that_is_too_short",
    )

    with pytest.raises(
        RuntimeError, match="Database accessed but key file is missing or invalid."
    ):
        crypto.get_cipher()


def test_multiple_databases_key_isolation(tmp_path, monkeypatch):
    # Disable keyring to force fallback to local files
    def mock_set_password(*args, **kwargs):
        raise Exception("Keyring unavailable")

    def mock_get_password(*args, **kwargs):
        return None

    monkeypatch.setattr(keyring, "set_password", mock_set_password)
    monkeypatch.setattr(keyring, "get_password", mock_get_password)

    db_path1 = tmp_path / "db1.db"
    db_path2 = tmp_path / "db2.db"
    key_path = tmp_path / "secret.key"

    crypto1 = SessionCrypto(key_path, db_path1)
    crypto2 = SessionCrypto(key_path, db_path2)

    crypto1.get_cipher()
    crypto2.get_cipher()

    assert crypto1.isolated_key_path.exists()
    assert crypto2.isolated_key_path.exists()

    # The keys must be unique and separate
    with open(crypto1.isolated_key_path, "rb") as f:
        key1 = f.read()
    with open(crypto2.isolated_key_path, "rb") as f:
        key2 = f.read()

    assert key1 != key2


def test_legacy_key_migration_and_decrypt(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"

    # Create a legacy key
    legacy_key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(legacy_key)

    crypto = SessionCrypto(key_path, db_path)
    cipher = crypto.get_cipher()
    assert cipher is not None

    # Original legacy file must NOT be unlinked
    assert key_path.exists()

    # Key must be copied to the isolated fallback key path
    assert crypto.isolated_key_path.exists()
    with open(crypto.isolated_key_path, "rb") as f:
        copied_key = f.read()
    assert copied_key == legacy_key


def test_missing_key_existing_db_fails(tmp_path, monkeypatch):
    # Disable keyring
    def mock_get_password(*args, **kwargs):
        return None

    monkeypatch.setattr(keyring, "get_password", mock_get_password)

    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"

    # Create fake existing DB with documents table and some data
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO documents (id) VALUES (1)")

    crypto = SessionCrypto(key_path, db_path)

    # Attempting to get cipher must fail because key is missing (keyring, isolated, legacy are all absent)
    with pytest.raises(
        RuntimeError, match="Database accessed but key file is missing."
    ):
        crypto.get_cipher()


def test_copy_db_to_new_system_without_keyring(tmp_path, monkeypatch):
    # Disable keyring entirely
    def mock_set_password(*args, **kwargs):
        raise Exception("Keyring unavailable")

    def mock_get_password(*args, **kwargs):
        return None

    monkeypatch.setattr(keyring, "set_password", mock_set_password)
    monkeypatch.setattr(keyring, "get_password", mock_get_password)

    db_dir1 = tmp_path / "machine1"
    db_dir1.mkdir()
    db_path1 = db_dir1 / "autosorter.db"
    key_path1 = db_dir1 / "secret.key"

    db_path1.touch()

    crypto1 = SessionCrypto(key_path1, db_path1)
    crypto1.get_cipher()

    # Encrypt some text
    original_text = "Highly secure database info."
    encrypted = crypto1.encrypt_text(original_text)

    # Simulate copy to a new directory (machine2)
    db_dir2 = tmp_path / "machine2"
    db_dir2.mkdir()

    # Copy the DB file and the hidden .keys/ folder
    shutil.copy(db_path1, db_dir2 / "autosorter.db")
    shutil.copytree(crypto1.isolated_dir, db_dir2 / ".keys")

    # Access it on machine 2
    crypto2 = SessionCrypto(db_dir2 / "secret.key", db_dir2 / "autosorter.db")
    cipher2 = crypto2.get_cipher()
    assert cipher2 is not None

    # Should successfully decrypt
    decrypted = crypto2.decrypt_text(encrypted)
    assert decrypted == original_text


def test_standard_sqlite_fallback_rejection(tmp_path, monkeypatch):
    """Verify that standard SQLite fallback connections are completely rejected during initialization."""
    import sys
    monkeypatch.setattr(sys, "platform", "linux")

    from app.core import db_conn

    monkeypatch.setattr(db_conn, "HAS_SQLCIPHER", False)

    db_path = tmp_path / "autosorter.db"
    with pytest.raises(RuntimeError, match="SQLCipher library is missing"):
        db_conn.get_db_connection(str(db_path))


def test_missing_cipher_version_rejection(tmp_path, monkeypatch):
    """Verify that if the driver lacks cipher capability or returns empty version, we reject and close."""
    import sys
    monkeypatch.setattr(sys, "platform", "linux")

    from app.core import db_conn

    monkeypatch.setattr(db_conn, "HAS_SQLCIPHER", True)

    # Mock sqlite3.connect to return a mock connection whose cursor returns empty for cipher_version
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (None,)  # empty version
    mock_conn.cursor.return_value = mock_cursor

    monkeypatch.setattr(db_conn.sqlite3, "connect", MagicMock(return_value=mock_conn))

    db_path = tmp_path / "autosorter.db"
    with pytest.raises(
        RuntimeError, match="SQLCipher is not active on this connection context"
    ):
        db_conn.get_db_connection(str(db_path))

    mock_conn.close.assert_called()


def test_decryption_failure_safe_error_propagation(tmp_path, monkeypatch, caplog):
    """Verify that when decryption fails, we:
    1. Immediately halt execution and raise a descriptive DatabaseError.
    2. Do NOT delete or modify any database files on disk (db, -wal, -shm).
    3. Log a clear, scrubbed error message without raw keys or credentials.
    4. Can successfully load the original intact database once the keyring/key is restored.
    """
    import gc
    import logging
    from contextlib import closing

    from app.core import db_conn
    if not db_conn.HAS_SQLCIPHER:
        pytest.skip("SQLCipher is missing/inactive; skipping decryption failure test.")

    from app.core.path_utils import resolve_db_crypto

    db_path = tmp_path / "secure_autosorter.db"

    # 1. Create and populate a database normally using the standard helper.
    # Set up some dummy keyring credentials
    crypto = resolve_db_crypto(db_path)
    crypto.get_cipher()
    correct_key = crypto.get_raw_key()
    assert correct_key is not None

    def create_initial_db(path, key):
        with closing(
            db_conn.sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
        ) as conn:
            with closing(conn.cursor()) as cursor:
                cursor.execute(f"PRAGMA key = '{key}'")
                cursor.execute(
                    "CREATE TABLE test_table (id INTEGER PRIMARY KEY, value TEXT)"
                )
                cursor.execute("INSERT INTO test_table (value) VALUES ('secret_data')")
                conn.commit()

    create_initial_db(db_path, correct_key)
    gc.collect()

    # Verify database file physically exists on disk and record its size
    assert db_path.exists()
    initial_size = db_path.stat().st_size
    assert initial_size > 0

    # Spy on os.remove to ensure our code never attempts to delete any database files
    removed_files = []
    original_remove = os.remove

    def mock_remove(path):
        removed_files.append(str(path))
        if os.path.exists(path):
            original_remove(path)

    monkeypatch.setattr(os, "remove", mock_remove)

    # 2. Simulate a locked/misconfigured keyring on subsequent startup
    # We mock keyring to return a mismatched/wrong key (or None)
    mismatched_key = Fernet.generate_key().decode("utf-8")

    # Clean up any potential leftover isolated fallback files to guarantee we only test mismatched key
    if crypto.isolated_key_path.exists():
        crypto.isolated_key_path.unlink()
    if crypto.key_path.exists():
        crypto.key_path.unlink()

    def mock_get_password_mismatched(service, account):
        return mismatched_key

    monkeypatch.setattr(keyring, "get_password", mock_get_password_mismatched)
    db_conn.clear_connection_cache()

    # Try to open the connection. It must raise a sqlite3.DatabaseError (decryption failure).
    exc_str = ""
    exc_type = None
    module_name = ""
    class_name = ""
    is_instance_db_err = False

    try:
        with caplog.at_level(logging.ERROR):
            db_conn.get_db_connection(str(db_path))
    except Exception as e:
        exc_str = str(e)
        exc_type = type(e)
        module_name = getattr(exc_type, "__module__", "") or ""
        module_name = str(module_name).lower()
        class_name = getattr(exc_type, "__name__", "") or ""
        is_instance_db_err = isinstance(e, (db_conn.sqlite3.Error, sqlite3.Error))
        # Explicitly clear exception and traceback reference to allow GC to release Windows file locks
        e = None

    db_conn.clear_connection_cache()
    gc.collect()

    # Verify that the exception raised is indeed a DatabaseError variant
    is_database_error = (
        is_instance_db_err
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
        or any(
            msg in exc_str.lower()
            for msg in (
                "not a database",
                "encrypted",
                "disk i/o error",
                "malformed",
                "authentication",
                "password",
                "passphrase",
                "mac",
                "bad decrypt",
                "mismatch",
                "failed to decrypt database",
            )
        )
    )
    assert is_database_error, f"Expected a database error, got {exc_type}"

    # 3. Verify that the descriptive exception was raised
    assert "Failed to decrypt database" in exc_str
    assert "OS keyring" in exc_str

    # 4. Verify that NO database files were deleted, modified, or truncated on disk by our application
    assert db_path.exists()
    assert db_path.stat().st_size == initial_size
    for removed in removed_files:
        assert "secure_autosorter.db" not in removed

    # 5. Verify the logs contain a descriptive error message indicating decryption or keyring failure
    # and that the raw key/password is completely scrubbed from logs.
    log_text = caplog.text
    assert "Database decryption failed" in log_text
    assert (
        "locked OS keyring" in log_text or "mismatched cryptographic keys" in log_text
    )
    assert "secure_autosorter.db" in log_text
    assert mismatched_key not in log_text
    assert correct_key not in log_text

    # 6. Correct the keyring configuration (restore correct key)
    def mock_get_password_correct(service, account):
        return correct_key

    monkeypatch.setattr(keyring, "get_password", mock_get_password_correct)
    db_conn.clear_connection_cache()

    # Attempt connection again. It should load successfully, and we should be able to read our original data.
    def verify_db_contents(path):
        conn = db_conn.get_db_connection(str(path))
        try:
            with closing(conn.cursor()) as cursor:
                cursor.execute("SELECT value FROM test_table")
                row = cursor.fetchone()
                assert row is not None
                assert row[0] == "secret_data"
            # Switch journal mode back to DELETE while connection is open
            with closing(conn.cursor()) as cursor:
                cursor.execute("PRAGMA journal_mode = DELETE")
        finally:
            # Deterministically clear the connection cache to close the connection and release all locks
            db_conn.clear_connection_cache()
            conn = None
            cursor = None
            row = None
            gc.collect()

    # 7. Clean up connection handles and run garbage collection.
    # On Windows, clearing the cache and invoking gc.collect() immediately releases
    # active file descriptors on the database file, ensuring no locks persist.
    verify_db_contents(db_path)
