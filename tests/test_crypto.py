import os
import sqlite3

import numpy as np
import pytest

from app.core import crypto


def test_key_generation_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    
    # Trigger key generation
    cipher = crypto.get_cipher()
    assert cipher is not None
    
    key_path = tmp_path / "secret.key"
    assert key_path.exists()
    
    # Check permissions (0o600) on non-Windows platforms
    if os.name != "nt":
        stat = os.stat(key_path)
        assert oct(stat.st_mode)[-3:] == "600"
    
def test_missing_key_with_existing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    
    db_path = tmp_path / "autosorter.db"
    
    # Create fake DB with documents table and some data
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO documents (id) VALUES (1)")
    conn.close()
        
    # Attempting to get cipher should now fail because key is missing but DB has data
    with pytest.raises(RuntimeError, match="Database accessed but key file is missing."):
        crypto.get_cipher()
        
def test_missing_key_with_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    
    db_path = tmp_path / "autosorter.db"
    
    # Create fake DB with NO data
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
    conn.close()
        
    # Should automatically generate key without error
    cipher = crypto.get_cipher()
    assert cipher is not None
    assert (tmp_path / "secret.key").exists()

def test_encryption_decryption(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    
    original_text = "This is a sensitive document."
    enc_text = crypto.encrypt_text(original_text)
    assert enc_text != original_text.encode('utf-8')
    assert crypto.decrypt_text(enc_text) == original_text
    
    # Embeddings
    original_emb = np.array([0.1, 0.2, 0.3], dtype=np.float32).tobytes()
    enc_emb = crypto.encrypt_embedding(original_emb)
    assert enc_emb != original_emb
    assert crypto.decrypt_embedding(enc_emb) == original_emb

def test_invalid_key(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_fernet_instance", None)
    monkeypatch.setattr(crypto, "get_app_dir", lambda: tmp_path)
    
    key_path = tmp_path / "secret.key"
    with open(key_path, "wb") as f:
        f.write(b"invalid_key_data_that_is_too_short")
        
    with pytest.raises(RuntimeError, match="Database accessed but key file is missing or invalid."):
        crypto.get_cipher()
