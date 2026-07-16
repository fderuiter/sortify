"""Cryptographic management for envelope encryption."""

import sqlite3

import keyring
from cryptography.fernet import Fernet

from app.config import get_app_dir

_fernet_instance = None
_SERVICE_NAME = "autosorter"
_ACCOUNT_NAME = "database_encryption_key"

def get_cipher():
    """Get or initialize the Fernet cipher instance."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    try:
        key_str = keyring.get_password(_SERVICE_NAME, _ACCOUNT_NAME)
    except Exception as e:
        raise RuntimeError("Failed to access system keychain.") from e
    
    if not key_str:
        db_path = get_app_dir() / "autosorter.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='documents'")
                    if cursor.fetchone()[0] > 0:
                        cursor = conn.execute("SELECT count(*) FROM documents")
                        if cursor.fetchone()[0] > 0:
                            raise RuntimeError("Database accessed but encryption key is missing from keychain.")
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        
        # Generate new key
        key = Fernet.generate_key()
        key_str = key.decode("utf-8")
        try:
            keyring.set_password(_SERVICE_NAME, _ACCOUNT_NAME, key_str)
        except Exception as e:
            raise RuntimeError("Failed to store encryption key in system keychain.") from e
            
    if not key_str:
        raise RuntimeError("Database accessed but encryption key is missing from keychain.")

    try:
        key = key_str.encode("utf-8")
        _fernet_instance = Fernet(key)
        return _fernet_instance
    except Exception as e:
        raise RuntimeError("Database accessed but encryption key is missing or invalid.") from e

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
