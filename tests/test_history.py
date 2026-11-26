import os
import shutil
import time
from unittest.mock import patch

import pytest

from app.core.db_conn import get_db_connection


def test_incremental_sync_and_stop_on_failure(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # Create two files
    file1_src = os.path.join(base_dir, "file1.txt")
    file2_src = os.path.join(base_dir, "file2.txt")
    with open(file1_src, "w") as f:
        f.write("file1")
    with open(file2_src, "w") as f:
        f.write("file2")

    # Upsert to DB
    db.upsert_document(base_dir, "file1.txt", "hash1", "text1")
    db.upsert_document(base_dir, "file2.txt", "hash2", "text2")

    # Register session and record canonical moves (file2 seq=1, file1 seq=2 so backwards replay restores file1 first and fails on file2)
    session_id = history_manager.start_session(base_dir)

    file1_dst = os.path.join(base_dir, "folder", "file1.txt")
    file2_dst = os.path.join(base_dir, "folder", "file2.txt")
    os.makedirs(os.path.dirname(file1_dst), exist_ok=True)

    seq2 = history_manager.moves.record_intent(session_id, file2_src, file2_dst)
    shutil.move(file2_src, file2_dst)
    history_manager.moves.mark_done(session_id, seq2, file2_dst)

    seq1 = history_manager.moves.record_intent(session_id, file1_src, file1_dst)
    shutil.move(file1_src, file1_dst)
    history_manager.moves.mark_done(session_id, seq1, file1_dst)
    history_manager.complete_session(session_id)

    def _delete():
        conn = get_db_connection(db.db_path)
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))

    db.worker.execute_write(_delete)
    db.upsert_document(base_dir, os.path.join("folder", "file1.txt"), "hash1", "text1")
    db.upsert_document(base_dir, os.path.join("folder", "file2.txt"), "hash2", "text2")

    # Mock resilient_move to fail on file2 (the second move replayed backwards)
    from app.core.resilient_file_ops import resilient_move as original_resilient_move

    def mock_resilient_move(src, dst, *args, **kwargs):
        if "file2.txt" in str(src) or "file2.txt" in str(dst):
            raise OSError("Mocked permission error on file2")
        return original_resilient_move(src, dst, *args, **kwargs)

    with patch(
        "app.core.resilient_file_ops.resilient_move",
        side_effect=mock_resilient_move,
    ):
        with pytest.raises(PermissionError, match="Mocked permission error on file2"):
            history_manager.rollback(session_id)

    # Validate state:
    # 1. file1 should be rolled back to its original position
    assert os.path.exists(file1_src)
    assert not os.path.exists(file1_dst)

    # 2. file2 should remain at its organized position because rollback failed
    assert os.path.exists(file2_dst)
    assert not os.path.exists(file2_src)

    # 3. Database should reflect file1 at 'file1.txt' and file2 at 'folder/file2.txt'
    conn = get_db_connection(db.db_path)
    with conn:
        cur = conn.execute(
            "SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,)
        )
        filepaths = {r[0] for r in cur.fetchall()}

    assert "file1.txt" in filepaths
    assert "folder/file2.txt" in filepaths
    assert "folder/file1.txt" not in filepaths
    assert "file2.txt" not in filepaths

    # 4. Session status should be 'failed'
    conn = get_db_connection(history_manager.db_path)
    with conn:
        cur = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        )
        status = cur.fetchone()[0]

    assert status == "failed"


def test_rollback_cyclic_collision(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    file1_src = os.path.join(base_dir, "A.txt")
    file2_src = os.path.join(base_dir, "B.txt")
    with open(file1_src, "w") as f:
        f.write("A")
    with open(file2_src, "w") as f:
        f.write("B")

    db.upsert_document(base_dir, "A.txt", "hashA", "textA")
    db.upsert_document(base_dir, "B.txt", "hashB", "textB")

    session_id = history_manager.start_session(base_dir)

    # Swap A and B via staging move recorded in canonical MoveLedger
    temp = os.path.join(base_dir, "temp.txt")
    for src, dst in ((file1_src, temp), (file2_src, file1_src), (temp, file2_src)):
        seq = history_manager.moves.record_intent(session_id, src, dst)
        shutil.move(src, dst)
        history_manager.moves.mark_done(session_id, seq, dst)
    history_manager.complete_session(session_id)

    def _delete():
        conn = get_db_connection(db.db_path)
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))

    db.worker.execute_write(_delete)
    db.upsert_document(base_dir, "A.txt", "hashB", "textB")
    db.upsert_document(base_dir, "B.txt", "hashA", "textA")

    # Perform rollback
    history_manager.rollback(session_id)

    # Verify files restored
    with open(file1_src) as f:
        assert f.read() == "A"
    with open(file2_src) as f:
        assert f.read() == "B"

    # Verify db restored
    docA = db.get_document(base_dir, "A.txt")
    assert docA["file_hash"] == "hashA"

    docB = db.get_document(base_dir, "B.txt")
    assert docB["file_hash"] == "hashB"


def test_rollback_multi_file_cyclic_collision(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    file_a = os.path.join(base_dir, "A.txt")
    file_b = os.path.join(base_dir, "B.txt")
    file_c = os.path.join(base_dir, "C.txt")

    with open(file_a, "w") as f:
        f.write("A")
    with open(file_b, "w") as f:
        f.write("B")
    with open(file_c, "w") as f:
        f.write("C")

    db.upsert_document(base_dir, "A.txt", "hashA", "textA")
    db.upsert_document(base_dir, "B.txt", "hashB", "textB")
    db.upsert_document(base_dir, "C.txt", "hashC", "textC")

    session_id = history_manager.start_session(base_dir)

    # 3-way rotation: C -> temp, B -> C, A -> B, temp -> A recorded in MoveLedger
    temp = os.path.join(base_dir, "temp.txt")
    for src, dst in (
        (file_c, temp),
        (file_b, file_c),
        (file_a, file_b),
        (temp, file_a),
    ):
        seq = history_manager.moves.record_intent(session_id, src, dst)
        shutil.move(src, dst)
        history_manager.moves.mark_done(session_id, seq, dst)
    history_manager.complete_session(session_id)

    def _update_docs():
        conn = get_db_connection(db.db_path)
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))

    db.worker.execute_write(_update_docs)
    db.upsert_document(base_dir, "A.txt", "hashC", "textC")
    db.upsert_document(base_dir, "B.txt", "hashA", "textA")
    db.upsert_document(base_dir, "C.txt", "hashB", "textB")

    # Perform rollback
    history_manager.rollback(session_id)

    # Verify files restored to original contents
    with open(file_a) as f:
        assert f.read() == "A"
    with open(file_b) as f:
        assert f.read() == "B"
    with open(file_c) as f:
        assert f.read() == "C"

    # Verify db records restored
    docA = db.get_document(base_dir, "A.txt")
    assert docA["file_hash"] == "hashA"
    docB = db.get_document(base_dir, "B.txt")
    assert docB["file_hash"] == "hashB"
    docC = db.get_document(base_dir, "C.txt")
    assert docC["file_hash"] == "hashC"


def test_rollback_cyclic_collision_with_non_cyclic_dest(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    file_a = os.path.join(base_dir, "A.txt")
    file_b = os.path.join(base_dir, "B.txt")
    file_c = os.path.join(base_dir, "C.txt")

    with open(file_a, "w") as f:
        f.write("A")
    with open(file_b, "w") as f:
        f.write("B")
    with open(file_c, "w") as f:
        f.write("C")

    db.upsert_document(base_dir, "A.txt", "hashA", "textA")
    db.upsert_document(base_dir, "B.txt", "hashB", "textB")
    db.upsert_document(base_dir, "C.txt", "hashC", "textC")

    session_id = history_manager.start_session(base_dir)

    # Swap A and B (cyclic collision) and move C to subfolder, recorded in MoveLedger
    temp = os.path.join(base_dir, "temp.txt")
    c_moved = os.path.join(base_dir, "sub", "C.txt")
    os.makedirs(os.path.dirname(c_moved), exist_ok=True)
    for src, dst in (
        (file_a, temp),
        (file_b, file_a),
        (temp, file_b),
        (file_c, c_moved),
    ):
        seq = history_manager.moves.record_intent(session_id, src, dst)
        shutil.move(src, dst)
        history_manager.moves.mark_done(session_id, seq, dst)
    history_manager.complete_session(session_id)

    # Create an unmanaged file at C.txt (occupied source collision on undo)
    with open(file_c, "w") as f:
        f.write("unmanaged")

    def _update_docs():
        conn = get_db_connection(db.db_path)
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))

    db.worker.execute_write(_update_docs)
    db.upsert_document(base_dir, "A.txt", "hashB", "textB")
    db.upsert_document(base_dir, "B.txt", "hashA", "textA")
    db.upsert_document(base_dir, "sub/C.txt", "hashC", "textC")

    # Perform rollback
    history_manager.rollback(session_id)

    # Verify A and B restored
    with open(file_a) as f:
        assert f.read() == "A"
    with open(file_b) as f:
        assert f.read() == "B"

    # Unmanaged file at C.txt is preserved untouched; C restores non-destructively to C_1.txt (ADR 0006)
    with open(file_c) as f:
        assert f.read() == "unmanaged"

    restored_c = os.path.join(base_dir, "C_1.txt")
    assert os.path.exists(restored_c)
    with open(restored_c) as f:
        assert f.read() == "C"

    # Verify db documents restored
    docA = db.get_document(base_dir, "A.txt")
    assert docA["file_hash"] == "hashA"
    docB = db.get_document(base_dir, "B.txt")
    assert docB["file_hash"] == "hashB"
    docC = db.get_document(base_dir, "C_1.txt")
    assert docC["file_hash"] == "hashC"


def test_age_based_snapshot_pruning(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env
    import time

    from app.core.db_conn import get_db_connection

    now = time.time()
    day_sec = 86400

    conn = get_db_connection(history_manager.db_path)
    with conn:
        # Session 1: 10 days old (within default 30-day retention window)
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("session-recent", now - (10 * day_sec), base_dir),
        )
        # Session 2: 40 days old (expired)
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("session-expired", now - (40 * day_sec), base_dir),
        )

    with conn:
        history_manager._prune_snapshots(conn, retention_days=30)

    sessions = history_manager.get_sessions()
    session_ids = [s["session_id"] for s in sessions]

    assert "session-recent" in session_ids
    assert "session-expired" not in session_ids


def test_divergent_branch_protection_from_pruning(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env
    import time

    from app.core.db_conn import get_db_connection

    now = time.time()
    day_sec = 86400

    expired_id = "session-expired-branch"
    branch_dir = os.path.join(base_dir, ".branches", expired_id)
    os.makedirs(branch_dir, exist_ok=True)
    with open(os.path.join(branch_dir, "unmerged.txt"), "w") as f:
        f.write("unmerged branch data")

    conn = get_db_connection(history_manager.db_path)
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            (expired_id, now - (50 * day_sec), base_dir),
        )

    with conn:
        history_manager._prune_snapshots(conn, retention_days=30)

    sessions = history_manager.get_sessions()
    session_ids = [s["session_id"] for s in sessions]

    assert expired_id in session_ids


def test_snapshot_pruning_preserves_sessions_within_age_limit_across_many_runs(
    test_history_env,
):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    now = time.time()
    day_sec = 86400

    conn = get_db_connection(history_manager.db_path)
    created_ids = []
    with conn:
        # Create 15 sessions spread across the last 80 days (within 90-day retention limit)
        for i in range(15):
            sid = f"session-recent-{i}"
            created_ids.append(sid)
            ts = now - ((i + 1) * 5 * day_sec)  # 5, 10, ..., 75 days ago
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
                (sid, ts, base_dir),
            )
        # Create 1 expired session (100 days old)
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("session-old-100", now - (100 * day_sec), base_dir),
        )

    with conn:
        history_manager._prune_snapshots(conn, retention_days=90)

    sessions = history_manager.get_sessions()
    session_ids = [s["session_id"] for s in sessions]

    # Verify all 15 sessions within the retention age limit stay
    for sid in created_ids:
        assert sid in session_ids
    assert "session-old-100" not in session_ids


def test_snapshot_pruning_respects_policy_engine_actions(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env

    now = time.time()
    day_sec = 86400

    conn = get_db_connection(history_manager.db_path)
    with conn:
        # Session 1: Status 'retain'
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'retain')",
            ("session-status-retain", now - (60 * day_sec), base_dir),
        )
        # Session 2: Session with policy-matched file 'compliance_doc.pdf'
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("session-policy-matched", now - (60 * day_sec), base_dir),
        )
        conn.execute(
            "INSERT INTO moves (session_id, seq, src, dst, state, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, 'done', ?, ?)",
            (
                "session-policy-matched",
                os.path.join(base_dir, "compliance_doc.pdf"),
                os.path.join(base_dir, "Docs", "compliance_doc.pdf"),
                now - (60 * day_sec),
                now - (60 * day_sec),
            ),
        )
        # Session 3: Ordinary expired session
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("session-ordinary-expired", now - (60 * day_sec), base_dir),
        )

    policies = [
        {"type": "compliance", "expression": "compliance_doc.pdf", "action": "retain"}
    ]

    with conn:
        history_manager._prune_snapshots(conn, retention_days=30, policies=policies)

    sessions = history_manager.get_sessions()
    session_ids = [s["session_id"] for s in sessions]

    assert "session-status-retain" in session_ids
    assert "session-policy-matched" in session_ids
    assert "session-ordinary-expired" not in session_ids


def test_remove_staging_leftovers_oserror(caplog):
    from app.core.history import _remove_staging_leftovers

    with (
        patch("os.walk") as mock_walk,
        patch(
            "app.core.resilient_file_ops.resilient_remove",
            side_effect=OSError("Access denied"),
        ),
    ):
        mock_walk.return_value = [("/tmp_base", [], [".tmp_stage_123"])]
        _remove_staging_leftovers("/tmp_base")
    assert "Could not remove staging file .tmp_stage_123" in caplog.text


def test_purge_overwrite_backups_oserror(caplog):
    from app.core.history import _purge_overwrite_backups

    with (
        patch("os.path.isdir", return_value=True),
        patch("os.path.islink", return_value=False),
        patch(
            "app.core.resilient_file_ops.resilient_rmtree",
            side_effect=OSError("RMDIR error"),
        ),
    ):
        _purge_overwrite_backups("/tmp_base", "sess-1")
    assert "Could not purge overwrite backups" in caplog.text


def test_get_session_moves_getsize_error(test_history_env):
    base_dir, db, cache, history_manager, db_worker = test_history_env
    session_id = history_manager.start_session(base_dir)
    seq = history_manager.moves.record_intent(
        session_id,
        os.path.join(base_dir, "a.txt"),
        "/nonexistent/path/file.txt",
        size=None,
    )
    history_manager.moves.mark_done(session_id, seq, "/nonexistent/path/file.txt")

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.getsize", side_effect=OSError("Get size failed")),
    ):
        moves = history_manager.get_session_moves(session_id)
        assert len(moves) == 1
        assert moves[0]["size"] is None


def test_history_manager_get_sessions_single_query(test_history_env):
    """Criterion 2: HistoryManager.get_sessions executes one aggregated SQL query instead of sequential count statements."""
    base_dir, db, cache, history_manager, db_worker = test_history_env
    conn = get_db_connection(history_manager.db_path)
    with conn:
        # Session 1: 3 moves across 2 distinct source files
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("sess-hist-1", 100.0, base_dir, "completed"),
        )
        moves_rows = [
            (1, f"{base_dir}/s0.txt", f"{base_dir}/d0.txt"),
            (2, f"{base_dir}/s1.txt", f"{base_dir}/d1.txt"),
            (3, f"{base_dir}/s0.txt", f"{base_dir}/d2.txt"),
        ]
        for seq, src, dst in moves_rows:
            conn.execute(
                "INSERT INTO moves (session_id, seq, src, dst, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'done', 100.0, 100.0)",
                ("sess-hist-1", seq, src, dst),
            )

        # Session 2: 0 moves, 0 files
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("sess-hist-2", 200.0, base_dir, "active"),
        )

    executed_queries = []
    conn.set_trace_callback(executed_queries.append)
    try:
        sessions = history_manager.get_sessions()
    finally:
        conn.set_trace_callback(None)

    # Must execute exactly ONE query
    assert len(executed_queries) == 1
    assert "LEFT JOIN" in executed_queries[0]
    assert "GROUP BY" in executed_queries[0]

    by_id = {s["session_id"]: s for s in sessions}
    s2 = by_id["sess-hist-2"]
    assert s2["timestamp"] == 200.0
    assert s2["status"] == "active"
    assert s2["step_count"] == 0
    assert s2["file_count"] == 0
    assert isinstance(s2["step_count"], int)
    assert isinstance(s2["file_count"], int)

    s1 = by_id["sess-hist-1"]
    assert s1["timestamp"] == 100.0
    assert s1["status"] == "completed"
    assert s1["step_count"] == 3
    assert s1["file_count"] == 2
    assert isinstance(s1["step_count"], int)
    assert isinstance(s1["file_count"], int)


def test_history_manager_get_sessions_benchmark_500_sessions(test_history_env):
    """Criterion 3: Benchmarks confirm that 500 session records load in under 10 ms."""
    base_dir, db, cache, history_manager, db_worker = test_history_env
    conn = get_db_connection(history_manager.db_path)
    with conn:
        for i in range(500):
            sid = f"hist-perf-{i:04d}"
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (sid, float(i), base_dir, "completed"),
            )
            if i % 2 == 0:
                conn.execute(
                    "INSERT INTO moves (session_id, seq, src, dst, state, created_at, updated_at) "
                    "VALUES (?, 1, ?, ?, 'done', 100.0, 100.0)",
                    (sid, f"{base_dir}/s_{i}.txt", f"{base_dir}/d_{i}.txt"),
                )

    # Warmup
    history_manager.get_sessions()

    # Measure latency
    t0 = time.perf_counter()
    sessions = history_manager.get_sessions()
    latency_ms = (time.perf_counter() - t0) * 1000.0

    assert len(sessions) >= 500
    assert latency_ms < 10.0, f"Latency {latency_ms:.2f} ms exceeded 10.0 ms limit"


def test_history_manager_get_sessions_legacy_database_missing_tables(
    test_history_env, tmp_path
):
    """Maintain backward compatibility with older database files lacking child tables."""
    from app.core.history import HistoryManager

    base_dir, db, cache, _, _ = test_history_env
    legacy_db = tmp_path / "legacy_hist.db"
    conn = get_db_connection(str(legacy_db))
    with conn:
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("leg-hist-1", 50.0, "/legacy", "completed"),
        )

    hm = HistoryManager(db, cache, str(legacy_db))
    sessions = hm.get_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "leg-hist-1"
    assert sessions[0]["step_count"] == 0
    assert sessions[0]["file_count"] == 0


def test_session_state_machine_managed_connection_and_audit_queries(tmp_path):
    """SessionStateMachine handles managed connections, listener errors, and durable audit rows."""
    import pytest

    from app.core.history import (
        COMPLETED,
        ROLLED_BACK,
        UNCOMMITTED,
        SessionStateMachine,
    )

    no_db_sm = SessionStateMachine(db_path=None)
    with pytest.raises(ValueError, match="db_path or an active SQLite connection"):
        no_db_sm.transition("s-none", COMPLETED)

    db_file = tmp_path / "sm_managed.db"
    conn = get_db_connection(str(db_file))
    with conn:
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
        )

    seen_events = []

    def bad_listener(_ev):
        raise RuntimeError("listener boom")

    def good_listener(ev):
        seen_events.append(ev["new_status"])

    sm = SessionStateMachine(db_path=str(db_file))
    sm.add_transition_listener(bad_listener)
    sm.add_transition_listener(good_listener)

    prev, nxt = sm.transition("s-managed", COMPLETED, metadata={"source": "unit"})
    assert (prev, nxt) == (UNCOMMITTED, COMPLETED)
    sm.remove_transition_listener(bad_listener)
    prev2, nxt2 = sm.transition("s-managed", ROLLED_BACK)
    assert (prev2, nxt2) == (COMPLETED, ROLLED_BACK)
    assert seen_events == [COMPLETED, ROLLED_BACK]

    audit_rows = conn.execute(
        "SELECT session_id, previous_status, new_status FROM session_lifecycle_audit ORDER BY id ASC"
    ).fetchall()
    assert audit_rows == [
        ("s-managed", UNCOMMITTED, COMPLETED),
        ("s-managed", COMPLETED, ROLLED_BACK),
    ]


def test_history_helpers_duplicate_staging_blocking_parents_and_find_moved(tmp_path):
    """Helper utilities safely purge staging, move blocking parents, and locate intent moves."""
    import hashlib

    from app.core.history import (
        EXPORTED_TO_TRASH,
        FAILED,
        INTENT,
        _find_moved_file,
        _holds_bytes,
        _move_aside_blocking_parents,
        _purge_duplicate_staging,
        _purge_overwrite_backups,
    )
    from app.core.mover import duplicate_staging_dir
    from app.core.moves import Move

    _purge_duplicate_staging("", "sid-empty")
    _purge_overwrite_backups("", "sid-empty")
    _move_aside_blocking_parents("", str(tmp_path))

    base_dir = tmp_path / "workspace"
    base_dir.mkdir()
    stage_dir = duplicate_staging_dir(str(base_dir), "sid-dup")
    os.makedirs(stage_dir, exist_ok=True)
    with open(os.path.join(stage_dir, "dup.txt"), "w", encoding="utf-8") as f:
        f.write("staged")

    _purge_duplicate_staging(str(base_dir), "sid-dup")
    assert not os.path.exists(stage_dir)

    # Blocking parent file where a directory needs to be recreated, updating DB via db_conn
    blocking = base_dir / "Archive"
    blocking.write_text("blocking file", encoding="utf-8")
    db_file = tmp_path / "docs_parent.db"
    dconn = get_db_connection(str(db_file))
    with dconn:
        dconn.execute("CREATE TABLE documents (base_dir TEXT, filepath TEXT)")
        dconn.execute(
            "INSERT INTO documents (base_dir, filepath) VALUES (?, ?)",
            (str(base_dir), "Archive"),
        )

    _move_aside_blocking_parents(
        str(base_dir / "Archive" / "report.pdf"),
        str(base_dir),
        db_conn=dconn,
    )
    assert not blocking.is_file()
    updated = dconn.execute("SELECT filepath FROM documents").fetchone()[0]
    assert updated != "Archive"
    dconn.close()

    # _holds_bytes and _find_moved_file branches
    assert _holds_bytes(str(base_dir / "missing.bin"), "deadbeef") is False
    payload = b"intent-payload"
    digest = hashlib.sha256(payload).hexdigest()
    dst_dir = base_dir / "Sorted"
    dst_dir.mkdir()
    suffixed = dst_dir / "doc_1.txt"
    suffixed.write_bytes(payload)

    failed_move = Move(
        seq=1,
        session_id="s1",
        src=str(base_dir / "doc.txt"),
        dst=str(dst_dir / "doc.txt"),
        src_sha256=digest,
        size=len(payload),
        displaced=None,
        state=FAILED,
        error="io",
        created_at=1.0,
        updated_at=1.0,
    )
    assert _find_moved_file(failed_move, set()) is None

    trashed_move = Move(
        seq=2,
        session_id="s1",
        src=str(base_dir / "doc.txt"),
        dst=str(dst_dir / "doc.txt"),
        src_sha256=digest,
        size=len(payload),
        displaced=None,
        state=EXPORTED_TO_TRASH,
        error=None,
        created_at=1.0,
        updated_at=1.0,
    )
    assert _find_moved_file(trashed_move, set()) is None

    no_hash_intent = Move(
        seq=3,
        session_id="s1",
        src=str(base_dir / "doc.txt"),
        dst=str(dst_dir / "doc.txt"),
        src_sha256=None,
        size=None,
        displaced=None,
        state=INTENT,
        error=None,
        created_at=1.0,
        updated_at=1.0,
    )
    assert _find_moved_file(no_hash_intent, set()) is None

    suffixed_intent = Move(
        seq=4,
        session_id="s1",
        src=str(base_dir / "doc.txt"),
        dst=str(dst_dir / "doc.txt"),
        src_sha256=digest,
        size=len(payload),
        displaced=None,
        state=INTENT,
        error=None,
        created_at=1.0,
        updated_at=1.0,
    )
    assert _find_moved_file(suffixed_intent, set()) == str(suffixed)


def test_export_audit_log_formats_and_move_states(test_history_env, tmp_path):
    """export_audit_log maps move states accurately across JSON and CSV outputs."""
    import pytest

    base_dir, _db, _cache, history_manager, _db_worker = test_history_env
    conn = get_db_connection(history_manager.db_path)
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("exp-sess", 1700000000.0, base_dir, "completed"),
        )
        for seq, state, dst_name in [
            (1, "verified", "a.txt"),
            (2, "exported_to_trash", "b.txt"),
            (3, "intent", "c_1.txt"),
            (4, "failed", "d.txt"),
        ]:
            conn.execute(
                "INSERT INTO moves (session_id, seq, src, dst, src_sha256, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "exp-sess",
                    seq,
                    f"{base_dir}/orig_{seq}.txt",
                    f"{base_dir}/{dst_name}",
                    "abc123",
                    state,
                    1700000000.0 + seq,
                    1700000000.0 + seq,
                ),
            )

    csv_out = tmp_path / "audit.csv"
    res_csv = history_manager.export_audit_log(
        str(csv_out), session_id="exp-sess", base_dir=base_dir
    )
    assert res_csv["format"] == "csv"
    assert res_csv["count"] == 4
    assert csv_out.exists()

    json_out = tmp_path / "audit_all.json"
    res_json = history_manager.export_audit_log(
        str(json_out), session_id=None, base_dir=base_dir
    )
    assert res_json["format"] == "json"
    assert res_json["count"] >= 4

    with pytest.raises(ValueError, match="not found in history database"):
        history_manager.export_audit_log(
            str(tmp_path / "missing.json"),
            session_id="nonexistent-session",
            base_dir=base_dir,
        )
    with pytest.raises(ValueError, match="Unsupported export format"):
        history_manager.export_audit_log(
            str(tmp_path / "bad.xml"),
            session_id="exp-sess",
            format="xml",
            base_dir=base_dir,
        )
