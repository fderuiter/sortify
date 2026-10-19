"""Tests for the numbered migration runner (ADR 0008)."""

import sqlite3
from contextlib import closing

import pytest

from app.core.db import MAIN_DB_MIGRATIONS
from app.core.migrations import (
    Migration,
    StoreTooNewError,
    migrate,
    schema_version,
    table_columns,
)


def _tables(conn):
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


@pytest.fixture
def conn(tmp_path):
    with closing(sqlite3.connect(tmp_path / "store.db")) as c:
        yield c


def _create(name):
    return lambda c: c.execute(f"CREATE TABLE {name} (x INTEGER)")


def test_applies_pending_migrations_in_order_once(conn):
    applied = []

    def record(version):
        def apply(c):
            applied.append(version)
            c.execute(f"CREATE TABLE t{version} (x INTEGER)")

        return apply

    migrations = [Migration(v, f"m{v}", record(v)) for v in (1, 2, 3)]

    assert migrate(conn, migrations[:2], label="test") == 2
    assert migrate(conn, migrations, label="test") == 3
    assert migrate(conn, migrations, label="test") == 3

    assert applied == [1, 2, 3]
    assert schema_version(conn) == 3
    assert {"t1", "t2", "t3"} <= _tables(conn)


def test_failed_migration_rolls_back_and_keeps_version(conn):
    def half_then_fail(c):
        c.execute("CREATE TABLE half (x INTEGER)")
        raise RuntimeError("boom")

    migrations = [
        Migration(1, "ok", _create("ok")),
        Migration(2, "bad", half_then_fail),
    ]

    with pytest.raises(RuntimeError, match="boom"):
        migrate(conn, migrations, label="test")

    assert schema_version(conn) == 1
    assert "ok" in _tables(conn)
    assert "half" not in _tables(conn)


def test_refuses_store_newer_than_code(conn):
    conn.execute("PRAGMA user_version = 9")
    with pytest.raises(StoreTooNewError, match="version 9"):
        migrate(conn, [Migration(1, "a", _create("a"))], label="test")
    assert schema_version(conn) == 9
    assert "a" not in _tables(conn)


@pytest.mark.parametrize("versions", [[2, 1], [1, 1], [0, 1], []])
def test_rejects_badly_numbered_lists(conn, versions):
    migrations = [
        Migration(v, str(v), _create(f"t{i}")) for i, v in enumerate(versions)
    ]
    with pytest.raises(ValueError):
        migrate(conn, migrations, label="test")


def test_main_baseline_upgrades_legacy_v6_database(conn):
    # A version 6 database from before the rating and model_signature columns.
    conn.execute(
        "CREATE TABLE documents (base_dir TEXT, filepath TEXT, file_hash TEXT, "
        "extracted_text TEXT, user_verified_target_path TEXT, "
        "PRIMARY KEY (base_dir, filepath))"
    )
    conn.execute(
        "CREATE TABLE document_vectors (base_dir TEXT, filepath TEXT, vector TEXT, "
        "PRIMARY KEY (base_dir, filepath))"
    )
    conn.execute("INSERT INTO documents VALUES ('b', 'f.txt', 'h', 't', NULL)")
    conn.execute("PRAGMA user_version = 6")
    conn.commit()

    assert migrate(conn, MAIN_DB_MIGRATIONS, label="main") == 7

    assert "rating" in table_columns(conn, "documents")
    assert "model_signature" in table_columns(conn, "document_vectors")
    assert {"quarantine_records", "jev_classification_cache"} <= _tables(conn)
    assert conn.execute("SELECT filepath FROM documents").fetchall() == [("f.txt",)]
