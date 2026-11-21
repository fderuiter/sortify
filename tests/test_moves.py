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
    HistoryManager,
)
from app.core.migrations import migrate, schema_version
from app.core.mover import execute_moves
from app.core.moves import MoveLedger, decode_move_failure, import_step_ledger
from app.core.session_catalog import list_sessions
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
    """Return the sole recorded sort session id."""
    (session,) = history.get_sessions()
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

        assert (
            migrate(conn, HISTORY_DB_MIGRATIONS, label="history")
            == HISTORY_DB_MIGRATIONS[-1].version
        )
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


def test_sort_and_undo_leave_superseded_stores_empty_and_skip_undo_snapshot(tmp_path):
    """Sorting and undoing write only canonical sessions and moves, creating no legacy rows or sidecar files."""
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        cache = store.cache(worker)
        history = store.history(db, cache)

        base_dir = tmp_path / "Downloads"
        base_dir.mkdir()
        (base_dir / "report.txt").write_bytes(b"quarterly numbers")

        execute_moves(str(base_dir), _plan("Reports", "report.txt"), db, history)
        sessions = history.get_sessions()
        assert len(sessions) == 1
        sid = sessions[0]["session_id"]
        assert sessions[0]["status"] == "completed"
        assert sessions[0]["step_count"] == 1
        assert sessions[0]["file_count"] == 1

        conn = get_db_connection(str(store.path))
        for legacy_table in (
            "snapshot_files",
            "snapshot_cache",
            "snapshot_documents",
            "step_ledger",
        ):
            assert (
                conn.execute(f"SELECT COUNT(*) FROM {legacy_table}").fetchone()[0] == 0
            )
        assert not (tmp_path / "app" / "transaction_ledger.db").exists()
        assert not (base_dir / "rollback_journal.json").exists()

        # Undo replays moves without creating any additional undo_snapshot session row
        history.rollback(sid)
        sessions_after = history.get_sessions()
        assert len(sessions_after) == 1
        assert sessions_after[0]["status"] == "rolled_back"
        assert (base_dir / "report.txt").read_bytes() == b"quarterly numbers"
    finally:
        worker.stop()


def test_store_upgrade_imports_step_ledger_main_tx_ledger_and_sidecar_and_undoes(
    tmp_path,
):
    """Upgrading a v2 store imports step_ledger, main transaction_ledger, and sidecar transaction_ledger.db."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    base_dir = tmp_path / "Downloads"
    (base_dir / "Docs").mkdir(parents=True)
    (base_dir / "Docs" / "from_step.txt").write_bytes(b"step data")
    (base_dir / "Docs" / "from_main_tx.txt").write_bytes(b"main tx data")
    (base_dir / "Docs" / "from_sidecar.txt").write_bytes(b"sidecar data")

    # Seed a v2 sortify.db with un-imported step_ledger and main-DB transaction_ledger
    with closing(sqlite3.connect(app_dir / "sortify.db")) as conn:
        migrate(conn, STORE_MIGRATIONS[:2], label="store")
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("s-step", 10.0, str(base_dir)),
        )
        conn.execute(
            "INSERT INTO step_ledger (session_id, source_path, target_path, file_hash, timestamp) "
            "VALUES (?, ?, ?, ?, 10.0)",
            (
                "s-step",
                str(base_dir / "from_step.txt"),
                str(base_dir / "Docs" / "from_step.txt"),
                _sha256(b"step data"),
            ),
        )
        conn.execute(
            "INSERT INTO transaction_ledger "
            "(session_id, step_number, base_dir, source_path, destination_path, file_hash, item_type, timestamp) "
            "VALUES (?, 1, ?, 'from_main_tx.txt', 'Docs/from_main_tx.txt', ?, 'file', 20.0)",
            (
                "s-main-tx",
                str(base_dir),
                _sha256(b"main tx data"),
            ),
        )
        conn.commit()

    # Seed a legacy sidecar transaction_ledger.db next to sortify.db using the exact historical schema
    with closing(sqlite3.connect(app_dir / "transaction_ledger.db")) as sidecar:
        sidecar.execute(
            "CREATE TABLE transaction_ledger ("
            "entry_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, base_dir TEXT NOT NULL, "
            "source_path TEXT NOT NULL, dest_path TEXT NOT NULL, "
            "source_rel_path TEXT NOT NULL, dest_rel_path TEXT NOT NULL, "
            "current_dest TEXT, file_hash TEXT, status TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        sidecar.execute(
            "INSERT INTO transaction_ledger VALUES (?, ?, ?, ?, ?, 'from_sidecar.txt', 'Docs/from_sidecar.txt', NULL, ?, 'COMPLETED', 30.0, 31.0)",
            (
                "e-1",
                "s-sidecar",
                str(base_dir),
                str(base_dir / "from_sidecar.txt"),
                str(base_dir / "Docs" / "from_sidecar.txt"),
                _sha256(b"sidecar data"),
            ),
        )
        sidecar.execute(
            "INSERT INTO transaction_ledger VALUES (?, ?, ?, ?, ?, 'locked.txt', 'Docs/locked.txt', NULL, NULL, 'FAILED_CONFLICT', 32.0, 33.0)",
            (
                "e-2",
                "s-sidecar",
                str(base_dir),
                str(base_dir / "locked.txt"),
                str(base_dir / "Docs" / "locked.txt"),
            ),
        )
        sidecar.commit()

    store = open_store(app_dir, legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        cache = store.cache(worker)
        history = store.history(db, cache)

        # Verify all three legacy sources were imported into canonical moves
        step_moves = history.moves.list_moves("s-step")
        assert len(step_moves) == 1
        assert step_moves[0].state == "done"

        main_tx_moves = history.moves.list_moves("s-main-tx")
        assert len(main_tx_moves) == 1
        assert main_tx_moves[0].state == "done"

        sidecar_moves = history.moves.list_moves("s-sidecar")
        assert [m.state for m in sidecar_moves] == ["done", "failed"]
        assert decode_move_failure(sidecar_moves[1].error)[0] == "FAILED_CONFLICT"

        # Undo each migrated session cleanly via canonical move replay
        for sid in ("s-step", "s-main-tx", "s-sidecar"):
            history.rollback(sid)

        assert (base_dir / "from_step.txt").read_bytes() == b"step data"
        assert (base_dir / "from_main_tx.txt").read_bytes() == b"main tx data"
        assert (base_dir / "from_sidecar.txt").read_bytes() == b"sidecar data"
    finally:
        worker.stop()


def test_reconcile_incomplete_transactions_and_purge_controls(env):
    """MoveLedger reconciles crash-interrupted intent rows across all disk states and purges cleanly."""
    base_dir, db, history = env
    sid = history.start_session(str(base_dir))

    # Case 1: Destination exists, source missing -> RECONCILED_SUCCESS (marks done + syncs DB path)
    dst_done = base_dir / "Docs" / "moved.txt"
    dst_done.parent.mkdir(parents=True, exist_ok=True)
    dst_done.write_bytes(b"moved bytes")
    db.upsert_document(str(base_dir), "moved.txt", _sha256(b"moved bytes"), "moved")
    seq_done = history.moves.record_intent(
        sid,
        str(base_dir / "moved.txt"),
        str(dst_done),
        src_sha256=_sha256(b"moved bytes"),
        size=11,
    )

    # Case 2: Source exists, destination missing -> ABORTED (marks failed)
    src_unmoved = base_dir / "still_here.txt"
    src_unmoved.write_bytes(b"untouched")
    seq_aborted = history.moves.record_intent(
        sid,
        str(src_unmoved),
        str(base_dir / "Docs" / "still_here.txt"),
        src_sha256=_sha256(b"untouched"),
        size=9,
    )

    # Case 3: Both source and destination hold identical bytes -> ABORTED and duplicate copy removed
    src_dup = base_dir / "dup.txt"
    dst_dup = base_dir / "Docs" / "dup.txt"
    src_dup.write_bytes(b"identical")
    dst_dup.write_bytes(b"identical")
    seq_dup = history.moves.record_intent(
        sid,
        str(src_dup),
        str(dst_dup),
        src_sha256=_sha256(b"identical"),
        size=9,
    )

    # Case 4: Both source and destination exist with different bytes -> FAILED_CONFLICT
    src_conflict = base_dir / "conflict.txt"
    dst_conflict = base_dir / "Docs" / "conflict.txt"
    src_conflict.write_bytes(b"version A")
    dst_conflict.write_bytes(b"version B")
    seq_conflict = history.moves.record_intent(
        sid,
        str(src_conflict),
        str(dst_conflict),
        src_sha256=_sha256(b"version A"),
        size=9,
    )

    # Case 5: Neither source nor destination exists -> FAILED_MISSING
    seq_missing = history.moves.record_intent(
        sid,
        str(base_dir / "vanished.txt"),
        str(base_dir / "Docs" / "vanished.txt"),
        src_sha256=_sha256(b"gone"),
        size=4,
    )

    reconciled = history.moves.reconcile_incomplete_transactions(db=db)
    assert reconciled == 5
    assert not dst_dup.exists()
    assert src_dup.read_bytes() == b"identical"
    assert db.get_document(str(base_dir), "Docs/moved.txt") is not None

    moves_by_seq = {m.seq: m for m in history.moves.list_moves(sid)}
    assert moves_by_seq[seq_done].state == "verified"
    assert moves_by_seq[seq_aborted].state == "failed"
    assert decode_move_failure(moves_by_seq[seq_aborted].error)[0] == "ABORTED"
    assert moves_by_seq[seq_dup].state == "failed"
    assert decode_move_failure(moves_by_seq[seq_dup].error)[0] == "ABORTED"
    assert moves_by_seq[seq_conflict].state == "failed"
    assert decode_move_failure(moves_by_seq[seq_conflict].error)[0] == "FAILED_CONFLICT"
    assert moves_by_seq[seq_missing].state == "failed"
    assert decode_move_failure(moves_by_seq[seq_missing].error)[0] == "FAILED_MISSING"

    # Purge controls
    assert history.moves.purge_completed() == 1
    assert history.moves.purge_failed() == 4
    assert history.moves.list_moves(sid) == []


def test_undo_aborts_on_destination_hash_mismatch_leaving_edited_file_in_place(env):
    """Undo refuses to overwrite/move a sorted file whose contents changed after sorting."""
    base_dir, db, history = env
    (base_dir / "notes.txt").write_bytes(b"original notes")
    execute_moves(str(base_dir), _plan("Docs", "notes.txt"), db, history)
    sid = _only_session(history)

    # User edits the sorted file at its destination
    (base_dir / "Docs" / "notes.txt").write_bytes(b"user edited notes")

    with pytest.raises(ValueError, match="changed since the sort"):
        history.rollback(sid)

    assert (base_dir / "Docs" / "notes.txt").read_bytes() == b"user edited notes"
    assert not (base_dir / "notes.txt").exists()
    (session,) = history.get_sessions()
    assert session["status"] == "failed"


def test_sort_and_undo_through_symlinked_base_dir(tmp_path):
    """Sorting and undoing via a symlinked base_dir restores files cleanly."""
    real_dir = tmp_path / "real_downloads"
    real_dir.mkdir()
    link_dir = tmp_path / "linked_downloads"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    (link_dir / "doc.txt").write_bytes(b"hello symlink")

    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        cache = store.cache(worker)
        history = store.history(db, cache)

        execute_moves(str(link_dir), _plan("Docs", "doc.txt"), db, history)
        assert (real_dir / "Docs" / "doc.txt").read_bytes() == b"hello symlink"

        sid = _only_session(history)
        history.rollback(sid)
        assert (real_dir / "doc.txt").read_bytes() == b"hello symlink"
        assert not (real_dir / "Docs").exists()
    finally:
        worker.stop()


def test_session_catalog_and_history_report_canonical_counts_across_statuses(tmp_path):
    """list_sessions() and get_sessions() report step_count and file_count from canonical moves across session states."""
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    worker = DBWorker()
    try:
        db = store.database(worker)
        cache = store.cache(worker)
        history = store.history(db, cache)

        base_dir = tmp_path / "Downloads"
        base_dir.mkdir()

        # 1. Completed session with 2 moves
        (base_dir / "c1.txt").write_bytes(b"1")
        (base_dir / "c2.txt").write_bytes(b"2")
        execute_moves(str(base_dir), _plan("Sorted", "c1.txt", "c2.txt"), db, history)
        sid_completed = history.get_sessions()[0]["session_id"]

        # 2. Rolled-back session with 1 move
        (base_dir / "r1.txt").write_bytes(b"3")
        execute_moves(str(base_dir), _plan("Sorted", "r1.txt"), db, history)
        sid_rolled = next(
            s["session_id"]
            for s in history.get_sessions()
            if s["session_id"] != sid_completed
        )
        history.rollback(sid_rolled)

        # 3. Failed session with 1 failed move
        sid_failed = history.start_session(str(base_dir))
        seq = history.moves.record_intent(
            sid_failed,
            str(base_dir / "f1.txt"),
            str(base_dir / "Sorted" / "f1.txt"),
        )
        history.moves.mark_failed(sid_failed, seq, "IO_ERROR: disk error")
        history._transition_session_status(sid_failed, "failed")

        by_history = {s["session_id"]: s for s in history.get_sessions()}
        by_catalog = {s["session_id"]: s for s in list_sessions(store=store)}

        for mapping in (by_history, by_catalog):
            assert mapping[sid_completed]["status"] == "completed"
            assert mapping[sid_completed]["step_count"] == 2
            assert mapping[sid_completed]["file_count"] == 2

            assert mapping[sid_rolled]["status"] == "rolled_back"
            assert mapping[sid_rolled]["step_count"] == 1
            assert mapping[sid_rolled]["file_count"] == 1

            assert mapping[sid_failed]["status"] == "failed"
            assert mapping[sid_failed]["step_count"] == 1
            assert mapping[sid_failed]["file_count"] == 1
    finally:
        worker.stop()
