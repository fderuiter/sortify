import builtins
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import keyring
import numpy as np
import pytest
from cryptography.fernet import Fernet

from app.core.crypto import (
    EphemeralSessionCrypto,
    SessionCrypto,
    VectorBuffer,
    _json_default,
    decrypt_ipc_payload,
    encrypt_ipc_payload,
    get_fallback_keys_dir,
    secure_delete_dir,
    secure_delete_file,
    zero_vector_buffer,
)
from app.core.exceptions import CryptoError


def _same_path(p1, p2) -> bool:
    return os.path.normcase(os.path.abspath(str(p1))) == os.path.normcase(os.path.abspath(str(p2)))


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
    from app.core import db_conn

    monkeypatch.setattr(db_conn, "_disable_pytest_win_fallback", True)
    monkeypatch.setattr(db_conn, "HAS_SQLCIPHER", False)

    db_path = tmp_path / "autosorter.db"
    with pytest.raises(RuntimeError, match="SQLCipher library is missing"):
        db_conn.get_db_connection(str(db_path))


def test_missing_cipher_version_rejection(tmp_path, monkeypatch):
    """Verify that if the driver lacks cipher capability or returns empty version, we reject and close."""
    from app.core import db_conn

    monkeypatch.setattr(db_conn, "_disable_pytest_win_fallback", True)
    monkeypatch.setattr(db_conn, "HAS_SQLCIPHER", True)

    # Mock sqlite3.connect to return a mock connection whose cursor returns empty for cipher_version
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (None,)  # empty version
    mock_conn.cursor.return_value = mock_cursor

    mock_sqlite3 = MagicMock()
    mock_sqlite3.connect.return_value = mock_conn
    mock_sqlite3.Error = Exception
    monkeypatch.setattr(db_conn, "sqlite3", mock_sqlite3)

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

    is_active = False
    if db_conn.HAS_SQLCIPHER:
        try:
            with closing(db_conn.sqlite3.connect(":memory:")) as conn:
                with closing(conn.cursor()) as cursor:
                    cursor.execute("PRAGMA cipher_version;")
                    row = cursor.fetchone()
                    if row and row[0]:
                        is_active = True
        except Exception:
            pass

    if not is_active:
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


def test_crypto_manager_hash_and_keyring_derivation(tmp_path):
    from app.core.crypto import CryptoManager

    db_path = tmp_path / "test_db.sqlite"

    sha256_hash = CryptoManager.derive_db_hash(db_path, algorithm="sha256")
    md5_hash = CryptoManager.derive_db_hash(db_path, algorithm="md5")

    assert len(sha256_hash) == 64
    assert len(md5_hash) == 32
    assert sha256_hash != md5_hash

    account_sha256 = CryptoManager.derive_keyring_account(db_path, legacy_md5=False)
    account_md5 = CryptoManager.derive_keyring_account(db_path, legacy_md5=True)

    assert account_sha256 == f"DatabaseDecryptionKey_{sha256_hash}"
    assert account_md5 == f"DatabaseDecryptionKey_{md5_hash}"

    path_sha256 = CryptoManager.derive_isolated_key_path(db_path, legacy_md5=False)
    path_md5 = CryptoManager.derive_isolated_key_path(db_path, legacy_md5=True)

    assert path_sha256.name == f"test_db.sqlite_{sha256_hash}.key"
    assert path_md5.name == f"test_db.sqlite_{md5_hash}.key"


def test_crypto_manager_bootstrap_key():
    from app.core.crypto import CryptoManager

    key1 = CryptoManager.generate_bootstrap_key()
    key2 = CryptoManager.generate_bootstrap_key()

    assert isinstance(key1, str)
    assert len(key1) > 20
    assert key1 != key2


def test_crypto_manager_proxy_setting_envelope(tmp_path):
    from app.core.crypto import CryptoManager, SessionCrypto

    db_path = tmp_path / "proxy_test.db"
    key_path = tmp_path / "secret.key"
    crypto = SessionCrypto(key_path, db_path)

    raw_proxy = "http://user:secret123@proxy.example.com:8080"
    assert not CryptoManager.is_encrypted_proxy(raw_proxy)

    enc_proxy = CryptoManager.encrypt_proxy_setting(raw_proxy, crypto=crypto)
    assert CryptoManager.is_encrypted_proxy(enc_proxy)
    assert enc_proxy.startswith("enc:")
    assert "secret123" not in enc_proxy

    # Idempotent encryption check
    assert CryptoManager.encrypt_proxy_setting(enc_proxy, crypto=crypto) == enc_proxy

    dec_proxy = CryptoManager.decrypt_proxy_setting(enc_proxy, crypto=crypto)
    assert dec_proxy == raw_proxy


def test_session_crypto_legacy_md5_key_resolution_and_migration(tmp_path):
    from app.core.crypto import CryptoManager, SessionCrypto

    db_path = tmp_path / "legacy_app.db"
    key_path = tmp_path / "secret.key"

    legacy_key = Fernet.generate_key()

    # Pre-populate legacy MD5 keyring account
    legacy_account = CryptoManager.derive_keyring_account(db_path, legacy_md5=True)
    keyring.set_password("AutoSorter", legacy_account, legacy_key.decode("utf-8"))

    # Instantiate SessionCrypto which uses SHA-256 account as primary
    session = SessionCrypto(key_path, db_path)

    # Resolution should find legacy MD5 key, initialize cipher, and migrate to SHA-256
    cipher = session.get_cipher()
    assert cipher is not None

    # Check migrated SHA-256 keyring account
    primary_account = CryptoManager.derive_keyring_account(db_path, legacy_md5=False)
    migrated_key = keyring.get_password("AutoSorter", primary_account)
    assert migrated_key == legacy_key.decode("utf-8")
    assert session.get_raw_key() == legacy_key.decode("utf-8")


def test_get_fallback_keys_dir_windows_and_posix(monkeypatch):
    """Verify fallback key store directory resolution for Windows and POSIX environments."""
    import pathlib

    # 1. Windows with APPDATA set
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr("app.core.crypto.Path", pathlib.PurePath)
    monkeypatch.setenv("APPDATA", "/fake/appdata")
    assert (
        get_fallback_keys_dir()
        == pathlib.PurePath("/fake/appdata") / "Sortify" / "keys"
    )

    # 2. Windows without APPDATA or POSIX with HOME set
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr("app.core.crypto.Path", pathlib.Path)
    monkeypatch.setenv("HOME", "/fake/home")
    assert get_fallback_keys_dir() == Path("/fake/home") / ".sortify" / "keys"

    # 3. Non-Windows with HOME and USERPROFILE unset, Path.home() throwing error
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(
        Path, "home", MagicMock(side_effect=Exception("Path.home failed"))
    )
    monkeypatch.setattr(os.path, "expanduser", lambda p: "/fake/expanduser")
    assert get_fallback_keys_dir() == Path("/fake/expanduser") / ".sortify" / "keys"


def test_secure_delete_file_edge_cases(tmp_path, monkeypatch, caplog):
    """Verify file shredding for non-existent, zero-byte, fsync errors, and overwrite failures."""
    # 1. Non-existent file (early return)
    non_existent = tmp_path / "absent.txt"
    secure_delete_file(non_existent)

    # 2. Zero-byte file
    zero_file = tmp_path / "zero.txt"
    zero_file.touch()
    assert zero_file.exists()
    secure_delete_file(zero_file)
    assert not zero_file.exists()

    # 3. Normal file with data
    data_file = tmp_path / "data.txt"
    data_file.write_bytes(b"sensitive content 123456")
    secure_delete_file(data_file)
    assert not data_file.exists()

    # 4. fsync failure handling
    fsync_file = tmp_path / "fsync.txt"
    fsync_file.write_bytes(b"some data")

    def mock_fsync(fd):
        raise OSError("Disk fsync error")

    monkeypatch.setattr(os, "fsync", mock_fsync)
    secure_delete_file(fsync_file)
    assert not fsync_file.exists()

    # 5. Overwrite failure handling (open throws exception) -> falls back to unlink
    fail_file = tmp_path / "fail.txt"
    fail_file.write_bytes(b"content")

    def mock_open_fail(*args, **kwargs):
        raise PermissionError("Access denied during overwrite")

    monkeypatch.setattr("builtins.open", mock_open_fail)
    secure_delete_file(fail_file)
    assert not fail_file.exists()

    # 6. Unlink failure in fallback block
    fail_file2 = tmp_path / "fail2.txt"
    fail_file2.write_bytes(b"content")

    monkeypatch.setattr(
        Path, "unlink", MagicMock(side_effect=OSError("Unlink error"))
    )
    secure_delete_file(fail_file2)


def test_secure_delete_dir_edge_cases(tmp_path, monkeypatch):
    """Verify recursive directory deletion and resilient_rmtree fallback."""
    # 1. Non-existent directory
    secure_delete_dir(tmp_path / "missing_dir")

    # 2. Recursive directory tree deletion
    dir_path = tmp_path / "tree_dir"
    dir_path.mkdir()
    sub_dir = dir_path / "subdir"
    sub_dir.mkdir()
    (dir_path / "f1.txt").write_bytes(b"data1")
    (sub_dir / "f2.txt").write_bytes(b"data2")

    secure_delete_dir(dir_path)
    assert not dir_path.exists()

    # 3. Deletion failure fallback to resilient_rmtree
    fail_dir = tmp_path / "fail_dir"
    fail_dir.mkdir()

    mock_rmtree = MagicMock()
    monkeypatch.setattr("app.core.resilient_file_ops.resilient_rmtree", mock_rmtree)
    monkeypatch.setattr(
        Path, "rmdir", MagicMock(side_effect=PermissionError("Permission denied"))
    )

    secure_delete_dir(fail_dir)
    mock_rmtree.assert_called_once_with(fail_dir, ignore_errors=True)


def test_session_crypto_get_raw_key_resolution(tmp_path, monkeypatch):
    """Verify SessionCrypto.get_raw_key fallbacks and key resolution behavior."""
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    # 1. When self._cipher and self._key are set
    raw_key = Fernet.generate_key()
    crypto._cipher = Fernet(raw_key)
    crypto._key = raw_key
    assert crypto.get_raw_key() == raw_key.decode("utf-8")

    # 2. Test fallbacks in lines 359-378 by setting _cipher truthy but _key = None
    crypto._cipher = MagicMock()
    crypto._key = None

    # Keyring lookup fallback
    monkeypatch.setattr(keyring, "get_password", lambda svc, acc: "keyring_key_123")
    assert crypto.get_raw_key() == "keyring_key_123"

    # Keyring error handling
    def mock_keyring_error(*args, **kwargs):
        raise Exception("Keyring failure")

    monkeypatch.setattr(keyring, "get_password", mock_keyring_error)

    # Isolated key file fallback
    crypto.isolated_dir.mkdir(parents=True, exist_ok=True)
    crypto.isolated_key_path.write_bytes(b"isolated_key_456")
    assert crypto.get_raw_key() == "isolated_key_456"

    # Isolated key file read error
    original_open = builtins.open

    def mock_open_isolated_err(file, *args, **kwargs):
        if _same_path(file, crypto.isolated_key_path):
            raise OSError("Isolated key read error")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open_isolated_err)

    # Key path fallback
    key_path.write_bytes(b"key_path_789")
    assert crypto.get_raw_key() == "key_path_789"

    # Key path read error and no keys found
    def mock_open_all_err(file, *args, **kwargs):
        raise OSError("Key path read error")

    monkeypatch.setattr("builtins.open", mock_open_all_err)
    assert crypto.get_raw_key() is None


def test_session_crypto_get_cipher_os_error_branches(tmp_path, monkeypatch):
    """Verify error and permission handling across get_cipher execution paths."""
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    # Keyring exception
    monkeypatch.setattr(
        keyring, "get_password", MagicMock(side_effect=Exception("Keyring err"))
    )

    # 1. Setup files for isolated_key_path, legacy_isolated_key_path, legacy_isolated_dir, key_path
    crypto.isolated_key_path.parent.mkdir(parents=True, exist_ok=True)
    crypto.isolated_key_path.touch()

    crypto.legacy_isolated_key_path.parent.mkdir(parents=True, exist_ok=True)
    crypto.legacy_isolated_key_path.touch()

    candidate = crypto.legacy_isolated_dir / "candidate.key"
    candidate.touch()

    key_path.touch()

    original_open = builtins.open

    # Fail reading on all key files to test OSError handlers on reading
    class MockFailFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            raise OSError("Read error inside read()")

        def strip(self):
            raise OSError("Strip error inside strip()")

    def mock_open_read_fail(file, mode="r", *args, **kwargs):
        if ("r" in mode) and ("w" not in mode) and any(
            _same_path(file, p)
            for p in (
                crypto.isolated_key_path,
                crypto.legacy_isolated_key_path,
                candidate,
                key_path,
            )
        ):
            return MockFailFile()
        return original_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", mock_open_read_fail)

    # Will proceed to generate a key since reading failed on all files
    cipher = crypto.get_cipher()
    assert cipher is not None

    # Test legacy migration with read key and cleanup exception
    crypto2 = SessionCrypto(tmp_path / "k2.key", tmp_path / "db2.db")
    legacy_key = Fernet.generate_key()
    crypto2.legacy_isolated_dir.mkdir(parents=True, exist_ok=True)
    (crypto2.legacy_isolated_dir / "mig.key").write_bytes(legacy_key)

    monkeypatch.setattr(
        keyring, "get_password", MagicMock(side_effect=Exception("Keyring err"))
    )
    monkeypatch.setattr(os, "chmod", MagicMock(side_effect=OSError("chmod error")))
    monkeypatch.setattr(os, "open", MagicMock(side_effect=OSError("os.open error")))
    monkeypatch.setattr(
        keyring,
        "set_password",
        MagicMock(side_effect=Exception("set_password error")),
    )
    monkeypatch.setattr(
        "app.core.crypto.secure_delete_dir",
        MagicMock(side_effect=Exception("delete error")),
    )

    cipher2 = crypto2.get_cipher()
    assert cipher2 is not None

    # 3. Test iterdir OSError on legacy_isolated_dir
    crypto3 = SessionCrypto(tmp_path / "k3.key", tmp_path / "db3.db")
    crypto3.legacy_isolated_dir.mkdir(parents=True, exist_ok=True)

    orig_iterdir = Path.iterdir

    def mock_iterdir(self_path):
        if _same_path(self_path, crypto3.legacy_isolated_dir):
            raise OSError("iterdir error")
        return orig_iterdir(self_path)

    monkeypatch.setattr(Path, "iterdir", mock_iterdir)
    cipher3 = crypto3.get_cipher()
    assert cipher3 is not None


def test_session_crypto_database_guard_checks(tmp_path, monkeypatch):
    """Verify database guard exception handling for encrypted or invalid databases."""
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"

    # Create DB file
    db_path.write_bytes(b"fake sqlite db header content")

    crypto = SessionCrypto(key_path, db_path)

    monkeypatch.setattr(keyring, "get_password", lambda *args: None)

    # 1. sqlite3.DatabaseError in guard check
    import app.core.crypto

    def mock_connect_db_err(*args, **kwargs):
        raise app.core.crypto.sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr("app.core.crypto.sqlite3.connect", mock_connect_db_err)

    with pytest.raises(
        CryptoError, match="Database accessed but key file is missing."
    ):
        crypto.get_cipher()

    # 2. sqlite3.Error in guard check (general error)
    def mock_connect_err(*args, **kwargs):
        raise app.core.crypto.sqlite3.Error("Operational SQLite error")

    monkeypatch.setattr("app.core.crypto.sqlite3.connect", mock_connect_err)

    # Should proceed to generate key
    cipher = crypto.get_cipher()
    assert cipher is not None


def test_session_crypto_generated_key_os_errors_and_invalid_key(
    tmp_path, monkeypatch
):
    """Verify chmod and fdopen error handling during generated key persistence, and invalid key handling."""
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)

    # Disable keyring
    monkeypatch.setattr(keyring, "get_password", lambda *args: None)
    monkeypatch.setattr(
        keyring,
        "set_password",
        MagicMock(side_effect=Exception("Keyring write error")),
    )

    monkeypatch.setattr(os, "chmod", MagicMock(side_effect=OSError("chmod error")))
    monkeypatch.setattr(os, "open", MagicMock(side_effect=OSError("os.open error")))

    cipher = crypto.get_cipher()
    assert cipher is not None

    # Test invalid Fernet key
    crypto2 = SessionCrypto(key_path, tmp_path / "db2.db")
    monkeypatch.setattr(keyring, "get_password", lambda *args: "invalid_key_string")

    with pytest.raises(
        CryptoError, match="Database accessed but key file is missing or invalid."
    ):
        crypto2.get_cipher()


def test_encrypt_decrypt_text_and_vector_methods(tmp_path):
    """Verify text and vector encryption/decryption helpers with None, string, and invalid inputs."""
    crypto = SessionCrypto(tmp_path / "k.key", tmp_path / "d.db")
    crypto._key = Fernet.generate_key()
    crypto._cipher = Fernet(crypto._key)

    # Text methods
    assert crypto.encrypt_text(None) is None
    assert crypto.decrypt_text(None) is None

    enc = crypto.encrypt_text("hello world")
    assert crypto.decrypt_text(enc) == "hello world"
    assert crypto.decrypt_text(enc.decode("utf-8")) == "hello world"

    with pytest.raises(CryptoError, match="Failed to decrypt text"):
        crypto.decrypt_text(b"invalid_ciphertext")

    # Vector methods
    assert crypto.encrypt_vector(None) is None
    assert crypto.decrypt_vector(None) is None

    vec_enc = crypto.encrypt_vector("[0.1, 0.2]")
    assert crypto.decrypt_vector(vec_enc) == "[0.1, 0.2]"
    assert crypto.decrypt_vector(vec_enc.decode("utf-8")) == "[0.1, 0.2]"

    with pytest.raises(CryptoError, match="Failed to decrypt vector"):
        crypto.decrypt_vector(b"invalid_vector")


def test_decrypt_and_parse_vector_cache_and_eviction(tmp_path, caplog):
    """Verify decrypt_and_parse_vector caching, LRU eviction, string inputs, and decryption failures."""
    crypto = SessionCrypto(tmp_path / "k.key", tmp_path / "d.db")
    crypto._key = Fernet.generate_key()
    crypto._cipher = Fernet(crypto._key)

    # 1. None check
    assert crypto.decrypt_and_parse_vector(None) is None

    # 2. Encryption and parsing
    vec_data = [0.1, 0.2, 0.3]
    enc_bytes = crypto.encrypt_vector(json.dumps(vec_data))

    # Cache miss
    res1 = crypto.decrypt_and_parse_vector(enc_bytes)
    assert res1 == vec_data

    # String input & cache hit
    enc_str = enc_bytes.decode("utf-8")
    res2 = crypto.decrypt_and_parse_vector(enc_str)
    assert res2 == vec_data

    # 3. LRU Cache Eviction
    crypto._vector_cache_max_entries = 2
    crypto._vector_parsed_cache.clear()

    enc1 = crypto.encrypt_vector(json.dumps([1.0]))
    enc2 = crypto.encrypt_vector(json.dumps([2.0]))
    enc3 = crypto.encrypt_vector(json.dumps([3.0]))

    crypto.decrypt_and_parse_vector(enc1)
    crypto.decrypt_and_parse_vector(enc2)
    assert len(crypto._vector_parsed_cache) == 2

    crypto.decrypt_and_parse_vector(enc3)
    assert len(crypto._vector_parsed_cache) == 2
    assert enc1 not in crypto._vector_parsed_cache

    # 4. Decryption/JSON parsing failure
    bad_res = crypto.decrypt_and_parse_vector(b"invalid_cipher_bytes")
    assert bad_res is None
    assert "Failed to decrypt or parse vector" in caplog.text


def test_vector_buffer_comprehensive_operations(monkeypatch):
    """Verify VectorBuffer initialization variants, indexing, slice, numpy conversions, and zero-filling."""
    # 1. Constructor variants
    vb_list = VectorBuffer([1.0, 2.0, 3.0])
    assert len(vb_list) == 3

    vb_tuple = VectorBuffer((4.0, 5.0))
    assert len(vb_tuple) == 2

    vb_empty = VectorBuffer([])
    assert len(vb_empty) == 0

    vb_from_vb = VectorBuffer(vb_list)
    assert len(vb_from_vb) == 3

    arr = np.array([0.5, 1.5], dtype=np.float32)
    vb_np = VectorBuffer(arr)
    assert len(vb_np) == 2

    ba = bytearray(b"\x00\x00\x00\x00\x00\x00\x80\x3f")
    vb_ba = VectorBuffer(ba)
    assert len(vb_ba) == 2

    b_bytes = bytes(ba)
    vb_bytes = VectorBuffer(b_bytes)
    assert len(vb_bytes) == 2

    vb_none = VectorBuffer(None)
    assert len(vb_none) == 0

    # 2. Indexing and Slicing
    assert vb_list[0] == pytest.approx(1.0)
    assert vb_list[-1] == pytest.approx(3.0)
    assert vb_list[0:2] == pytest.approx([1.0, 2.0])

    with pytest.raises(IndexError, match="Vector index out of range"):
        _ = vb_list[10]

    with pytest.raises(IndexError, match="Vector index out of range"):
        _ = vb_list[-10]

    # 3. NumPy conversion
    np_res = vb_list.to_numpy()
    assert np.allclose(np_res, np.array([1.0, 2.0, 3.0], dtype=np.float32))

    empty_np = vb_empty.to_numpy()
    assert len(empty_np) == 0

    # NumPy missing exception
    monkeypatch.setattr("app.core.crypto.np", None)
    with pytest.raises(RuntimeError, match="NumPy is not installed"):
        vb_list.to_numpy()

    # Restore np
    monkeypatch.setattr("app.core.crypto.np", np)

    # 4. Zero fill & cleared indexing error
    vb_list.zero_fill()
    assert vb_list.is_zeroed()
    with pytest.raises(IndexError, match="Vector buffer has been zeroed/cleared"):
        _ = vb_list[0]

    # 5. zero_vector_buffer helper
    zero_vector_buffer(None)


def test_json_default_helper_types():
    """Verify _json_default custom JSON serializer for sets, tuples, models, custom list converters, and invalid types."""
    # set and tuple
    assert _json_default({1, 2}) in ([1, 2], [2, 1])
    assert _json_default((3, 4)) == [3, 4]

    # model_dump
    class ModelDumpObj:
        def model_dump(self):
            return {"a": 1}

    assert _json_default(ModelDumpObj()) == {"a": 1}

    # dict
    class DictObj:
        def dict(self):
            return {"b": 2}

    assert _json_default(DictObj()) == {"b": 2}

    # to_list
    class ToListObj:
        def to_list(self):
            return [5, 6]

    assert _json_default(ToListObj()) == [5, 6]

    # tolist
    class ToListNumpyObj:
        def tolist(self):
            return [7, 8]

    assert _json_default(ToListNumpyObj()) == [7, 8]

    # Unsupported type
    with pytest.raises(
        TypeError, match="Object of type object is not JSON serializable"
    ):
        _json_default(object())


def test_ephemeral_session_crypto_and_ipc_payloads():
    """Verify EphemeralSessionCrypto and standalone IPC encryption functions."""
    # Key input types
    key_str = Fernet.generate_key().decode("utf-8")
    crypto_str = EphemeralSessionCrypto(key_str)
    assert crypto_str.session_key == key_str.encode("utf-8")

    key_bytes = Fernet.generate_key()
    crypto_bytes = EphemeralSessionCrypto(key_bytes)
    assert crypto_bytes.session_key == key_bytes

    # Payload encryption/decryption
    payload = {"data": [1, 2, 3]}
    enc = crypto_bytes.encrypt_payload(payload)
    dec = crypto_bytes.decrypt_payload(enc)
    assert dec == payload

    # Post-purge rejections
    crypto_bytes.purge()
    with pytest.raises(ValueError, match="Ephemeral session key has been purged"):
        crypto_bytes.encrypt_payload(payload)

    with pytest.raises(ValueError, match="Ephemeral session key has been purged"):
        crypto_bytes.decrypt_payload(enc)

    # Standalone functions
    # Missing session key
    with pytest.raises(ValueError, match="Session key cannot be None"):
        encrypt_ipc_payload(payload, None)

    with pytest.raises(ValueError, match="Session key cannot be None"):
        decrypt_ipc_payload(enc, None)

    # String vs bytes key in IPC payload functions
    enc_ipc = encrypt_ipc_payload(payload, key_str)
    assert decrypt_ipc_payload(enc_ipc, key_str) == payload
    assert decrypt_ipc_payload(enc_ipc, key_str.encode("utf-8")) == payload

    # UnicodeDecodeError handling -> JSONDecodeError
    cipher = Fernet(key_bytes)
    bad_utf8_encrypted = cipher.encrypt(b"\x80\x81\x82\x83")

    crypto_active = EphemeralSessionCrypto(key_bytes)
    with pytest.raises(json.JSONDecodeError):
        crypto_active.decrypt_payload(bad_utf8_encrypted)

    with pytest.raises(json.JSONDecodeError):
        decrypt_ipc_payload(bad_utf8_encrypted, key_bytes)


def test_top_level_import_fallbacks_and_missing_key_branch(tmp_path, monkeypatch):
    """Verify top-level module import error fallbacks and get_cipher missing key branch."""
    # 1. Test key is None in get_cipher (line 341)
    crypto = SessionCrypto(tmp_path / "k.key", tmp_path / "d.db")
    monkeypatch.setattr(keyring, "get_password", lambda *args: None)
    monkeypatch.setattr("cryptography.fernet.Fernet.generate_key", lambda: None)

    mock_file = MagicMock()
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: mock_file)
    monkeypatch.setattr(os, "open", lambda *args, **kwargs: 123)
    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: mock_file)

    with pytest.raises(
        CryptoError, match="Database accessed but key file is missing."
    ):
        crypto.get_cipher()


def test_import_fallbacks():
    """Verify import fallbacks when numpy or sqlite3 / sqlcipher3 are unavailable in isolated processes."""
    import subprocess
    import sys

    repo_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = repo_root + os.pathsep + os.pathsep.join(sys.path)

    # 1. Simulate numpy import error
    code_np = (
        "import sys\n"
        "sys.modules['numpy'] = None\n"
        "import app.core.crypto\n"
        "assert app.core.crypto.np is None\n"
    )
    res_np = subprocess.run(
        [sys.executable, "-c", code_np],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )
    assert res_np.returncode == 0, f"Numpy fallback failed: {res_np.stderr}"

    # 2. Simulate sqlite3 and sqlcipher3 import error
    code_sql = (
        "import sys\n"
        "sys.modules['sqlite3'] = None\n"
        "sys.modules['sqlcipher3'] = None\n"
        "import app.core.crypto\n"
        "assert app.core.crypto.sqlite3 is None\n"
    )
    res_sql = subprocess.run(
        [sys.executable, "-c", code_sql],
        capture_output=True,
        text=True,
        env=env,
        cwd=repo_root,
    )
    assert res_sql.returncode == 0, f"SQLite fallback failed: {res_sql.stderr}"

