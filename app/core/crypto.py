"""Cryptographic management for envelope encryption."""

import os
import sqlite3

import keyring
from cryptography.fernet import Fernet

import app.config

_fernet_instance = None
_raw_key = None

KEYRING_SERVICE = "AutoSorter"
KEYRING_ACCOUNT = "DatabaseDecryptionKey"

def get_cipher():
    """Get or initialize the Fernet cipher instance."""
    global _fernet_instance, _raw_key
    if _fernet_instance is not None:
        return _fernet_instance

    key_path = app.config.get_app_dir() / "secret.key"
    key = None
    
    # 1. Try loading from Keyring
    try:
        key_str = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if key_str:
            key = key_str.encode("utf-8")
    except Exception:
        pass

    # 2. Check for Legacy Plaintext File
    if key is None and key_path.exists():
        try:
            with open(key_path, "rb") as f:
                key = f.read().strip()
            
            # Try to migrate to keyring
            try:
                keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key.decode("utf-8"))
                verify_str = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
                if verify_str and verify_str.encode("utf-8") == key:
                    os.unlink(key_path)  # Securely delete legacy file
            except Exception:
                pass # Fallback to keeping it in the file
        except Exception:
            pass

    # 3. Database Guard
    if key is None:
        db_path = app.config.get_app_dir() / "autosorter.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(db_path)
                try:
                    cursor = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='documents'")
                    if cursor.fetchone()[0] > 0:
                        cursor = conn.execute("SELECT count(*) FROM documents")
                        if cursor.fetchone()[0] > 0:
                            raise RuntimeError("Database accessed but key file is missing.")
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
        
        # 4. Generate new key
        key = Fernet.generate_key()
        saved_to_keyring = False
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, key.decode("utf-8"))
            verify_str = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            if verify_str and verify_str.encode("utf-8") == key:
                saved_to_keyring = True
        except Exception:
            pass
            
        if not saved_to_keyring:
            # Fallback to local file with strict permissions
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(key)
            
    if key is None:
        raise RuntimeError("Database accessed but key file is missing.")

    _raw_key = key.decode("utf-8")
    try:
        _fernet_instance = Fernet(key)
        return _fernet_instance
    except Exception as e:
        raise RuntimeError("Database accessed but key file is missing or invalid.") from e

def get_raw_key() -> str:
    """Retrieve the raw URL-safe base64 key used for database encryption."""
    get_cipher()  # Ensure key is generated
    return _raw_key

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
