"""Cryptographic management for envelope encryption."""

import hashlib
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any

import keyring
from cryptography.fernet import Fernet

from app.core.exceptions import CryptoError

logger = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:
    np = None

try:
    import sqlite3
except Exception:
    try:
        from sqlcipher3 import dbapi2 as sqlite3
    except Exception:
        sqlite3 = None


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
        except Exception as e:
            logger.debug(f"Path.home() resolution failed in get_fallback_keys_dir: {e}")
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
                    except OSError as fsync_err:
                        logger.debug(f"fsync failed during secure file deletion of '{file_path}': {fsync_err}")
            file_path.unlink()
    except Exception as e:
        logger.warning(f"Failed to overwrite file '{file_path}' during secure deletion: {e}")
        try:
            file_path.unlink()
        except OSError as unlink_err:
            logger.warning(f"Failed to unlink file '{file_path}' after overwrite failure: {unlink_err}")


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
    except Exception as e:
        logger.warning(f"Failed to securely delete directory '{dir_path}', using rmtree fallback: {e}")
        import shutil

        shutil.rmtree(dir_path, ignore_errors=True)


class SessionCrypto:
    """Manages encryption and decryption of data per session."""

    def __init__(self, key_path: Path, db_path: Path):
        import threading

        self.db_path = Path(os.path.abspath(db_path))
        self.key_path = Path(os.path.abspath(key_path))
        self._cipher = None
        self._key = None
        self.keyring_service = "AutoSorter"
        db_hash = hashlib.md5(str(self.db_path).encode("utf-8")).hexdigest()
        self.keyring_account = f"DatabaseDecryptionKey_{db_hash}"
        self._vector_cache_max_entries = 10000
        self._vector_parsed_cache = {}
        self._vector_decrypt_lock = threading.Lock()

        # Centralized key store location under user's home directory / APPDATA
        self.isolated_dir = get_fallback_keys_dir()
        self.isolated_key_path = (
            self.isolated_dir / f"{self.db_path.name}_{db_hash}.key"
        )

        # Legacy key paths for migration
        self.legacy_isolated_dir = self.db_path.parent / ".keys"
        self.legacy_isolated_key_path = (
            self.legacy_isolated_dir / f"{self.db_path.name}.key"
        )

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
        except Exception as e:
            logger.warning(f"Keyring lookup failed for account '{self.keyring_account}': {e}")

        # 2. Centralized Fallback Key Lookup
        if key is None and self.isolated_key_path.exists():
            try:
                with open(self.isolated_key_path, "rb") as f:
                    key = f.read().strip()
            except OSError as e:
                logger.warning(f"Failed to read isolated key at '{self.isolated_key_path}': {e}")

        # 3. Legacy Fallback Migration and Cleanup
        legacy_key = None
        if self.legacy_isolated_key_path.exists():
            try:
                with open(self.legacy_isolated_key_path, "rb") as f:
                    legacy_key = f.read().strip()
            except OSError as e:
                logger.warning(f"Failed to read legacy isolated key at '{self.legacy_isolated_key_path}': {e}")

        if (
            legacy_key is None
            and self.legacy_isolated_dir.exists()
            and self.legacy_isolated_dir.is_dir()
        ):
            try:
                for p in self.legacy_isolated_dir.iterdir():
                    if p.is_file() and p.suffix == ".key":
                        try:
                            with open(p, "rb") as f:
                                legacy_key = f.read().strip()
                                if legacy_key:
                                    break
                        except OSError as e:
                            logger.debug(f"Failed to read legacy key candidate '{p}': {e}")
            except OSError as e:
                logger.warning(f"Failed to iterate legacy isolated key directory '{self.legacy_isolated_dir}': {e}")

        if legacy_key is None and self.key_path.exists():
            try:
                with open(self.key_path, "rb") as f:
                    legacy_key = f.read().strip()
            except OSError as e:
                logger.warning(f"Failed to read key path '{self.key_path}': {e}")

        if legacy_key:
            # If we didn't find a key in the keyring or centralized store, use the legacy key.
            if key is None:
                key = legacy_key

            # Write/copy to the centralized fallback key path
            self.isolated_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.isolated_dir, 0o700)
            except OSError as chmod_err:
                logger.debug(f"Failed to set permissions on isolated directory '{self.isolated_dir}': {chmod_err}")

            try:
                fd = os.open(
                    str(self.isolated_key_path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o600,
                )
                with os.fdopen(fd, "wb") as f:
                    f.write(legacy_key)
            except OSError as e:
                logger.warning(f"fdopen failed writing key to '{self.isolated_key_path}', falling back to open: {e}")
                with open(self.isolated_key_path, "wb") as f:
                    f.write(legacy_key)
                try:
                    os.chmod(self.isolated_key_path, 0o600)
                except OSError as chmod_err:
                    logger.debug(f"Failed to set permissions on key file '{self.isolated_key_path}': {chmod_err}")

            # Try to migrate to keyring
            try:
                keyring.set_password(
                    self.keyring_service,
                    self.keyring_account,
                    legacy_key.decode("utf-8"),
                )
            except Exception as e:
                logger.warning(f"Failed to set keyring password during legacy migration: {e}")

        # Always clean up legacy fallback keys if they exist (even if they weren't used to load the key)
        if self.legacy_isolated_dir.exists():
            try:
                secure_delete_dir(self.legacy_isolated_dir)
            except Exception as e:
                logger.warning(f"Failed to clean up legacy key directory '{self.legacy_isolated_dir}': {e}")

        # 4. Database Guard Check
        if key is None:
            if (
                self.db_path.exists()
                and self.db_path.stat().st_size > 0
                and self.db_path.suffix.lower()
                not in (
                    ".json",
                    ".txt",
                    ".log",
                )
            ):
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
                        raise CryptoError("Database accessed but key file is missing.")
                except sqlite3.DatabaseError:
                    # If it's encrypted with SQLCipher, sqlite3 will fail with "file is not a database"
                    # which means it's an existing DB! We cannot read it without a key.
                    raise CryptoError("Database accessed but key file is missing.")
                except sqlite3.Error as e:
                    logger.debug(f"SQLite error checking unencrypted database guard: {e}")

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
            except Exception as e:
                logger.warning(f"Failed to store generated key in OS keyring: {e}")

            if not saved_to_keyring:
                # Fallback to isolated fallback key path with secure permissions
                self.isolated_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    os.chmod(self.isolated_dir, 0o700)
                except OSError as chmod_err:
                    logger.debug(f"Failed to set permissions on isolated directory '{self.isolated_dir}': {chmod_err}")

                try:
                    fd = os.open(
                        str(self.isolated_key_path),
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                        0o600,
                    )
                    with os.fdopen(fd, "wb") as f:
                        f.write(key)
                except OSError as e:
                    logger.warning(f"fdopen failed writing generated key to '{self.isolated_key_path}', falling back to open: {e}")
                    with open(self.isolated_key_path, "wb") as f:
                        f.write(key)
                    try:
                        os.chmod(self.isolated_key_path, 0o600)
                    except OSError as chmod_err:
                        logger.debug(f"Failed to set permissions on key file '{self.isolated_key_path}': {chmod_err}")

        if key is None:
            raise CryptoError("Database accessed but key file is missing.")

        try:
            self._key = key
            self._cipher = Fernet(key)
            return self._cipher
        except Exception as e:
            raise CryptoError(
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
        except Exception as e:
            logger.warning(f"Keyring lookup failed in get_raw_key: {e}")
        if key is None and self.isolated_key_path.exists():
            try:
                with open(self.isolated_key_path, "rb") as f:
                    key = f.read().strip()
            except OSError as e:
                logger.warning(f"Failed to read isolated key in get_raw_key: {e}")
        if key is None and self.key_path.exists():
            try:
                with open(self.key_path, "rb") as f:
                    key = f.read().strip()
            except OSError as e:
                logger.warning(f"Failed to read key_path in get_raw_key: {e}")
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
            raise CryptoError("Failed to decrypt text") from e

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
        if isinstance(cipher_bytes, str):
            cipher_bytes = cipher_bytes.encode("utf-8")
        cipher = self.get_cipher()
        try:
            return cipher.decrypt(cipher_bytes).decode("utf-8")
        except Exception as e:
            raise CryptoError("Failed to decrypt vector") from e

    def decrypt_and_parse_vector(self, cipher_bytes: bytes):
        """Decrypt vector bytes, parse as JSON, and return list of floats."""
        if cipher_bytes is None:
            return None
        if isinstance(cipher_bytes, str):
            cipher_bytes = cipher_bytes.encode("utf-8")
        with self._vector_decrypt_lock:
            cached = self._vector_parsed_cache.get(cipher_bytes)
        if cached is not None:
            return cached

        cipher = self.get_cipher()
        try:
            decrypted_str = cipher.decrypt(cipher_bytes).decode("utf-8")
            import json

            parsed = json.loads(decrypted_str)
            with self._vector_decrypt_lock:
                if len(self._vector_parsed_cache) >= self._vector_cache_max_entries:
                    self._vector_parsed_cache.pop(next(iter(self._vector_parsed_cache)))
                self._vector_parsed_cache[cipher_bytes] = parsed
            return parsed
        except Exception as e:
            logger.warning(f"Failed to decrypt or parse vector: {e}")
            return None


class VectorBuffer:
    """Mutable byte buffer wrapping floating-point vector arrays with zero-filling cleanup support."""

    def __init__(self, vector: Any):
        self._buffer: bytearray | None = None
        self._dim: int = 0

        if vector is not None:
            if isinstance(vector, VectorBuffer):
                if vector._buffer is not None:
                    self._buffer = bytearray(vector._buffer)
                    self._dim = vector._dim
            elif np is not None and isinstance(vector, np.ndarray):
                arr = vector.astype(np.float32)
                self._dim = len(arr)
                self._buffer = bytearray(arr.tobytes())
            elif isinstance(vector, (list, tuple)):
                self._dim = len(vector)
                if self._dim > 0:
                    self._buffer = bytearray(struct.pack(f"{self._dim}f", *vector))
                else:
                    self._buffer = bytearray()
            elif isinstance(vector, bytearray):
                self._buffer = bytearray(vector)
                self._dim = len(vector) // 4
            elif isinstance(vector, bytes):
                self._buffer = bytearray(vector)
                self._dim = len(vector) // 4

    def __len__(self) -> int:
        """Return vector dimension count."""
        return self._dim

    def __getitem__(self, idx: Any) -> Any:
        """Retrieve element or slice from vector buffer."""
        if self._buffer is None:
            raise IndexError("Vector buffer has been zeroed/cleared")
        if isinstance(idx, slice):
            floats = self.to_list()
            return floats[idx]
        if idx < 0:
            idx += self._dim
        if idx < 0 or idx >= self._dim:
            raise IndexError("Vector index out of range")
        return struct.unpack_from("f", self._buffer, idx * 4)[0]

    def __iter__(self):
        """Iterate over vector float values."""
        return iter(self.to_list())

    def to_list(self) -> list[float]:
        """Convert byte buffer to list of float values."""
        if self._buffer is None or len(self._buffer) == 0:
            return []
        return list(struct.unpack(f"{self._dim}f", self._buffer))

    def to_numpy(self) -> Any:
        """Convert byte buffer to NumPy float32 array."""
        if np is None:
            raise RuntimeError("NumPy is not installed")
        if self._buffer is None or len(self._buffer) == 0:
            return np.array([], dtype=np.float32)
        return np.frombuffer(bytes(self._buffer), dtype=np.float32)

    def zero_fill(self) -> None:
        """Overwrite the mutable byte buffer with null bytes before clearing reference."""
        if self._buffer is not None:
            for i in range(len(self._buffer)):
                self._buffer[i] = 0
            self._buffer = None
            self._dim = 0

    def is_zeroed(self) -> bool:
        """Return True if backing buffer has been zeroed and cleared."""
        return self._buffer is None


def zero_vector_buffer(target: Any) -> None:
    """Recursively or directly zero-fill vector buffers or arrays."""
    if target is None:
        return
    if isinstance(target, VectorBuffer):
        target.zero_fill()
    elif isinstance(target, bytearray):
        for i in range(len(target)):
            target[i] = 0
    elif np is not None and isinstance(target, np.ndarray):
        target.fill(0)
    elif isinstance(target, (list, tuple, set)):
        for item in target:
            zero_vector_buffer(item)
    elif isinstance(target, dict):
        for k, v in list(target.items()):
            zero_vector_buffer(v)


def _json_default(obj: Any) -> Any:
    """JSON default encoder helper for converting non-primitive Python types to JSON lists."""
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if hasattr(obj, "to_list") and callable(obj.to_list):
        return obj.to_list()
    if hasattr(obj, "tolist") and callable(obj.tolist):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class EphemeralSessionCrypto:
    """Manages ephemeral session encryption for inter-process communication (IPC)."""

    def __init__(self, session_key: bytes | str | None = None):
        if session_key is None:
            self.session_key = Fernet.generate_key()
        elif isinstance(session_key, str):
            self.session_key = session_key.encode("utf-8")
        else:
            self.session_key = session_key
        self._cipher = Fernet(self.session_key)

    def encrypt_payload(self, payload: Any) -> bytes:
        """Serialize and encrypt a data payload."""
        if self._cipher is None:
            raise ValueError("Ephemeral session key has been purged")
        serialized = json.dumps(payload, default=_json_default).encode("utf-8")
        return self._cipher.encrypt(serialized)

    def decrypt_payload(self, encrypted_bytes: bytes) -> Any:
        """Decrypt and deserialize a data payload."""
        if self._cipher is None:
            raise ValueError("Ephemeral session key has been purged")
        decrypted_bytes = self._cipher.decrypt(encrypted_bytes)
        try:
            decrypted_str = decrypted_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid UTF-8 sequence for JSON payload: {e}",
                doc=decrypted_bytes.decode("utf-8", errors="replace"),
                pos=0,
            ) from e
        return json.loads(decrypted_str)

    def purge(self) -> None:
        """Purge the session key."""
        self.session_key = None
        self._cipher = None


def encrypt_ipc_payload(payload: Any, session_key: bytes | str) -> bytes:
    """Encrypt an IPC queue payload with an ephemeral session key."""
    if session_key is None:
        raise ValueError("Session key cannot be None")
    if isinstance(session_key, str):
        session_key = session_key.encode("utf-8")
    cipher = Fernet(session_key)
    serialized = json.dumps(payload, default=_json_default).encode("utf-8")
    return cipher.encrypt(serialized)


def decrypt_ipc_payload(encrypted_bytes: bytes, session_key: bytes | str) -> Any:
    """Decrypt an IPC queue payload with an ephemeral session key."""
    if session_key is None:
        raise ValueError("Session key cannot be None")
    if isinstance(session_key, str):
        session_key = session_key.encode("utf-8")
    cipher = Fernet(session_key)
    decrypted_bytes = cipher.decrypt(encrypted_bytes)
    try:
        decrypted_str = decrypted_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid UTF-8 sequence for JSON payload: {e}",
            doc=decrypted_bytes.decode("utf-8", errors="replace"),
            pos=0,
        ) from e
    return json.loads(decrypted_str)

