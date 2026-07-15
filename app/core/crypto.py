"""Cryptographic management for envelope encryption."""

import os
import sqlite3

from cryptography.fernet import Fernet

from app.config import get_app_dir

_fernet_instance = None

def get_cipher():
    """Get or initialize the Fernet cipher instance."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    key_path = get_app_dir() / "secret.key"
    
    if not key_path.exists():
        db_path = get_app_dir() / "autosorter.db"
        if db_path.exists():
            try:
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='documents'")
                    if cursor.fetchone()[0] > 0:
                        cursor = conn.execute("SELECT count(*) FROM documents")
                        if cursor.fetchone()[0] > 0:
                            raise RuntimeError("Database accessed but key file is missing.")
            except sqlite3.Error:
                pass
        
        # Generate new key with strict permissions
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(key)
            
    if not key_path.exists():
        raise RuntimeError("Database accessed but key file is missing.")

    try:
        with open(key_path, "rb") as f:
            key = f.read().strip()
        _fernet_instance = Fernet(key)
        return _fernet_instance
    except Exception as e:
        raise RuntimeError("Database accessed but key file is missing or invalid.") from e

def encrypt_text(text: str) -> bytes:
    """Encrypt a string and return bytes."""
    if text is None:
        return None
    cipher = get_cipher()
    return cipher.encrypt(text.encode("utf-8"))

def decrypt_text(cipher_bytes: bytes) -> str:
    """Decrypt bytes and return the original string."""
    if cipher_bytes is None:
        return None
    cipher = get_cipher()
    try:
        # Allow passing string if it was somehow stored as string
        if isinstance(cipher_bytes, str):
            cipher_bytes = cipher_bytes.encode("utf-8")
        return cipher.decrypt(cipher_bytes).decode("utf-8")
    except Exception as e:
        raise RuntimeError("Failed to decrypt text") from e

def encrypt_embedding(emb_bytes: bytes) -> bytes:
    """Encrypt the raw embedding bytes."""
    if emb_bytes is None:
        return None
    cipher = get_cipher()
    return cipher.encrypt(emb_bytes)

def decrypt_embedding(cipher_bytes: bytes) -> bytes:
    """Decrypt the embedding bytes."""
    if cipher_bytes is None:
        return None
    cipher = get_cipher()
    try:
        return cipher.decrypt(cipher_bytes)
    except Exception as e:
        raise RuntimeError("Failed to decrypt embedding") from e
