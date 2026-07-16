import sqlite3

import numpy as np
import pytest

from app.core import crypto


def test_key_generation_keyring(tmp_path, monkeypatch, mock_keyring):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    
    # Ensure no key exists initially
    mock_keyring.passwords.clear()
    
    # Trigger key generation
    cipher = crypto.get_cipher()
    assert cipher is not None
    
    # Verify key is stored in keyring
    key_str = mock_keyring.get_password(crypto._SERVICE_NAME, crypto._ACCOUNT_NAME)
    assert key_str is not None
    assert len(key_str) > 0
    
def test_missing_key_with_existing_db(tmp_path, monkeypatch, mock_keyring):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    mock_keyring.passwords.clear()
    
    db_path = tmp_path / "autosorter.db"
    
    # Create fake DB with documents table and some data
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO documents (id) VALUES (1)")
    conn.close()
        
    # Attempting to get cipher should now fail because key is missing but DB has data
    with pytest.raises(RuntimeError, match="Database accessed but encryption key is missing from keychain."):
        crypto.get_cipher()
        
def test_missing_key_with_empty_db(tmp_path, monkeypatch, mock_keyring):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    mock_keyring.passwords.clear()
    
    db_path = tmp_path / "autosorter.db"
    
    # Create fake DB with NO data
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
    conn.close()
        
    # Should automatically generate key without error
    cipher = crypto.get_cipher()
    assert cipher is not None
    assert mock_keyring.get_password(crypto._SERVICE_NAME, crypto._ACCOUNT_NAME) is not None

def test_encryption_decryption(tmp_path, monkeypatch, mock_keyring):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    mock_keyring.passwords.clear()
    
    original_text = "This is a sensitive document."
    enc_text = crypto.encrypt_text(original_text)
    assert enc_text != original_text.encode('utf-8')
    assert crypto.decrypt_text(enc_text) == original_text
    
    # Embeddings
    original_emb = np.array([0.1, 0.2, 0.3], dtype=np.float32).tobytes()
    enc_emb = crypto.encrypt_embedding(original_emb)
    assert enc_emb != original_emb
    assert crypto.decrypt_embedding(enc_emb) == original_emb

def test_invalid_key(tmp_path, monkeypatch, mock_keyring):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    mock_keyring.passwords.clear()
    
    mock_keyring.set_password(crypto._SERVICE_NAME, crypto._ACCOUNT_NAME, "invalid_key_data_that_is_too_short")
        
    with pytest.raises(RuntimeError, match="Database accessed but encryption key is missing or invalid."):
        crypto.get_cipher()
