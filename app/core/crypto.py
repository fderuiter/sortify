"""Cryptographic management for envelope encryption."""

import hashlib
import os

try:
    import sqlite3
except Exception:
    try:
        from sqlcipher3 import dbapi2 as sqlite3
    except Exception:
        sqlite3 = None
from pathlib import Path

import keyring
from cryptography.fernet import Fernet


def get_fallback_keys_dir() -> Path:
    """Get the centralized fallback key store directory based on OS.

    Windows: %APPDATA%/Sortify/keys
    POSIX: ~/.sortify/keys
    """
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Sortify" / "keys"

    # Non-Windows or APPDATA not defined on Windows
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        try:
            home = str(Path.home())
        except Exception:
            home = os.path.expanduser("~")
    return Path(home) / ".sortify" / "keys"


def secure_delete_file(file_path: Path):
    """Overwrite a file with zeros and delete it securely."""
    if not file_path.exists():
        return
    try:
        if file_path.is_file():
            size = file_path.stat().st_size
            if size > 0:
                with open(file_path, "wb") as f:
                    f.write(b"\x00" * size)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
            file_path.unlink()
    except Exception:
        try:
            file_path.unlink()
        except Exception:
            pass


def secure_delete_dir(dir_path: Path):
    """Recursively secure delete files in a directory and then delete the directory."""
    if not dir_path.exists():
        return
    try:
        for item in list(dir_path.iterdir()):
            if item.is_file():
                secure_delete_file(item)
            elif item.is_dir():
                secure_delete_dir(item)
        dir_path.rmdir()
    except Exception:
        import shutil
        shutil.rmtree(dir_path, ignore_errors=True)


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
        
        # Centralized key store location under user's home directory / APPDATA
        self.isolated_dir = get_fallback_keys_dir()
        self.isolated_key_path = self.isolated_dir / f"{self.db_path.name}_{db_hash}.key"
        
        # Legacy key paths for migration
        self.legacy_isolated_dir = self.db_path.parent / ".keys"
        self.legacy_isolated_key_path = self.legacy_isolated_dir / f"{self.db_path.name}.key"

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

        # 2. Centralized Fallback Key Lookup
        if key is None and self.isolated_key_path.exists():
            try:
                with open(self.isolated_key_path, "rb") as f:
                    key = f.read().strip()
            except Exception:
                pass

        # 3. Legacy Fallback Migration and Cleanup
        legacy_key = None
        if self.legacy_isolated_key_path.exists():
            try:
                with open(self.legacy_isolated_key_path, "rb") as f:
                    legacy_key = f.read().strip()
            except Exception:
                pass

        if legacy_key is None and self.legacy_isolated_dir.exists() and self.legacy_isolated_dir.is_dir():
            try:
                for p in self.legacy_isolated_dir.iterdir():
                    if p.is_file() and p.suffix == ".key":
                        try:
                            with open(p, "rb") as f:
                                legacy_key = f.read().strip()
                                if legacy_key:
                                    break
                        except Exception:
                            pass
            except Exception:
                pass

        if legacy_key is None and self.key_path.exists():
            try:
                with open(self.key_path, "rb") as f:
                    legacy_key = f.read().strip()
            except Exception:
                pass

        if legacy_key:
            # If we didn't find a key in the keyring or centralized store, use the legacy key.
            if key is None:
                key = legacy_key

            # Write/copy to the centralized fallback key path
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

            # Try to migrate to keyring
            try:
                keyring.set_password(
                    self.keyring_service,
                    self.keyring_account,
                    legacy_key.decode("utf-8"),
                )
            except Exception:
                pass

        # Always clean up legacy fallback keys if they exist (even if they weren't used to load the key)
        if self.legacy_isolated_dir.exists():
            try:
                secure_delete_dir(self.legacy_isolated_dir)
            except Exception:
                pass

        # 4. Database Guard Check
        if key is None:
            if self.db_path.exists():
                try:
                    from contextlib import closing

                    has_docs = False
                    with closing(sqlite3.connect(str(self.db_path))) as conn:
                        with closing(conn.cursor()) as cursor:
                            cursor.execute(
                                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='documents'"
                            )
                            row = cursor.fetchone()
                            if row and row[0] > 0:
                                cursor.execute("SELECT count(*) FROM documents")
                                d_row = cursor.fetchone()
                                if d_row and d_row[0] > 0:
                                    has_docs = True
                    if has_docs:
                        raise RuntimeError("Database accessed but key file is missing.")
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

    def encrypt_vector(self, text: str) -> bytes:
        """Encrypt a vector string and return bytes."""
        if text is None:
            return None
        cipher = self.get_cipher()
        return cipher.encrypt(text.encode("utf-8"))

    def decrypt_vector(self, cipher_bytes: bytes) -> str:
        """Decrypt vector bytes and return the original string."""
        if cipher_bytes is None:
            return None
        cipher = self.get_cipher()
        try:
            if isinstance(cipher_bytes, str):
                cipher_bytes = cipher_bytes.encode("utf-8")
            return cipher.decrypt(cipher_bytes).decode("utf-8")
        except Exception as e:
            raise RuntimeError("Failed to decrypt vector") from e
