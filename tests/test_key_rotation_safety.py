"""A key rotation that fails partway must leave the database readable.

Each test builds a real SQLCipher database, makes one step of
``SessionCrypto.rotate_key`` fail, and then opens the database the way the app
does: with whatever key ``get_cipher()`` finds in the keyring or key file.
"""

import keyring
import pytest
from cryptography.fernet import Fernet

import app.core.crypto as crypto
import app.core.db_conn as db_conn
from app.core.crypto import CryptoManager, SessionCrypto
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.exceptions import CryptoError

NEW_KEY = Fernet.generate_key().decode("utf-8")


@pytest.fixture
def encrypted_db(tmp_path):
    db_path = tmp_path / "docs.db"
    key_path = tmp_path / "secret.key"
    session = SessionCrypto(key_path, db_path)
    session.get_cipher()
    clear_connection_cache(only_current_and_inactive=False)
    conn = get_db_connection(str(db_path))
    conn.execute("CREATE TABLE docs (name TEXT)")
    conn.execute("INSERT INTO docs VALUES ('report.pdf')")
    conn.commit()
    clear_connection_cache(only_current_and_inactive=False)
    assert not db_path.read_bytes().startswith(b"SQLite format 3"), "not encrypted"
    yield db_path, key_path, session
    clear_connection_cache(only_current_and_inactive=False)


def _stored_key(db_path, key_path):
    """The key a fresh start would load."""
    fresh = SessionCrypto(key_path, db_path)
    fresh.get_cipher()
    return fresh._key


def _rows(db_path):
    """Open the database fresh, as the next app start would."""
    clear_connection_cache(only_current_and_inactive=False)
    try:
        return (
            get_db_connection(str(db_path)).execute("SELECT name FROM docs").fetchall()
        )
    finally:
        clear_connection_cache(only_current_and_inactive=False)


def _fail(message):
    def raiser(*_args, **_kwargs):
        raise OSError(message)

    return raiser


def test_rotation_keeps_the_database_readable(encrypted_db):
    db_path, key_path, _session = encrypted_db
    before = _stored_key(db_path, key_path)

    CryptoManager.rotate_database_key(db_path, key_path, new_key_str=NEW_KEY)

    assert _stored_key(db_path, key_path) == NEW_KEY.encode("utf-8") != before
    assert _rows(db_path) == [("report.pdf",)]


def test_failed_rekey_raises_and_keeps_the_old_key(encrypted_db, monkeypatch):
    db_path, key_path, session = encrypted_db
    before = _stored_key(db_path, key_path)
    with monkeypatch.context() as m:
        m.setattr(db_conn, "escape_pragma_key", _fail("rekey failed"))
        with pytest.raises(CryptoError, match="rekey failed"):
            session.rotate_key(new_key_str=NEW_KEY)

    assert _stored_key(db_path, key_path) == before
    assert _rows(db_path) == [("report.pdf",)]


def test_failed_key_file_write_undoes_the_rekey(encrypted_db, monkeypatch):
    db_path, key_path, session = encrypted_db
    before = _stored_key(db_path, key_path)
    real_write = SessionCrypto._write_isolated_key

    def disk_full_for_new_key(self, key):
        if key == NEW_KEY.encode("utf-8"):
            raise OSError("No space left on device")
        return real_write(self, key)

    with monkeypatch.context() as m:
        m.setattr(SessionCrypto, "_write_isolated_key", disk_full_for_new_key)
        with pytest.raises(CryptoError, match="rotation was undone"):
            session.rotate_key(new_key_str=NEW_KEY)

    assert _stored_key(db_path, key_path) == before
    assert _rows(db_path) == [("report.pdf",)]


def test_unwritable_keyring_falls_back_to_the_key_file(encrypted_db, monkeypatch):
    """A keyring that refuses writes but can drop the old entry is fine."""
    db_path, key_path, session = encrypted_db
    with monkeypatch.context() as m:
        m.setattr(crypto.keyring, "set_password", _fail("keyring locked"))
        session.rotate_key(new_key_str=NEW_KEY)

    assert (
        keyring.get_password(session.keyring_service, session.keyring_account) is None
    )
    assert _stored_key(db_path, key_path) == NEW_KEY.encode("utf-8")
    assert _rows(db_path) == [("report.pdf",)]


def test_keyring_stuck_on_the_old_key_undoes_the_rotation(encrypted_db, monkeypatch):
    """get_cipher() reads the keyring first, so a stale entry there would win."""
    db_path, key_path, session = encrypted_db
    before = _stored_key(db_path, key_path)
    with monkeypatch.context() as m:
        m.setattr(crypto.keyring, "set_password", _fail("keyring locked"))
        m.setattr(crypto.keyring, "delete_password", _fail("keyring locked"))
        with pytest.raises(CryptoError, match="rotation was undone"):
            session.rotate_key(new_key_str=NEW_KEY)

    assert _stored_key(db_path, key_path) == before
    assert _rows(db_path) == [("report.pdf",)]
