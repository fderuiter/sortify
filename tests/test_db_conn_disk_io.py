"""A "disk I/O error" while opening an encrypted database.

On Windows a file lock can make SQLCipher report "disk I/O error" for a
database it could otherwise decrypt. ``get_db_connection`` then opens a
copy of the file to tell a lock from a wrong key: a copy that opens means
the original error was transient and is re-raised as is; a copy that fails
to decrypt means the key is wrong, reported as a decryption failure.
"""

import os
from unittest.mock import patch

import pytest

from app.core import db_conn
from app.core.crypto import SessionCrypto
from app.core.db_conn import clear_connection_cache, get_db_connection

pytestmark = pytest.mark.skipif(
    not db_conn.HAS_SQLCIPHER, reason="the copy check runs only with SQLCipher"
)


@pytest.fixture
def encrypted_db(tmp_path):
    """A real encrypted database with a table and a WAL sidecar beside it."""
    db_file = tmp_path / "docs.db"
    key_file = tmp_path / "docs.key"
    key_file.write_text("disk-io-test-key", encoding="utf-8")
    crypto = SessionCrypto(key_file, db_file)
    with patch("app.core.path_utils.resolve_db_crypto", return_value=crypto):
        clear_connection_cache(only_current_and_inactive=False)
        conn = get_db_connection(str(db_file))
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('kept')")
        conn.commit()
        clear_connection_cache(only_current_and_inactive=False)
    # Leave a sidecar so the copy step copies it too; an empty WAL is valid.
    (tmp_path / "docs.db-wal").write_bytes(b"")
    yield db_file, crypto
    clear_connection_cache(only_current_and_inactive=False)


def _same(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _fail_on(db_file, error, copy_error=None):
    """Make connect fail for db_file, and optionally for the copy as well."""
    real_connect = db_conn.sqlite3.connect
    copies = []

    def connect(path, *args, **kwargs):
        if _same(path, db_file):
            raise error
        # Other databases (the app's own ledger, say) open as usual.
        if db_conn.Path(path).name == "temp_db.db":
            copies.append(path)
            if copy_error is not None:
                raise copy_error
        return real_connect(path, *args, **kwargs)

    return connect, copies


def test_lock_error_is_raised_as_is_when_the_copy_opens(encrypted_db):
    db_file, crypto = encrypted_db
    connect, copies = _fail_on(
        db_file, db_conn.sqlite3.OperationalError("disk I/O error")
    )

    with (
        patch("app.core.path_utils.resolve_db_crypto", return_value=crypto),
        patch.object(db_conn.sqlite3, "connect", connect),
        pytest.raises(db_conn.sqlite3.OperationalError, match="disk I/O error"),
    ):
        get_db_connection(str(db_file))

    assert len(copies) == 1
    # The copy lived in a temporary folder that is gone again.
    assert not db_conn.Path(copies[0]).parent.exists()


def test_copy_that_fails_to_decrypt_reports_a_decryption_failure(encrypted_db):
    db_file, crypto = encrypted_db
    connect, copies = _fail_on(
        db_file,
        db_conn.sqlite3.OperationalError("disk I/O error"),
        copy_error=db_conn.sqlite3.DatabaseError("file is not a database"),
    )

    with (
        patch("app.core.path_utils.resolve_db_crypto", return_value=crypto),
        patch.object(db_conn.sqlite3, "connect", connect),
        pytest.raises(db_conn.sqlite3.DatabaseError, match="Failed to decrypt"),
    ):
        get_db_connection(str(db_file))

    assert len(copies) == 1


def test_database_still_opens_after_the_check(encrypted_db):
    db_file, crypto = encrypted_db
    connect, _copies = _fail_on(
        db_file, db_conn.sqlite3.OperationalError("disk I/O error")
    )
    with (
        patch("app.core.path_utils.resolve_db_crypto", return_value=crypto),
        patch.object(db_conn.sqlite3, "connect", connect),
        pytest.raises(db_conn.sqlite3.OperationalError),
    ):
        get_db_connection(str(db_file))

    with patch("app.core.path_utils.resolve_db_crypto", return_value=crypto):
        conn = get_db_connection(str(db_file))
        assert conn.execute("SELECT v FROM t").fetchall() == [("kept",)]
