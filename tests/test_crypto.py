import os
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SessionCrypto


def test_key_generation_and_permissions(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    
    crypto = SessionCrypto(key_path, db_path)
    cipher = crypto.get_cipher()
    
    assert key_path.exists()
    assert isinstance(cipher, Fernet)
    
    # Check strict permissions
    if os.name != "nt":
        stat = os.stat(key_path)
        assert (stat.st_mode & 0o777) == 0o600

def test_missing_key_with_existing_db(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE documents (id TEXT)")
    conn.execute("INSERT INTO documents VALUES ('test')")
    conn.commit()
    conn.close()

    crypto = SessionCrypto(key_path, db_path)
    
    with pytest.raises(RuntimeError, match="Database accessed but key file is missing"):
        crypto.get_cipher()

def test_missing_key_with_empty_db(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE documents (id TEXT)")
    conn.commit()
    conn.close()

    crypto = SessionCrypto(key_path, db_path)
    cipher = crypto.get_cipher()
    
    assert key_path.exists()
    assert isinstance(cipher, Fernet)

def test_encryption_decryption(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    
    crypto = SessionCrypto(key_path, db_path)
    
    text = "Hello, privacy!"
    encrypted = crypto.encrypt_text(text)
    assert encrypted != text.encode()
    
    decrypted = crypto.decrypt_text(encrypted)
    assert decrypted == text

def test_invalid_key(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    
    key_path.write_text("invalid_key_data")
    
    crypto = SessionCrypto(key_path, db_path)
    with pytest.raises(RuntimeError, match="key file is missing or invalid"):
        crypto.get_cipher()
