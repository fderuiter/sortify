"""Sessions read and write the one app store (ADR 0008)."""

import json
import time
from pathlib import Path

import pytest

from app.config import AppSettings
from app.core.db_conn import get_db_connection
from app.core.history import open_session_history
from app.core.pipeline import SortPipeline
from app.core.session import AppSession
from app.core.session_catalog import (
    find_abandoned_sessions,
    list_sessions,
    mark_session_abandoned,
)
from app.core.store import Store, open_store


@pytest.fixture
def store(tmp_path):
    return open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")


@pytest.fixture
def session(tmp_path, store):
    src = tmp_path / "src"
    src.mkdir()
    (src / "finance_doc.txt").write_text("Finance, investment, and banking report.")
    (src / "tech_doc.txt").write_text("Software engineering, Python, algorithms.")
    (src / "health_doc.txt").write_text("Medical science, clinical trial, patient.")
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=str(src), store=store)
    try:
        yield s
    finally:
        s.close()


def test_session_managers_all_use_the_store(session, store):
    assert session.store is store
    assert (
        session.db.db_path
        == session.cache_manager.db_path
        == session.history_manager.db_path
        == str(store.path)
    )


def test_steps_are_logged_outside_any_sort_long_transaction(
    session, store, monkeypatch
):
    in_session = []
    log_step = session.history_manager.log_step

    def spy(*args, **kwargs):
        in_session.append(session.db_worker.is_in_session())
        return log_step(*args, **kwargs)

    monkeypatch.setattr(session.history_manager, "log_step", spy)

    plan, result = SortPipeline(session).run(dry_run=False)

    assert result.ok
    assert in_session and not any(in_session)
    sorted_dirs = [s for s in list_sessions(store) if s["base_dir"] == session.base_dir]
    assert len(sorted_dirs) == 1
    assert sorted_dirs[0]["history_db_path"] == str(store.path)


def test_undo_from_the_store_restores_a_sort(session, store):
    SortPipeline(session).run(dry_run=False)
    (sorted_session,) = list_sessions(store)

    with open_session_history(store) as history:
        history.rollback(sorted_session["session_id"])

    files = sorted(p.name for p in Path(session.base_dir).rglob("*") if p.is_file())
    assert files == ["finance_doc.txt", "health_doc.txt", "tech_doc.txt"]
    for name in files:
        assert (Path(session.base_dir) / name).is_file()


def _unfinished_session(store, base_dir):
    with open_session_history(store) as history:
        session_id = history.create_snapshot(str(base_dir))
        history.log_step(session_id, f"{base_dir}/a.txt", f"{base_dir}/Docs/a.txt")
    return session_id


def test_an_unfinished_session_with_steps_is_abandoned(tmp_path, store):
    session_id = _unfinished_session(store, tmp_path)

    (entry,) = find_abandoned_sessions(store)

    assert entry["session_id"] == session_id
    assert entry["step_count"] == 1


def test_a_session_marked_abandoned_is_not_offered_again(tmp_path, store):
    session_id = _unfinished_session(store, tmp_path)

    mark_session_abandoned(session_id, store)

    assert find_abandoned_sessions(store) == []


def test_an_interrupted_rollback_is_found_by_its_journal(store):
    journal = store.app_dir / "rollback_journal.json"
    journal.write_text(json.dumps({"session_id": "s1", "base_dir": "/x"}))

    (entry,) = find_abandoned_sessions(store)

    assert entry["session_id"] == "s1"
    assert entry["is_rollback_recovery"] is True
    assert entry["journal_path"] == str(journal)
    # The journal sits in the app directory, which recovery must never delete.
    assert "session_dir" not in entry


def test_list_sessions_executes_single_query(store):
    """Criterion 1: list_sessions executes one SQL statement with LEFT JOIN and GROUP BY."""
    conn = get_db_connection(str(store.path))
    with conn:
        # Session 1: 2 steps, 3 files
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("sess-1", 100.0, "/base1", "completed"),
        )
        conn.execute(
            "INSERT INTO step_ledger (session_id, source_path, target_path) VALUES (?, ?, ?)",
            ("sess-1", "/base1/a.txt", "/base1/dest/a.txt"),
        )
        conn.execute(
            "INSERT INTO step_ledger (session_id, source_path, target_path) VALUES (?, ?, ?)",
            ("sess-1", "/base1/b.txt", "/base1/dest/b.txt"),
        )
        conn.execute(
            "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime) VALUES (?, ?, 1, 10, 100.0)",
            ("sess-1", "a.txt"),
        )
        conn.execute(
            "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime) VALUES (?, ?, 2, 20, 100.0)",
            ("sess-1", "b.txt"),
        )
        conn.execute(
            "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime) VALUES (?, ?, 3, 30, 100.0)",
            ("sess-1", "c.txt"),
        )

        # Session 2: 0 steps, 0 files (empty child records)
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("sess-2", 200.0, "/base2", "active"),
        )

    # Trace queries on the connection
    executed_queries = []
    conn.set_trace_callback(executed_queries.append)
    try:
        sessions = list_sessions(store)
    finally:
        conn.set_trace_callback(None)

    # Verify exactly ONE query was executed (Criterion 1 & Success metric)
    assert len(executed_queries) == 1
    assert "LEFT JOIN" in executed_queries[0]
    assert "GROUP BY" in executed_queries[0]

    # Verify ordering (newest timestamp first)
    assert len(sessions) == 2
    assert sessions[0]["session_id"] == "sess-2"
    assert sessions[0]["timestamp"] == 200.0
    assert sessions[0]["base_dir"] == "/base2"
    assert sessions[0]["status"] == "active"
    assert sessions[0]["step_count"] == 0
    assert sessions[0]["file_count"] == 0
    assert isinstance(sessions[0]["step_count"], int)
    assert isinstance(sessions[0]["file_count"], int)
    assert sessions[0]["history_db_path"] == str(store.path)

    assert sessions[1]["session_id"] == "sess-1"
    assert sessions[1]["timestamp"] == 100.0
    assert sessions[1]["base_dir"] == "/base1"
    assert sessions[1]["status"] == "completed"
    assert sessions[1]["step_count"] == 2
    assert sessions[1]["file_count"] == 3
    assert isinstance(sessions[1]["step_count"], int)
    assert isinstance(sessions[1]["file_count"], int)
    assert sessions[1]["history_db_path"] == str(store.path)


def test_list_sessions_benchmark_500_sessions(store):
    """Criterion 3: Benchmarks confirm that 500 session records load in under 10 ms."""
    conn = get_db_connection(str(store.path))
    with conn:
        for i in range(500):
            sid = f"perf-sess-{i:04d}"
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (sid, float(i), f"/dir/{i}", "completed"),
            )
            # Add steps and files to every other session
            if i % 2 == 0:
                conn.execute(
                    "INSERT INTO step_ledger (session_id, source_path, target_path) VALUES (?, ?, ?)",
                    (sid, f"/dir/{i}/src.txt", f"/dir/{i}/dst.txt"),
                )
                conn.execute(
                    "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime) VALUES (?, ?, 1, 10, 100.0)",
                    (sid, "src.txt"),
                )

    # Warmup
    list_sessions(store)

    # Measure latency
    t0 = time.perf_counter()
    sessions = list_sessions(store)
    latency_ms = (time.perf_counter() - t0) * 1000.0

    assert len(sessions) == 500
    assert latency_ms < 10.0, f"Latency {latency_ms:.2f} ms exceeded 10.0 ms limit"


def test_list_sessions_legacy_database_missing_tables(tmp_path):
    """Maintain backward compatibility with older database files lacking child tables."""
    legacy_db_path = tmp_path / "legacy_app.db"
    conn = get_db_connection(str(legacy_db_path))
    with conn:
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("leg-1", 50.0, "/legacy", "completed"),
        )

    store = Store(app_dir=tmp_path, path=legacy_db_path)
    sessions = list_sessions(store)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "leg-1"
    assert sessions[0]["step_count"] == 0
    assert sessions[0]["file_count"] == 0
