"""Cryptographic management for envelope encryption."""

import hashlib
import os
import sqlite3
from pathlib import Path

import keyring
from cryptography.fernet import Fernet


class SessionCrypto:
    """Manages encryption and decryption of data per session."""

    def __init__(self, key_path: Path, db_path: Path):
        self.db_path = Path(os.path.abspath(db_path))
        self.key_path = Path(os.path.abspath(key_path))
        self._cipher = None
        self._key = None
        self.keyring_service = "AutoSorter"
        db_hash = hashlib.md5(str(self.db_path).encode("utf-8")).hexdigest()
        self.keyring_account = f"DatabaseDecryptionKey_{db_hash}"
        self.isolated_dir = self.db_path.parent / ".keys"
        self.isolated_key_path = self.isolated_dir / f"{self.db_path.name}.key"

    def get_cipher(self):
        """Get or initialize the Fernet cipher instance."""
        if self._cipher is not None:
            return self._cipher

        key = None

        # 1. OS Keyring Lookup
        try:
            key_str = keyring.get_password(self.keyring_service, self.keyring_account)
            if key_str:
                key = key_str.encode("utf-8")
        except Exception:
            pass

        # 2. Isolated Fallback Key Lookup
        if key is None and self.isolated_key_path.exists():
            try:
                with open(self.isolated_key_path, "rb") as f:
                    key = f.read().strip()
            except Exception:
                pass

        # 3. Legacy Fallback Migration
        if key is None and self.key_path.exists():
            try:
                with open(self.key_path, "rb") as f:
                    legacy_key = f.read().strip()
                if legacy_key:
                    # Write/copy to isolated fallback key path
                    self.isolated_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    try:
                        os.chmod(self.isolated_dir, 0o700)
                    except Exception:
                        pass

                    try:
                        fd = os.open(
                            str(self.isolated_key_path),
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                            0o600,
                        )
                        with os.fdopen(fd, "wb") as f:
                            f.write(legacy_key)
                    except Exception:
                        with open(self.isolated_key_path, "wb") as f:
                            f.write(legacy_key)
                        try:
                            os.chmod(self.isolated_key_path, 0o600)
                        except Exception:
                            pass
                    key = legacy_key

                    # Try to migrate to keyring
                    try:
                        keyring.set_password(
                            self.keyring_service,
                            self.keyring_account,
                            key.decode("utf-8"),
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        # 4. Database Guard Check
        if key is None:
            if self.db_path.exists():
                try:
                    conn = sqlite3.connect(str(self.db_path))
                    try:
                        cursor = conn.execute(
                            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='documents'"
                        )
                        if cursor.fetchone()[0] > 0:
                            # It might be encrypted, so query will fail, but if it's plaintext, it will succeed
                            cursor = conn.execute("SELECT count(*) FROM documents")
                            if cursor.fetchone()[0] > 0:
                                raise RuntimeError(
                                    "Database accessed but key file is missing."
                                )
                    finally:
                        conn.close()
                except sqlite3.DatabaseError:
                    # If it's encrypted with SQLCipher, sqlite3 will fail with "file is not a database"
                    # which means it's an existing DB! We cannot read it without a key.
                    raise RuntimeError("Database accessed but key file is missing.")
                except sqlite3.Error:
                    pass

            # 5. New Key Generation
            key = Fernet.generate_key()
            saved_to_keyring = False
            try:
                keyring.set_password(
                    self.keyring_service, self.keyring_account, key.decode("utf-8")
                )
                verify_str = keyring.get_password(
                    self.keyring_service, self.keyring_account
                )
                if verify_str and verify_str.encode("utf-8") == key:
                    saved_to_keyring = True
            except Exception:
                pass

            if not saved_to_keyring:
                # Fallback to isolated fallback key path with secure permissions
                self.isolated_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    os.chmod(self.isolated_dir, 0o700)
                except Exception:
                    pass

                try:
                    fd = os.open(
                        str(self.isolated_key_path),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                    with os.fdopen(fd, "wb") as f:
                        f.write(key)
                except Exception:
                    with open(self.isolated_key_path, "wb") as f:
                        f.write(key)
                    try:
                        os.chmod(self.isolated_key_path, 0o600)
                    except Exception:
                        pass

        if key is None:
            raise RuntimeError("Database accessed but key file is missing.")

        try:
            self._key = key
            self._cipher = Fernet(key)
            return self._cipher
        except Exception as e:
            raise RuntimeError(
                "Database accessed but key file is missing or invalid."
            ) from e

    def get_raw_key(self) -> str:
        """Get the raw key string for SQLCipher."""
        if self._cipher is None:
            self.get_cipher()
        if hasattr(self, "_key") and self._key:
            return self._key.decode("utf-8")
        # Fallback (same hierarchy)
        key = None
        try:
            key_str = keyring.get_password(self.keyring_service, self.keyring_account)
            if key_str:
                key = key_str.encode("utf-8")
        except Exception:
            pass
        if key is None and self.isolated_key_path.exists():
            try:
                with open(self.isolated_key_path, "rb") as f:
                    key = f.read().strip()
            except Exception:
                pass
        if key is None and self.key_path.exists():
            try:
                with open(self.key_path, "rb") as f:
                    key = f.read().strip()
            except Exception:
                pass
        return key.decode("utf-8") if key else None

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
            if isinstance(cipher_bytes, str):
                cipher_bytes = cipher_bytes.encode("utf-8")
            return cipher.decrypt(cipher_bytes).decode("utf-8")
        except Exception as e:
            raise RuntimeError("Failed to decrypt text") from e
