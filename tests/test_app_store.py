"""Tests for the one app store and its import of old session databases (ADR 0008)."""

import logging

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.migrations import StoreTooNewError, schema_version
from app.core.path_utils import resolve_db_crypto
from app.core.store import STORE_FILENAME, STORE_MIGRATIONS, open_store


@pytest.fixture
def worker():
    w = DBWorker()
    yield w
    w.stop()


@pytest.fixture
def base_dir(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "invoice.txt").write_text("invoice 42")
    return str(folder)


def _legacy_session(session_dir, worker, base_dir, text):
    """Write a session folder the way older versions did: one database of each kind."""
    session_dir.mkdir(parents=True)
    db = Database(session_dir / "autosorter.db", worker)
    cache = CacheManager(str(session_dir / "cache.db"), worker)
    history = HistoryManager(db, cache, str(session_dir / "history.db"))
    db.upsert_document(base_dir, "invoice.txt", "hash-1", text)
    db.set_user_verified_target_path(base_dir, "invoice.txt", "Finance")
    session_id = history.create_snapshot(base_dir)
    history.log_step(session_id, f"{base_dir}/invoice.txt", f"{base_dir}/Finance/x")
    return session_id


def _count(store, table):
    conn = get_db_connection(str(store.path))
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_new_store_has_every_table_at_the_head_version(tmp_path):
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")

    assert store.path == tmp_path / "app" / STORE_FILENAME
    conn = get_db_connection(str(store.path))
    assert schema_version(conn) == STORE_MIGRATIONS[-1].version
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "documents",
        "sessions",
        "snapshot_files",
        "step_ledger",
        "directory_cache",
        "quarantine_records",
        "legacy_imports",
    } <= tables


def test_managers_share_the_store_without_changing_its_version(tmp_path, worker):
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")

    db = store.database(worker)
    cache = store.cache(worker)
    history = store.history(db, cache)

    assert db.db_path == cache.db_path == history.db_path == str(store.path)
    conn = get_db_connection(str(store.path))
    assert schema_version(conn) == STORE_MIGRATIONS[-1].version


def test_imports_old_sessions_once_and_reencrypts_text(tmp_path, worker, base_dir):
    sessions = tmp_path / "sessions"
    session_id = _legacy_session(sessions / "s1", worker, base_dir, "invoice 42")

    store = open_store(tmp_path / "app", legacy_session_root=sessions)
    db = store.database(worker)

    docs = {d[0]: d for d in db.get_all_documents(base_dir)}
    assert docs["invoice.txt"][1] == "invoice 42"

    conn = get_db_connection(str(store.path))
    assert conn.execute(
        "SELECT user_verified_target_path FROM documents WHERE filepath = 'invoice.txt'"
    ).fetchone() == ("Finance",)
    assert conn.execute(
        "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert _count(store, "snapshot_files") == 1
    assert _count(store, "step_ledger") == 1

    store_crypto = resolve_db_crypto(store.path)
    (snapshot_text,) = conn.execute(
        "SELECT extracted_text FROM snapshot_documents WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert store_crypto.decrypt_text(snapshot_text) == "invoice 42"

    open_store(tmp_path / "app", legacy_session_root=sessions)
    assert _count(store, "sessions") == 1
    assert _count(store, "snapshot_files") == 1
    assert _count(store, "step_ledger") == 1


def test_newer_session_wins_when_two_indexed_the_same_file(tmp_path, worker, base_dir):
    import os

    sessions = tmp_path / "sessions"
    _legacy_session(sessions / "old", worker, base_dir, "old text")
    _legacy_session(sessions / "new", worker, base_dir, "new text")
    for name, mtime in (("old", 1_000_000), ("new", 2_000_000)):
        for db_file in (sessions / name).glob("*.db"):
            os.utime(db_file, (mtime, mtime))

    store = open_store(tmp_path / "app", legacy_session_root=sessions)

    docs = {d[0]: d for d in store.database(worker).get_all_documents(base_dir)}
    assert docs["invoice.txt"][1] == "new text"
    assert _count(store, "sessions") == 2


def test_unreadable_session_database_is_skipped_and_retried(
    tmp_path, worker, base_dir, caplog
):
    sessions = tmp_path / "sessions"
    _legacy_session(sessions / "good", worker, base_dir, "invoice 42")
    broken = sessions / "broken"
    broken.mkdir()
    (broken / "history.db").write_bytes(b"not a database" * 100)

    with caplog.at_level(logging.WARNING):
        store = open_store(tmp_path / "app", legacy_session_root=sessions)

    assert _count(store, "sessions") == 1
    assert "Could not import" in caplog.text
    conn = get_db_connection(str(store.path))
    imported = {r[0] for r in conn.execute("SELECT source_path FROM legacy_imports")}
    assert str((broken / "history.db").resolve()) not in imported


def test_refuses_a_store_written_by_a_newer_version(tmp_path):
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    conn = get_db_connection(str(store.path))
    conn.execute(f"PRAGMA user_version = {STORE_MIGRATIONS[-1].version + 1}")
    conn.commit()

    with pytest.raises(StoreTooNewError):
        open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")


def test_text_without_its_key_is_left_for_re_extraction(tmp_path, worker, base_dir):
    sessions = tmp_path / "sessions"
    session_id = _legacy_session(sessions / "s1", worker, base_dir, "invoice 42")
    # Windows can't delete a database file while a connection to it is open.
    clear_connection_cache(only_current_and_inactive=False)
    (sessions / "s1" / "autosorter.db").unlink()

    store = open_store(tmp_path / "app", legacy_session_root=sessions)

    conn = get_db_connection(str(store.path))
    assert conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    # Row metadata is preserved in snapshot_documents with extracted_text = None
    assert _count(store, "snapshot_documents") == 1
    (snapshot_text,) = conn.execute(
        "SELECT extracted_text FROM snapshot_documents WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert snapshot_text is None
    # Audit record created in quarantine_records with status DECRYPTION_FAILED
    assert _count(store, "quarantine_records") == 1
    q_row = conn.execute(
        "SELECT status, policy_action, original_filepath FROM quarantine_records"
    ).fetchone()
    assert q_row[0] == "DECRYPTION_FAILED"
    assert q_row[1] == "legacy_migration_reencryption"
    assert q_row[2] == "invoice.txt"


def test_reencryption_failure_preserves_metadata_and_quarantines(
    tmp_path, worker, base_dir
):
    """Corrupted ciphertexts preserve metadata in documents/snapshot_documents and create quarantine records."""
    sessions = tmp_path / "sessions"
    session_id = _legacy_session(sessions / "s1", worker, base_dir, "good text")

    # Corrupt extracted_text in autosorter.db and history.db to cause CryptoError on decrypt
    clear_connection_cache(only_current_and_inactive=False)
    conn_main = get_db_connection(str(sessions / "s1" / "autosorter.db"))
    conn_main.execute("UPDATE documents SET extracted_text = X'1234567890ABCDEF'")
    conn_main.commit()

    conn_hist = get_db_connection(str(sessions / "s1" / "history.db"))
    conn_hist.execute(
        "UPDATE snapshot_documents SET extracted_text = X'1234567890ABCDEF'"
    )
    conn_hist.commit()
    clear_connection_cache(only_current_and_inactive=False)

    store = open_store(tmp_path / "app", legacy_session_root=sessions)
    conn = get_db_connection(str(store.path))

    # Verify documents metadata preserved
    doc_row = conn.execute(
        "SELECT filepath, file_hash, extracted_text, user_verified_target_path FROM documents WHERE filepath = 'invoice.txt'"
    ).fetchone()
    assert doc_row is not None
    assert doc_row[0] == "invoice.txt"
    assert doc_row[1] == "hash-1"
    assert doc_row[2] is None  # extracted_text set to None
    assert doc_row[3] == "Finance"  # user_verified_target_path intact

    # Verify snapshot_documents metadata preserved
    snap_row = conn.execute(
        "SELECT filepath, file_hash, extracted_text FROM snapshot_documents WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert snap_row is not None
    assert snap_row[0] == "invoice.txt"
    assert snap_row[1] == "hash-1"
    assert snap_row[2] is None

    # Verify quarantine_records created for both decryption failures
    q_rows = conn.execute(
        "SELECT status, original_filepath, policy_action, error_message FROM quarantine_records WHERE status = 'DECRYPTION_FAILED'"
    ).fetchall()
    assert len(q_rows) == 2
    for r in q_rows:
        assert r[0] == "DECRYPTION_FAILED"
        assert r[1] == "invoice.txt"
        assert r[2] == "legacy_migration_reencryption"
        assert "Failed to decrypt extracted_text" in r[3]
