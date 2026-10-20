"""Tests for the move ledger and the mover writing it (ADR 0006, step 2 of #714)."""

import hashlib
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

import app.core.resilient_file_ops as resilient_file_ops
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import (
    HISTORY_DB_MIGRATIONS,
    UNDO_SNAPSHOT_STATUS,
    HistoryManager,
)
from app.core.migrations import migrate, schema_version
from app.core.mover import execute_moves
from app.core.moves import MoveLedger, import_step_ledger
from app.core.store import STORE_MIGRATIONS, open_store


@pytest.fixture
def env(tmp_path):
    base_dir = tmp_path / "Downloads"
    base_dir.mkdir()
    worker = DBWorker()
    db = Database(tmp_path / "autosorter.db", worker)
    cache = CacheManager(str(tmp_path / "cache.db"), worker)
    history = HistoryManager(db, cache, str(tmp_path / "history.db"))
    yield base_dir, db, history
    worker.stop()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan(folder, *names):
    return {
        folder: {
            name: {
                "__type__": "file",
                "relative_source": f"../{name}",
                "status": "To Be Sorted",
            }
            for name in names
        }
    }


def _only_session(history):
    """The sort's session, leaving out the safety snapshot a rollback takes."""
    (session,) = [
        s for s in history.get_sessions() if s["status"] != UNDO_SNAPSHOT_STATUS
    ]
    return session["session_id"]


def test_sort_records_each_move_in_order_and_verifies_its_hash(env):
    base_dir, db, history = env
    contents = {"a.txt": b"alpha bytes", "b.txt": b"beta bytes, longer"}
    for name, data in contents.items():
        (base_dir / name).write_bytes(data)

    execute_moves(str(base_dir), _plan("Docs", *contents), db, history)

    moves = history.moves.list_moves(_only_session(history))
    assert [m.seq for m in moves] == [1, 2]
    by_name = {os.path.basename(m.src): m for m in moves}
    assert set(by_name) == set(contents)
    for name, data in contents.items():
        move = by_name[name]
        assert move.src == str(base_dir / name)
        assert move.dst == str(base_dir / "Docs" / name)
        assert (base_dir / "Docs" / name).read_bytes() == data
        assert move.src_sha256 == _sha256(data)
        assert move.size == len(data)
        assert move.displaced is None
        assert move.state == "verified"


def test_intent_is_committed_before_the_move_and_marked_failed_after(env, monkeypatch):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"keep me")
    real_move = resilient_file_ops.resilient_move
    seen_on_disk = []

    def failing_move(src, dst, *args, **kwargs):
        if str(src) == str(base_dir / "a.txt"):
            # Read on this thread's own connection: only committed rows show.
            conn = get_db_connection(history.db_path)
            seen_on_disk.extend(conn.execute("SELECT src, state FROM moves").fetchall())
            raise OSError("disk unplugged")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(resilient_file_ops, "resilient_move", failing_move)

    with pytest.raises(Exception):
        execute_moves(str(base_dir), _plan("Docs", "a.txt"), db, history)

    assert seen_on_disk == [(str(base_dir / "a.txt"), "intent")]
    (move,) = history.moves.list_moves(_only_session(history))
    assert move.state == "failed"
    assert "disk unplugged" in move.error
    assert (base_dir / "a.txt").read_bytes() == b"keep me"


def test_overwrite_records_where_the_existing_file_was_moved_aside(env):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"new")
    (base_dir / "Docs").mkdir()
    (base_dir / "Docs" / "a.txt").write_bytes(b"old")

    execute_moves(
        str(base_dir),
        _plan("Docs", "a.txt"),
        db,
        history,
        runtime_settings=SimpleNamespace(CONFLICT_POLICY="overwrite"),
    )

    (move,) = history.moves.list_moves(_only_session(history))
    assert move.dst == str(base_dir / "Docs" / "a.txt")
    assert move.state == "verified"
    assert move.displaced
    with open(move.displaced, "rb") as f:
        assert f.read() == b"old"


def test_ledger_refuses_a_state_change_out_of_order(env):
    _, _, history = env
    seq = history.moves.record_intent("s1", "/x/a", "/x/b/a", "abc", 3)

    with pytest.raises(ValueError):
        history.moves.mark_verified("s1", seq)
    history.moves.mark_done("s1", seq, "/x/b/a (1)")
    history.moves.mark_verified("s1", seq)
    with pytest.raises(ValueError):
        history.moves.mark_failed("s1", seq)

    (move,) = history.moves.list_moves("s1")
    assert (move.seq, move.dst, move.state) == (1, "/x/b/a (1)", "verified")
    assert history.moves.record_intent("s1", "/x/c", "/x/b/c") == 2
    assert history.moves.record_intent("s2", "/x/d", "/x/b/d") == 1


def _v1_history_with_steps(conn):
    migrate(conn, HISTORY_DB_MIGRATIONS[:1], label="history")
    steps = [
        ("s1", "/d/a.txt", "/d/A/a.txt", "h-a", 10.0),
        ("s2", "/d/x.txt", "/d/X/x.txt", "h-x", 11.0),
        ("s1", "/d/b.txt", "/d/B/b.txt", "h-b", 12.0),
    ]
    conn.executemany(
        "INSERT INTO step_ledger (session_id, source_path, target_path, "
        "original_path, file_hash, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        [(sid, src, dst, src, h, ts) for sid, src, dst, h, ts in steps],
    )
    conn.commit()


def test_history_migration_imports_the_step_ledger_once(tmp_path):
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        _v1_history_with_steps(conn)

        assert migrate(conn, HISTORY_DB_MIGRATIONS, label="history") == 2
        migrate(conn, HISTORY_DB_MIGRATIONS, label="history")
        assert import_step_ledger(conn) == 0

        rows = conn.execute(
            "SELECT session_id, seq, src, dst, src_sha256, state, created_at "
            "FROM moves ORDER BY session_id, seq"
        ).fetchall()
    assert rows == [
        ("s1", 1, "/d/a.txt", "/d/A/a.txt", "h-a", "done", 10.0),
        ("s1", 2, "/d/b.txt", "/d/B/b.txt", "h-b", "done", 12.0),
        ("s2", 1, "/d/x.txt", "/d/X/x.txt", "h-x", "done", 11.0),
    ]


def test_store_migration_adds_moves_and_imports_its_steps(tmp_path):
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    conn = get_db_connection(str(store.path))
    assert schema_version(conn) == STORE_MIGRATIONS[-1].version >= 2
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='moves'"
    ).fetchone()

    with closing(sqlite3.connect(tmp_path / "v1.db")) as old:
        migrate(old, STORE_MIGRATIONS[:1], label="store")
        old.execute(
            "INSERT INTO step_ledger (session_id, source_path, target_path, "
            "file_hash, timestamp) VALUES ('s1', '/d/a', '/d/A/a', 'h', 1.0)"
        )
        old.commit()
        migrate(old, STORE_MIGRATIONS, label="store")
        assert old.execute("SELECT seq, src, dst, state FROM moves").fetchall() == [
            (1, "/d/a", "/d/A/a", "done")
        ]


def test_store_builds_moves_for_old_sessions_it_imports(tmp_path):
    session_dir = tmp_path / "sessions" / "s"
    session_dir.mkdir(parents=True)
    with closing(sqlite3.connect(session_dir / "history.db")) as conn:
        _v1_history_with_steps(conn)

    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "sessions")

    ledger = MoveLedger(str(store.path))
    assert [(m.seq, m.src, m.state) for m in ledger.list_moves("s1")] == [
        (1, "/d/a.txt", "done"),
        (2, "/d/b.txt", "done"),
    ]
    assert [m.src for m in ledger.list_moves("s2")] == ["/d/x.txt"]


# Step 3 of #714: undo replays the moves table.


def _cut_off_after_intent(history, session_id, seq):
    """Put a move back to its intent row, as a crash right after the move leaves it."""
    conn = get_db_connection(history.db_path)
    with conn:
        conn.execute(
            "UPDATE moves SET state = 'intent' WHERE session_id = ? AND seq = ?",
            (session_id, seq),
        )


def test_undo_restores_a_move_a_crash_cut_off_before_its_update(env):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"alpha")
    (base_dir / "b.txt").write_bytes(b"beta")
    execute_moves(str(base_dir), _plan("Docs", "a.txt", "b.txt"), db, history)
    session_id = _only_session(history)
    _cut_off_after_intent(history, session_id, 2)

    history.rollback(session_id)

    assert (base_dir / "a.txt").read_bytes() == b"alpha"
    assert (base_dir / "b.txt").read_bytes() == b"beta"
    assert not (base_dir / "Docs").exists()


def test_undo_finds_a_cut_off_move_that_landed_under_another_name(env):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"alpha")
    execute_moves(str(base_dir), _plan("Docs", "a.txt"), db, history)
    session_id = _only_session(history)
    _cut_off_after_intent(history, session_id, 1)
    # The move found its target taken and picked a suffixed name.
    os.rename(base_dir / "Docs" / "a.txt", base_dir / "Docs" / "a_1.txt")
    (base_dir / "Docs" / "a.txt").write_bytes(b"someone else's file")

    history.rollback(session_id)

    assert (base_dir / "a.txt").read_bytes() == b"alpha"
    assert (base_dir / "Docs" / "a.txt").read_bytes() == b"someone else's file"
    assert not (base_dir / "Docs" / "a_1.txt").exists()


def test_undo_leaves_a_file_whose_intent_never_moved_it(env):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"alpha")
    execute_moves(str(base_dir), _plan("Docs", "a.txt"), db, history)
    session_id = _only_session(history)
    # A crash before the second move ran: its source is untouched.
    (base_dir / "c.txt").write_bytes(b"gamma")
    (base_dir / "Docs" / "c.txt").write_bytes(b"gamma")
    history.moves.record_intent(
        session_id,
        str(base_dir / "c.txt"),
        str(base_dir / "Docs" / "c.txt"),
        src_sha256=_sha256(b"gamma"),
        size=5,
    )

    history.rollback(session_id)

    assert (base_dir / "a.txt").read_bytes() == b"alpha"
    assert (base_dir / "c.txt").read_bytes() == b"gamma"
    assert (base_dir / "Docs" / "c.txt").read_bytes() == b"gamma"


def test_overwrite_journals_the_displaced_path_before_moving_it(env, monkeypatch):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"new")
    (base_dir / "Docs").mkdir()
    (base_dir / "Docs" / "a.txt").write_bytes(b"old")
    real_move = resilient_file_ops.resilient_move
    seen_on_disk = []

    def watching_move(src, dst, *args, **kwargs):
        if str(src) == str(base_dir / "Docs" / "a.txt"):
            conn = get_db_connection(history.db_path)
            seen_on_disk.extend(
                conn.execute("SELECT state, displaced FROM moves").fetchall()
            )
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(resilient_file_ops, "resilient_move", watching_move)

    execute_moves(
        str(base_dir),
        _plan("Docs", "a.txt"),
        db,
        history,
        runtime_settings=SimpleNamespace(CONFLICT_POLICY="overwrite"),
    )

    ((state, displaced),) = seen_on_disk
    assert state == "intent"
    (move,) = history.moves.list_moves(_only_session(history))
    assert displaced == move.displaced

    history.rollback(_only_session(history))

    assert (base_dir / "a.txt").read_bytes() == b"new"
    assert (base_dir / "Docs" / "a.txt").read_bytes() == b"old"


def test_a_file_never_moves_without_its_intent_row(env, monkeypatch):
    base_dir, db, history = env
    (base_dir / "a.txt").write_bytes(b"alpha")

    def no_ledger(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(history.moves, "record_intent", no_ledger)

    summary = None
    try:
        summary = execute_moves(str(base_dir), _plan("Docs", "a.txt"), db, history)
    except Exception:
        pass

    assert (base_dir / "a.txt").read_bytes() == b"alpha", summary
    assert not (base_dir / "Docs" / "a.txt").exists()
