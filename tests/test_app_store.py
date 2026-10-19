"""Tests for the one app store and its import of old session databases (ADR 0008)."""

import logging

import pytest

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
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


def test_newer_session_wins_when_two_indexed_the_same_file(
    tmp_path, worker, base_dir
):
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
    (sessions / "s1" / "autosorter.db").unlink()

    store = open_store(tmp_path / "app", legacy_session_root=sessions)

    conn = get_db_connection(str(store.path))
    assert conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    assert _count(store, "snapshot_documents") == 0
