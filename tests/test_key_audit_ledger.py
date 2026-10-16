"""Tests for Key Audit Ledger in sidecar SQLite database."""

import time
from unittest.mock import patch

import pytest

from app.core.crypto import CryptoManager, SessionCrypto
from app.core.db_conn import get_db_connection
from app.core.ledger import TransactionLedger, sanitize_audit_metadata
from app.core.text_utils import contains_secrets


def test_key_audit_ledger_schema(tmp_path):
    """Verify that key_audit_ledger table is initialized with required schema columns and indices."""
    ledger_path = str(tmp_path / "transaction_ledger.db")

    # Initialize ledger DB
    TransactionLedger(ledger_path)

    conn = get_db_connection(ledger_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(key_audit_ledger)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}

    expected_columns = {
        "event_id": "TEXT",
        "event_type": "TEXT",
        "db_path_hash": "TEXT",
        "session_id": "TEXT",
        "status": "TEXT",
        "created_at": "REAL",
        "metadata_json": "TEXT",
    }

    for col_name, col_type in expected_columns.items():
        assert col_name in columns, f"Missing column {col_name} in key_audit_ledger"
        assert columns[col_name].upper() == col_type, f"Column {col_name} type mismatch"

    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indices = {row[0] for row in cursor.fetchall()}

    assert "idx_key_audit_session" in indices
    assert "idx_key_audit_event_type" in indices
    assert "idx_key_audit_db_hash" in indices
    assert "idx_key_audit_created_at" in indices


def test_rotate_database_key_audit_recording(tmp_path):
    """Verify that CryptoManager.rotate_database_key records a KEY_ROTATION audit entry."""
    db_path = tmp_path / "app_data.db"
    key_path = tmp_path / "secret.key"
    ledger_path = str(tmp_path / "transaction_ledger.db")

    # Create dummy database
    conn = get_db_connection(str(db_path))
    with conn:
        conn.execute("CREATE TABLE test (id INT)")

    # Initialize session crypto
    session_crypto = SessionCrypto(key_path, db_path)
    session_crypto.get_cipher()

    with patch("app.core.ledger.get_default_ledger_path", return_value=ledger_path):
        new_key = CryptoManager.rotate_database_key(
            db_path,
            key_path,
            session_id="session_rot_101",
            actor="admin_user",
            caller_metadata={"policy": "compliance_q3"},
        )
        assert new_key is not None

        ledger = TransactionLedger(ledger_path)
        events = ledger.get_key_audit_events(session_id="session_rot_101")

        assert len(events) >= 1
        event = events[0]
        assert event["event_type"] == "KEY_ROTATION"
        assert event["status"] == "SUCCESS"
        assert event["db_path_hash"] == CryptoManager.derive_db_hash(db_path)
        assert event["session_id"] == "session_rot_101"
        assert event["metadata"]["actor"] == "admin_user"
        assert event["metadata"]["policy"] == "compliance_q3"


def test_key_rotation_failure_audit_recording(tmp_path):
    """Verify that key rotation failures write a KEY_ROTATION entry with FAILED status."""
    db_path = tmp_path / "protected_db.db"
    key_path = tmp_path / "secret.key"
    ledger_path = str(tmp_path / "transaction_ledger.db")

    crypto = SessionCrypto(key_path, db_path)
    crypto.get_cipher()

    with patch("app.core.ledger.get_default_ledger_path", return_value=ledger_path):
        with patch("cryptography.fernet.Fernet.generate_key", side_effect=ValueError("Crypto entropy exhaustion")):
            with pytest.raises(ValueError, match="Crypto entropy exhaustion"):
                crypto.rotate_key(
                    session_id="failed_rot_session",
                    actor="automated_agent",
                    caller_metadata={"trigger": "schedule"},
                )

        ledger = TransactionLedger(ledger_path)
        events = ledger.get_key_audit_events(session_id="failed_rot_session")

        assert len(events) == 1
        event = events[0]
        assert event["event_type"] == "KEY_ROTATION"
        assert event["status"] == "FAILED"
        assert "Crypto entropy exhaustion" in event["metadata"]["error"]


def test_get_raw_key_export_audit_recording(tmp_path):
    """Verify executing SessionCrypto.get_raw_key emits a KEY_EXPORT audit entry."""
    db_path = tmp_path / "backup_target.db"
    key_path = tmp_path / "secret.key"
    ledger_path = str(tmp_path / "transaction_ledger.db")

    crypto = SessionCrypto(key_path, db_path)
    crypto.get_cipher()

    with patch("app.core.ledger.get_default_ledger_path", return_value=ledger_path):
        raw_key = crypto.get_raw_key(
            session_id="export_session_202",
            actor="backup_service",
            caller_context={"reason": "offline_disaster_recovery"},
        )
        assert raw_key is not None

        ledger = TransactionLedger(ledger_path)
        events = ledger.get_key_audit_events(session_id="export_session_202")

        assert len(events) >= 1
        export_event = events[0]
        assert export_event["event_type"] == "KEY_EXPORT"
        assert export_event["status"] == "SUCCESS"
        assert export_event["db_path_hash"] == CryptoManager.derive_db_hash(db_path)
        assert export_event["metadata"]["actor"] == "backup_service"
        assert export_event["metadata"]["reason"] == "offline_disaster_recovery"


def test_audit_records_contain_no_plain_text_keys_or_secrets(tmp_path):
    """Verify automated sanitization ensures no plain-text cryptographic keys appear in ledger DB files."""
    db_path = tmp_path / "sensitive_db.db"
    key_path = tmp_path / "secret.key"
    ledger_path = str(tmp_path / "transaction_ledger.db")

    crypto = SessionCrypto(key_path, db_path)
    raw_key = crypto.get_raw_key()

    sensitive_caller_metadata = {
        "raw_key": raw_key,
        "fernet_key": "gAAAAABk1234567890abcdefsecretkeybytes==",
        "password": "SuperSecretPassphrase123!",
        "auth_token": "ghp_123456789012345678901234567890123456",
        "safe_note": "Regular non-sensitive operational note",
    }

    with patch("app.core.ledger.get_default_ledger_path", return_value=ledger_path):
        crypto.rotate_key(
            session_id="sanitization_session",
            actor="admin",
            caller_metadata=sensitive_caller_metadata,
        )

        ledger = TransactionLedger(ledger_path)
        events = ledger.get_key_audit_events(session_id="sanitization_session")
        assert len(events) == 1
        meta = events[0]["metadata"]

        # Sensitive keys must be redacted
        assert meta["raw_key"] == "[REDACTED]"
        assert meta["fernet_key"] == "[REDACTED]"
        assert meta["password"] == "[REDACTED]"
        assert meta["auth_token"] == "[REDACTED]"
        assert meta["safe_note"] == "Regular non-sensitive operational note"

        # Inspect raw database metadata text
        conn = get_db_connection(ledger_path)
        cursor = conn.cursor()
        cursor.execute("SELECT metadata_json FROM key_audit_ledger")
        db_rows = cursor.fetchall()

        for (meta_str,) in db_rows:
            assert raw_key not in meta_str
            assert "SuperSecretPassphrase123!" not in meta_str
            assert "ghp_" not in meta_str
            assert not contains_secrets(meta_str)


def test_sanitize_audit_metadata_unit():
    """Unit test for metadata sanitization helper."""
    input_meta = {
        "User_Key": "secret123",
        "API_SECRET": "sk_live_123456789012345678901234",
        "nested": {
            "token": "bearer_token_xyz",
            "normal_field": "hello world",
        },
        "list_field": ["ghp_123456789012345678901234567890123456", "clean_value"],
        "key_path": "/path/to/key.key",
    }

    sanitized = sanitize_audit_metadata(input_meta)
    assert sanitized["User_Key"] == "[REDACTED]"
    assert sanitized["API_SECRET"] == "[REDACTED]"
    assert sanitized["nested"]["token"] == "[REDACTED]"
    assert sanitized["nested"]["normal_field"] == "hello world"
    assert sanitized["list_field"][0] == "[REDACTED]"
    assert sanitized["list_field"][1] == "clean_value"
    assert sanitized["key_path"] == "/path/to/key.key"


def test_query_key_audit_events_filtering(tmp_path):
    """Verify historical event querying by session_id, event_type, db_path, and time range."""
    ledger_path = str(tmp_path / "transaction_ledger.db")
    ledger = TransactionLedger(ledger_path)

    hash1 = CryptoManager.derive_db_hash("/db/path1.db")
    hash2 = CryptoManager.derive_db_hash("/db/path2.db")

    t0 = time.time() - 100.0
    t1 = time.time() - 50.0
    t2 = time.time()

    ledger.log_key_audit(
        "KEY_ROTATION", hash1, session_id="s1", status="SUCCESS", created_at=t0
    )
    ledger.log_key_audit(
        "KEY_EXPORT", hash1, session_id="s1", status="SUCCESS", created_at=t1
    )
    ledger.log_key_audit(
        "KEY_ROTATION", hash2, session_id="s2", status="FAILED", created_at=t2
    )

    # Filter by event_type
    rotations = ledger.get_key_audit_events(event_type="KEY_ROTATION")
    assert len(rotations) == 2
    assert all(r["event_type"] == "KEY_ROTATION" for r in rotations)

    # Filter by db_path_hash
    p1_events = ledger.get_key_audit_events(db_path_hash=hash1)
    assert len(p1_events) == 2

    # Filter by db_path
    p2_events = ledger.get_key_audit_events(db_path="/db/path2.db")
    assert len(p2_events) == 1
    assert p2_events[0]["session_id"] == "s2"

    # Filter by time range
    time_filtered = ledger.get_key_audit_events(start_time=t0 - 1, end_time=t1 + 1)
    assert len(time_filtered) == 2


def test_audit_logging_failure_graceful_handling(tmp_path):
    """Verify failure in log_key_audit does not interrupt cryptographic operations."""
    db_path = tmp_path / "test.db"
    key_path = tmp_path / "secret.key"

    crypto = SessionCrypto(key_path, db_path)

    with patch(
        "app.core.ledger.TransactionLedger.log_key_audit",
        side_effect=RuntimeError("Sidecar DB disk full"),
    ):
        raw_key = crypto.get_raw_key()
        assert raw_key is not None

        new_key = crypto.rotate_key()
        assert new_key is not None
