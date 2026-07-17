import os
import sqlite3
from contextlib import closing

import keyring
import numpy as np
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
    
    assert key_path.exists()
    
    # Check strict permissions
    if os.name != "nt":
        stat = os.stat(key_path)
        assert (stat.st_mode & 0o777) == 0o600


def test_legacy_key_migration(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)
    
    legacy_key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(legacy_key)
        
    cipher = crypto.get_cipher()
    assert cipher is not None
    
    # Should be deleted
    assert not key_path.exists()
    
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
    with pytest.raises(RuntimeError, match="Database accessed but key file is missing."):
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
    assert keyring.get_password(crypto.keyring_service, crypto.keyring_account) is not None


def test_encryption_decryption(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)
    
    original_text = "This is a sensitive document."
    enc_text = crypto.encrypt_text(original_text)
    assert enc_text != original_text.encode('utf-8')
    assert crypto.decrypt_text(enc_text) == original_text
    
    # Embeddings
    original_emb = np.array([0.1, 0.2, 0.3], dtype=np.float32).tobytes()
    enc_emb = crypto.encrypt_embedding(original_emb)
    assert enc_emb != original_emb
    assert crypto.decrypt_embedding(enc_emb) == original_emb


def test_invalid_key(tmp_path):
    key_path = tmp_path / "secret.key"
    db_path = tmp_path / "autosorter.db"
    crypto = SessionCrypto(key_path, db_path)
    
    # Put invalid key in keyring directly to test invalid key behavior
    keyring.set_password(crypto.keyring_service, crypto.keyring_account, "invalid_key_data_that_is_too_short")
        
    with pytest.raises(RuntimeError, match="Database accessed but key file is missing or invalid."):
        crypto.get_cipher()
