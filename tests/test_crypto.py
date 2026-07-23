import os
import shutil
import sqlite3
from contextlib import closing

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

    cipher1 = crypto1.get_cipher()
    cipher2 = crypto2.get_cipher()

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
    with pytest.raises(RuntimeError, match="Database accessed but key file is missing."):
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
    cipher1 = crypto1.get_cipher()

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
