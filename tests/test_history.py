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

    # Take snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # Move files and update DB to simulate organization
    file1_dst = os.path.join(base_dir, "folder", "file1.txt")
    file2_dst = os.path.join(base_dir, "folder", "file2.txt")
    os.makedirs(os.path.dirname(file1_dst), exist_ok=True)
    shutil.move(file1_src, file1_dst)
    shutil.move(file2_src, file2_dst)

    def _delete():
        conn = get_db_connection(db.db_path)
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))

    db.worker.execute_write(_delete)
    db.upsert_document(base_dir, os.path.join("folder", "file1.txt"), "hash1", "text1")
    db.upsert_document(base_dir, os.path.join("folder", "file2.txt"), "hash2", "text2")

    # Mock _robust_move to fail on the second file
    from app.core.history import _robust_move

    original_robust_move = _robust_move

    def mock_robust_move(src, dst):
        if "file2.txt" in src or "file2.txt" in dst:
            raise OSError("Mocked permission error on file2")
        return original_robust_move(src, dst)

    with patch("app.core.history._robust_move", side_effect=mock_robust_move):
        with pytest.raises(OSError, match="Mocked permission error on file2"):
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

    session_id = history_manager.create_snapshot(base_dir)

    # Swap A and B
    temp = os.path.join(base_dir, "temp.txt")
    shutil.move(file1_src, temp)
    shutil.move(file2_src, file1_src)
    shutil.move(temp, file2_src)

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
    with open(file1_src, "r") as f:
        assert f.read() == "A"
    with open(file2_src, "r") as f:
        assert f.read() == "B"

    # Verify db restored
    docA = db.get_document(base_dir, "A.txt")
    assert docA["file_hash"] == "hashA"

    docB = db.get_document(base_dir, "B.txt")
    assert docB["file_hash"] == "hashB"


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


def test_snapshot_pruning_preserves_sessions_within_age_limit_across_many_runs(test_history_env):
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
            "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime) VALUES (?, ?, 1, 100, ?)",
            ("session-policy-matched", "compliance_doc.pdf", now - (60 * day_sec)),
        )
        # Session 3: Ordinary expired session
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, 'completed')",
            ("session-ordinary-expired", now - (60 * day_sec), base_dir),
        )

    policies = [{"type": "compliance", "expression": "compliance_doc.pdf", "action": "retain"}]

    with conn:
        history_manager._prune_snapshots(conn, retention_days=30, policies=policies)

    sessions = history_manager.get_sessions()
    session_ids = [s["session_id"] for s in sessions]

    assert "session-status-retain" in session_ids
    assert "session-policy-matched" in session_ids
    assert "session-ordinary-expired" not in session_ids

