"""Cryptographic management for envelope encryption."""

import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet


class SessionCrypto:
    def __init__(self, key_path: Path, db_path: Path):
        self.key_path = key_path
        self.db_path = db_path
        self._cipher = None

    def get_cipher(self):
        """Get or initialize the Fernet cipher instance."""
        if self._cipher is not None:
            return self._cipher

        if not self.key_path.exists():
            if self.db_path.exists():
                try:
                    conn = sqlite3.connect(str(self.db_path))
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
            
            # Generate new key with strict permissions
            key = Fernet.generate_key()
            fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(key)
                
        if not self.key_path.exists():
            raise RuntimeError("Database accessed but key file is missing.")

        try:
            with open(self.key_path, "rb") as f:
                key = f.read().strip()
            self._cipher = Fernet(key)
            return self._cipher
        except Exception as e:
            raise RuntimeError("Database accessed but key file is missing or invalid.") from e

    def encrypt_text(self, text: str) -> bytes:
        """Encrypt a string and return bytes."""
        if text is None:
            return None
        cipher = self.get_cipher()
        return cipher.encrypt(text.encode("utf-8"))

    def decrypt_text(self, cipher_bytes: bytes) -> str:
        """Decrypt bytes and return the original string."""
        if cipher_bytes is None:
            return None
        cipher = self.get_cipher()
        try:
            # Allow passing string if it was somehow stored as string
            if isinstance(cipher_bytes, str):
                cipher_bytes = cipher_bytes.encode("utf-8")
            return cipher.decrypt(cipher_bytes).decode("utf-8")
        except Exception as e:
            raise RuntimeError("Failed to decrypt text") from e

    def encrypt_embedding(self, emb_bytes: bytes) -> bytes:
        """Encrypt the raw embedding bytes."""
        if emb_bytes is None:
            return None
        cipher = self.get_cipher()
        return cipher.encrypt(emb_bytes)

    def decrypt_embedding(self, cipher_bytes: bytes) -> bytes:
        """Decrypt the embedding bytes."""
        if cipher_bytes is None:
            return None
        cipher = self.get_cipher()
        try:
            return cipher.decrypt(cipher_bytes)
        except Exception as e:
            raise RuntimeError("Failed to decrypt embedding") from e
