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
        self.key_path = key_path
        self.db_path = db_path
        self._cipher = None
        self.recovery_code = None
        self.keyring_service = "AutoSorter"
        db_hash = hashlib.md5(str(db_path).encode("utf-8")).hexdigest()
        self.keyring_account = f"DatabaseDecryptionKey_{db_hash}"

    def get_cipher(self):
        """Get or initialize the Fernet cipher instance."""
        if self._cipher is not None:
            return self._cipher

        key = None

        # 1. Try loading from Keyring
        try:
            key_str = keyring.get_password(self.keyring_service, self.keyring_account)
            if key_str:
                key = key_str.encode("utf-8")
        except Exception:
            pass

        # 2. Check for Legacy Plaintext File
        if key is None and self.key_path.exists():
            try:
                with open(self.key_path, "rb") as f:
                    key = f.read().strip()

                # Try to migrate to keyring
                try:
                    keyring.set_password(
                        self.keyring_service, self.keyring_account, key.decode("utf-8")
                    )
                    verify_str = keyring.get_password(
                        self.keyring_service, self.keyring_account
                    )
                    if verify_str and verify_str.encode("utf-8") == key:
                        os.unlink(self.key_path)  # Securely delete legacy file
                except Exception:
                    pass  # Fallback to keeping it in the file
            except Exception:
                pass

        # 3. Database Guard
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

            # 4. Generate new key from a new 24-character recovery code
            self.recovery_code = self.generate_recovery_code()
            key = self.derive_key(self.recovery_code)
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
                # Fallback to local file with strict permissions
                fd = os.open(
                    str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(key)

        if key is None:
            raise RuntimeError("Database accessed but key file is missing.")

        try:
            self._cipher = Fernet(key)
            return self._cipher
        except Exception as e:
            raise RuntimeError(
                "Database accessed but key file is missing or invalid."
            ) from e

    @staticmethod
    def generate_recovery_code() -> str:
        """Generate a random 24-character alphanumeric recovery code."""
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(24))

    @staticmethod
    def derive_key(recovery_code: str) -> bytes:
        """Derive a 32-byte Fernet key from the 24-character recovery code."""
        import base64
        import hashlib

        digest = hashlib.sha256(recovery_code.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)

    def verify_recovery_code(self, recovery_code: str) -> bool:
        """Verify if the 24-character recovery code correctly decrypts the database."""
        if len(recovery_code) != 24:
            return False
        try:
            key_bytes = self.derive_key(recovery_code)
            key_str = key_bytes.decode("utf-8")

            if not self.db_path.exists():
                return True

            from app.core.db_conn import sqlite3

            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute(f"PRAGMA key = '{key_str}'")
                conn.execute("PRAGMA user_version")
                return True
            except sqlite3.DatabaseError:
                return False
            finally:
                conn.close()
        except Exception:
            return False

    def save_recovered_key(self, recovery_code: str):
        """Save the key derived from the recovery code to the keyring/file."""
        key_bytes = self.derive_key(recovery_code)
        saved_to_keyring = False
        try:
            keyring.set_password(
                self.keyring_service, self.keyring_account, key_bytes.decode("utf-8")
            )
            verify_str = keyring.get_password(
                self.keyring_service, self.keyring_account
            )
            if verify_str and verify_str.encode("utf-8") == key_bytes:
                saved_to_keyring = True
        except Exception:
            pass

        if not saved_to_keyring:
            # Fallback to local file with strict permissions
            fd = os.open(
                str(self.key_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                if self.key_path.exists()
                else os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "wb") as f:
                f.write(key_bytes)

        self._cipher = None

    def get_raw_key(self) -> str:
        """Get the raw key string for SQLCipher."""
        if self._cipher is None:
            self.get_cipher()
        # Since _cipher is initialized, we can fetch it again using get_cipher logic, but we need the raw bytes.
        # Actually, self._cipher._signing_key + self._cipher._encryption_key is the raw key, but let's just
        # extract it the same way.
        key = None
        try:
            key_str = keyring.get_password(self.keyring_service, self.keyring_account)
            if key_str:
                key = key_str.encode("utf-8")
        except Exception:
            pass
        if key is None and self.key_path.exists():
            with open(self.key_path, "rb") as f:
                key = f.read().strip()
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
