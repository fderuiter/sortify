import json
import os
import shutil

import pytest

from app.core.session_catalog import find_abandoned_sessions
from app.core.store import open_store


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


@pytest.mark.anyio
async def test_scanner_detects_interrupted_rollback(mock_session_base, tmp_path):
    """A rollback journal an older version left in a session folder is found."""
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

    abandoned = find_abandoned_sessions(open_store(tmp_path / "app"))

    assert len(abandoned) == 1
    session_info = abandoned[0]
    assert session_info["session_id"] == session_id
    assert session_info["safety_session_id"] == "safety-123"
    assert session_info["is_rollback_recovery"] is True
    assert session_info["status"] == "interrupted_rollback"
    assert session_info["journal_path"] == str(journal_path)
    assert session_info["session_dir"] == str(session_dir)


def test_rollback_leaves_no_active_sessions(test_history_env):
    """After a successful undo, no session stays active."""
    base_dir, db, cache, history_manager, db_worker = test_history_env

    src = os.path.join(base_dir, "report.txt")
    with open(src, "w") as f:
        f.write("quarterly revenue")
    db.upsert_document(base_dir, "report.txt", "hash_val", "extracted text")
    session_id = history_manager.create_snapshot(base_dir)

    os.makedirs(os.path.join(base_dir, "Finance"))
    dst = os.path.join(base_dir, "Finance", "report.txt")
    shutil.move(src, dst)
    seq = history_manager.moves.record_intent(session_id, src, dst)
    history_manager.moves.mark_done(session_id, seq, dst)

    history_manager.rollback(session_id)

    statuses = {s["session_id"]: s["status"] for s in history_manager.get_sessions()}
    assert statuses[session_id] == "rolled_back"
    assert "active" not in statuses.values(), statuses
