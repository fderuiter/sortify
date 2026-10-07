"""Centralized cryptographic security and key rotation management.

Provides administrative security interfaces for database encryption key rotation,
key inspection, and key material export.
"""

import os
from pathlib import Path
from typing import Dict, Optional, Union

from app.config import get_app_dir
from app.core.crypto import SessionCrypto
from app.core.license import (
    generate_license_key,
    mask_license_key,
    validate_license_key,
)


def get_key_info(db_path: Optional[Union[str, Path]] = None) -> Dict[str, str]:
    """Retrieve database encryption key location and storage backend status.

    Args:
        db_path: Optional custom path to target database file. Defaults to application database.

    Returns
    -------
        Dict containing key path, storage backend name, and resolved database path.
    """
    resolved_db = (
        Path(db_path).resolve()
        if db_path
        else (get_app_dir() / "autosorter.db").resolve()
    )
    key_path = resolved_db.parent / f"{resolved_db.name}.key"
    session_crypto = SessionCrypto(key_path=key_path, db_path=resolved_db)

    key_exists = session_crypto.isolated_key_path.exists()
    keyring_val = None
    try:
        import keyring

        keyring_val = keyring.get_password(
            session_crypto.keyring_service, session_crypto.keyring_account
        )
    except Exception:
        pass

    storage_backend = (
        "keyring" if keyring_val else ("file" if key_exists else "generated")
    )

    return {
        "status": "success",
        "key_path": str(session_crypto.isolated_key_path),
        "storage_backend": storage_backend,
        "db_path": str(resolved_db),
    }


def rotate_database_key(
    db_path: Optional[Union[str, Path]] = None,
    new_key: Optional[str] = None,
) -> str:
    """Safely rotate and re-encrypt database encryption key.

    Args:
        db_path: Optional custom path to target database file.
        new_key: Optional explicit new key string. Generates new Fernet key if None.

    Returns
    -------
        The new encryption key string.
    """
    resolved_db = (
        Path(db_path).resolve()
        if db_path
        else (get_app_dir() / "autosorter.db").resolve()
    )
    key_path = resolved_db.parent / f"{resolved_db.name}.key"
    session_crypto = SessionCrypto(key_path=key_path, db_path=resolved_db)

    return session_crypto.rotate_key(new_key_str=new_key)


def export_database_key(
    db_path: Optional[Union[str, Path]] = None,
    output_path: Optional[Union[str, Path]] = None,
) -> str:
    """Retrieve raw database encryption key material and optionally write to output file.

    Args:
        db_path: Optional custom path to target database file.
        output_path: Optional target file path to export key string to.

    Returns
    -------
        The raw encryption key string.
    """
    resolved_db = (
        Path(db_path).resolve()
        if db_path
        else (get_app_dir() / "autosorter.db").resolve()
    )
    key_path = resolved_db.parent / f"{resolved_db.name}.key"
    session_crypto = SessionCrypto(key_path=key_path, db_path=resolved_db)
    raw_key = session_crypto.get_raw_key()
    if raw_key is None:
        raise RuntimeError(f"No encryption key found for database at {resolved_db}")

    if output_path:
        p = Path(output_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw_key.encode("utf-8"))
        except OSError:
            with open(p, "w", encoding="utf-8") as f:
                f.write(raw_key)

    return raw_key


__all__ = [
    "get_key_info",
    "rotate_database_key",
    "export_database_key",
    "mask_license_key",
    "generate_license_key",
    "validate_license_key",
]
