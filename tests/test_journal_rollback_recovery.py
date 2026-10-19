import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.db_conn import get_db_connection
from app.core.session import scan_abandoned_sessions_async


@pytest.fixture
def mock_session_base(tmp_path, monkeypatch):
    """Isolate the session base directory so it scans from a known clean temp location."""
    session_base = tmp_path / "autosorter_sessions"
    session_base.mkdir(parents=True, exist_ok=True)

    def mock_get_session_base_dir():
        return session_base

    monkeypatch.setattr(
        "app.core.path_utils.get_session_base_dir", mock_get_session_base_dir
    )
    return session_base


def test_normal_rollback_writes_and_deletes_journal(test_history_env):
    """Verify that a normal rollback writes a physical journal and deletes it upon success."""
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # 1. Create source file
    file_src = os.path.join(base_dir, "test_doc.txt")
    with open(file_src, "w") as f:
        f.write("source content")

    db.upsert_document(base_dir, "test_doc.txt", "hash_val", "extracted text")

    # 2. Take a snapshot
    session_id = history_manager.create_snapshot(base_dir)

    # 3. Move file to simulate organization
    file_dst = os.path.join(base_dir, "organized", "test_doc.txt")
    os.makedirs(os.path.dirname(file_dst), exist_ok=True)
    shutil.move(file_src, file_dst)

    # Update DB document path to organized
    def _update_db():
        conn = get_db_connection(db.db_path)
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))

    db.worker.execute_write(_update_db)
    db.upsert_document(
        base_dir,
        os.path.join("organized", "test_doc.txt"),
        "hash_val",
        "extracted text",
    )

    # 4. Mock the internal move execution to verify journal file exists right before file relocation
    from app.core.history import _robust_move

    original_robust_move = _robust_move
    journal_found_during_move = False
    journal_path = Path(history_manager.db_path).parent / "rollback_journal.json"

    def mock_robust_move(src, dst):
        nonlocal journal_found_during_move
        if journal_path.exists():
            journal_found_during_move = True
            # Read contents of journal
            with open(journal_path, "r") as jf:
                jdata = json.load(jf)
                assert jdata["session_id"] == session_id
                assert "safety_session_id" in jdata
                assert jdata["base_dir"] == base_dir
        return original_robust_move(src, dst)

    with patch("app.core.history._robust_move", side_effect=mock_robust_move):
        history_manager.rollback(session_id)

    # 5. Assertions
    assert journal_found_during_move is True, (
        "Journal was not written before file relocations!"
    )
    assert not journal_path.exists(), "Journal was not deleted on successful rollback!"


@pytest.mark.anyio
async def test_scanner_detects_interrupted_rollback(mock_session_base, tmp_path):
    """Verify that scan_abandoned_sessions_async correctly detects rollback_journal.json."""
    session_id = "interrupted-session-123"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    # Create dummy rollback_journal.json inside the session directory
    journal_path = session_dir / "rollback_journal.json"
    journal_data = {
        "session_id": session_id,
        "safety_session_id": "safety-123",
        "base_dir": str(tmp_path / "target_dir"),
        "moves": [["/some/src", "/some/dst"]],
        "symlinks": [],
        "shortcuts": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal_data, f, indent=2)

    abandoned = await scan_abandoned_sessions_async()

    assert len(abandoned) == 1
    session_info = abandoned[0]
    assert session_info["session_id"] == session_id
    assert session_info["safety_session_id"] == "safety-123"
    assert session_info["is_rollback_recovery"] is True
    assert session_info["status"] == "interrupted_rollback"
    assert session_info["journal_path"] == str(journal_path)


def test_step_rollback_deletes_journal_on_success(test_history_env):
    """A completed ledger-step rollback must not leave a journal behind.

    A leftover journal makes the next start report the finished undo as an
    interrupted rollback and offer to resume it.
    """
    from app.core.extractor import get_file_hash

    base_dir, db, cache, history_manager, db_worker = test_history_env

    moved = os.path.join(base_dir, "Finance", "report.txt")
    os.makedirs(os.path.dirname(moved), exist_ok=True)
    with open(moved, "w") as f:
        f.write("quarterly revenue")

    steps = [
        {
            "step_number": 1,
            "source_path": "report.txt",
            "destination_path": os.path.join("Finance", "report.txt"),
            "file_hash": get_file_hash(moved),
            "item_type": "file",
            "metadata": None,
        }
    ]
    journal_path = Path(history_manager.db_path).parent / "rollback_journal.json"

    db.worker.execute_write(
        lambda: history_manager._rollback_from_steps(
            "session-1", steps, base_dir, safety_session_id=None
        )
    )

    assert os.path.exists(os.path.join(base_dir, "report.txt"))
    assert not journal_path.exists(), (
        "Journal was left behind after a successful step rollback"
    )


def test_rollback_leaves_no_active_sessions(test_history_env):
    """After a successful undo, no session (including its safety snapshot) stays active."""
    base_dir, db, cache, history_manager, db_worker = test_history_env

    src = os.path.join(base_dir, "report.txt")
    with open(src, "w") as f:
        f.write("quarterly revenue")
    db.upsert_document(base_dir, "report.txt", "hash_val", "extracted text")
    session_id = history_manager.create_snapshot(base_dir)

    os.makedirs(os.path.join(base_dir, "Finance"))
    shutil.move(src, os.path.join(base_dir, "Finance", "report.txt"))

    history_manager.rollback(session_id)

    statuses = {s["session_id"]: s["status"] for s in history_manager.get_sessions()}
    assert statuses[session_id] == "rolled_back"
    assert "active" not in statuses.values(), statuses
