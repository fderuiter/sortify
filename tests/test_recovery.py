from unittest.mock import MagicMock

from app.core.crypto import SessionCrypto

# Import sqlite3 from db_conn to ensure we use SQLCipher-enabled sqlite3 if available
from app.core.db_conn import sqlite3
from app.ui.recovery import show_recovery_code_onboarding, show_recovery_screen


def test_recovery_code_generation_and_derivation():
    # 1. Recovery code generation
    code = SessionCrypto.generate_recovery_code()
    assert len(code) == 24
    assert code.isalnum()

    # Different calls generate different codes
    code2 = SessionCrypto.generate_recovery_code()
    assert code != code2

    # 2. Key derivation
    key1 = SessionCrypto.derive_key(code)
    key2 = SessionCrypto.derive_key(code)
    assert key1 == key2  # Must be deterministic
    assert len(key1) == 44  # Valid Fernet key length (base64 of 32 bytes is 44 chars)


def test_recovery_code_verification(tmp_path):
    db_path = tmp_path / "autosorter.db"
    key_path = tmp_path / "secret.key"

    # Generate a code and key
    code = SessionCrypto.generate_recovery_code()
    key = SessionCrypto.derive_key(code).decode("utf-8")

    # Write an encrypted database using SQLCipher-enabled sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"PRAGMA key = '{key}'")
        conn.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO test_table (id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()

    crypto = SessionCrypto(key_path, db_path)

    # Verify matching code
    assert crypto.verify_recovery_code(code) is True

    # Verify invalid codes
    assert crypto.verify_recovery_code("invalidcode") is False
    assert crypto.verify_recovery_code("A" * 24) is False


def test_save_recovered_key(tmp_path):
    db_path = tmp_path / "autosorter.db"
    key_path = tmp_path / "secret.key"

    code = SessionCrypto.generate_recovery_code()
    crypto = SessionCrypto(key_path, db_path)

    # Save key
    crypto.save_recovered_key(code)

    # The raw key should match the derived key
    expected_key = SessionCrypto.derive_key(code).decode("utf-8")
    assert crypto.get_raw_key() == expected_key


def test_ui_onboarding_loading(monkeypatch):
    """Ensure show_recovery_code_onboarding can be called with mocked nicegui UI elements."""
    mock_dialog_obj = MagicMock()
    mock_dialog_context = MagicMock()
    # When ui.dialog() is called, return mock_dialog_context
    # When context is entered with "with ui.dialog() as dialog:", __enter__ returns mock_dialog_obj
    mock_dialog_context.__enter__.return_value = mock_dialog_obj

    mock_card = MagicMock()
    mock_label = MagicMock()
    mock_row = MagicMock()
    mock_button = MagicMock()

    # Mock NiceGUI ui.dialog, ui.card, ui.label, ui.row, ui.button
    from nicegui import ui

    monkeypatch.setattr(ui, "dialog", MagicMock(return_value=mock_dialog_context))
    monkeypatch.setattr(ui, "card", MagicMock(return_value=mock_card))
    monkeypatch.setattr(ui, "label", MagicMock(return_value=mock_label))
    monkeypatch.setattr(ui, "row", MagicMock(return_value=mock_row))
    monkeypatch.setattr(ui, "button", MagicMock(return_value=mock_button))

    parent_app = MagicMock()
    show_recovery_code_onboarding(parent_app, "ABC123XYZ456789012345678")

    assert ui.dialog.called
    assert mock_dialog_obj.open.called


def test_ui_recovery_screen_loading(monkeypatch, tmp_path):
    """Ensure show_recovery_screen can be called with mocked nicegui UI elements."""
    mock_dialog_obj = MagicMock()
    mock_dialog_context = MagicMock()
    mock_dialog_context.__enter__.return_value = mock_dialog_obj

    mock_card = MagicMock()
    mock_label = MagicMock()
    mock_row = MagicMock()
    mock_button = MagicMock()
    mock_input = MagicMock()

    from nicegui import ui

    monkeypatch.setattr(ui, "dialog", MagicMock(return_value=mock_dialog_context))
    monkeypatch.setattr(ui, "card", MagicMock(return_value=mock_card))
    monkeypatch.setattr(ui, "label", MagicMock(return_value=mock_label))
    monkeypatch.setattr(ui, "row", MagicMock(return_value=mock_row))
    monkeypatch.setattr(ui, "button", MagicMock(return_value=mock_button))
    monkeypatch.setattr(ui, "input", MagicMock(return_value=mock_input))

    parent_app = MagicMock()
    db_path = tmp_path / "autosorter.db"

    show_recovery_screen(parent_app, "session-123", {}, "resume", db_path)

    assert ui.dialog.called
    assert mock_dialog_obj.open.called
