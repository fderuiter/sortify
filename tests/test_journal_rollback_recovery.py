import asyncio
import json
import os
import shutil
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.db_conn import get_db_connection
from app.core.session import scan_abandoned_sessions_async
from app.ui.app import AutoSorterApp


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
    original_move = shutil.move
    journal_found_during_move = False
    journal_path = Path(history_manager.db_path).parent / "rollback_journal.json"

    def mock_move(src, dst):
        nonlocal journal_found_during_move
        if journal_path.exists():
            journal_found_during_move = True
            # Read contents of journal
            with open(journal_path, "r") as jf:
                jdata = json.load(jf)
                assert jdata["session_id"] == session_id
                assert "safety_session_id" in jdata
                assert jdata["base_dir"] == base_dir
        return original_move(src, dst)

    with patch("shutil.move", side_effect=mock_move):
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


def test_resume_interrupted_rollback(test_history_env):
    """Verify that resume_rollback completes remaining moves and cleans up."""
    base_dir, db, cache, history_manager, db_worker = test_history_env

    file1_original = os.path.join(base_dir, "file1.txt")
    file2_original = os.path.join(base_dir, "file2.txt")

    with open(file1_original, "w") as f:
        f.write("content 1")
    with open(file2_original, "w") as f:
        f.write("content 2")

    db.upsert_document(base_dir, "file1.txt", "hash1", "text1")
    db.upsert_document(base_dir, "file2.txt", "hash2", "text2")

    # Take snapshot (target snapshot represents files at root)
    session_id = history_manager.create_snapshot(base_dir)

    # Now, simulate a partial rollback (e.g., interrupted):
    # file1.txt stays at root (already rollback-moved, or not yet organized).
    # file2.txt is partially/interrupted-moved to 'organized' folder using shutil.move
    file2_dst = os.path.join(base_dir, "organized", "file2.txt")
    os.makedirs(os.path.dirname(file2_dst), exist_ok=True)
    shutil.move(file2_original, file2_dst)

    # Let's create a journal file to resume
    journal_path = Path(history_manager.db_path).parent / "rollback_journal.json"
    journal_data = {
        "session_id": session_id,
        "safety_session_id": "dummy_safety_id",
        "base_dir": base_dir,
        "moves": [
            [file2_dst, file2_original]  # Pending move
        ],
        "symlinks": [],
        "shortcuts": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal_data, f, indent=2)

    # Resume!
    history_manager.resume_rollback(session_id)

    # Assertions
    # 1. Both files should be at their original locations
    assert os.path.exists(file1_original)
    assert os.path.exists(file2_original)
    assert not os.path.exists(file2_dst)

    # 2. DB should be synchronized to original locations
    doc1 = db.get_document(base_dir, "file1.txt")
    doc2 = db.get_document(base_dir, "file2.txt")
    assert doc1 is not None
    assert doc2 is not None

    # 3. Journal file should be deleted
    assert not journal_path.exists()


def test_revert_interrupted_rollback(test_history_env):
    """Verify that revert_rollback completely restores previous state using safety snapshot."""
    base_dir, db, cache, history_manager, db_worker = test_history_env

    # We want to revert back to safety snapshot state.
    # Original (safety) state: both files were in 'organized' directory
    file1_organized = os.path.join(base_dir, "organized", "file1.txt")
    file2_organized = os.path.join(base_dir, "organized", "file2.txt")
    os.makedirs(os.path.dirname(file1_organized), exist_ok=True)

    with open(file1_organized, "w") as f:
        f.write("content 1")
    with open(file2_organized, "w") as f:
        f.write("content 2")

    db.upsert_document(
        base_dir, os.path.join("organized", "file1.txt"), "hash1", "text1"
    )
    db.upsert_document(
        base_dir, os.path.join("organized", "file2.txt"), "hash2", "text2"
    )

    # Take safety snapshot (which represents pre-rollback state)
    safety_session_id = history_manager.create_snapshot(base_dir)

    # Interrupted Rollback State: file1 was moved to root, file2 is still in organized folder
    file1_root = os.path.join(base_dir, "file1.txt")
    shutil.move(file1_organized, file1_root)

    # Setup the active journal pointing to safety_session_id
    journal_path = Path(history_manager.db_path).parent / "rollback_journal.json"
    journal_data = {
        "session_id": "target_rollback_session",
        "safety_session_id": safety_session_id,
        "base_dir": base_dir,
        "moves": [
            [file1_organized, file1_root],
            [file2_organized, os.path.join(base_dir, "file2.txt")],
        ],
        "symlinks": [],
        "shortcuts": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal_data, f, indent=2)

    # Revert!
    history_manager.revert_rollback(safety_session_id)

    # Assertions:
    # 1. Both files must be restored to organized directory
    assert os.path.exists(file1_organized)
    assert os.path.exists(file2_organized)
    assert not os.path.exists(file1_root)

    # 2. DB should reflect the original organized paths
    doc1 = db.get_document(base_dir, os.path.join("organized", "file1.txt"))
    doc2 = db.get_document(base_dir, os.path.join("organized", "file2.txt"))
    assert doc1 is not None
    assert doc2 is not None

    # 3. Journal must be deleted
    assert not journal_path.exists()


@pytest.mark.anyio
async def test_ui_rollback_recovery_dialog_trigger(mock_session_base, tmp_path):
    """Verify that AutoSorterApp triggers the rollback recovery dialog on startup when an active journal is found."""
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "interrupted-rollback-session"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    # Create dummy history.db inside the session folder to keep scanner happy
    history_db = session_dir / "history.db"
    import sqlite3
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            cursor.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(tmp_path / "user_data"), "active"),
            )
        conn.commit()

    # Create journal file
    journal_path = session_dir / "rollback_journal.json"
    journal_data = {
        "session_id": "target_session",
        "safety_session_id": "safety_session",
        "base_dir": str(tmp_path / "user_data"),
        "moves": [],
        "symlinks": [],
        "shortcuts": [],
    }
    with open(journal_path, "w") as f:
        json.dump(journal_data, f, indent=2)

    app = AutoSorterApp(settings)

    with patch.object(app, "show_rollback_recovery_dialog") as mock_dialog:
        app.check_abandoned_sessions()
        await asyncio.sleep(0.1)
        mock_dialog.assert_called_once()
        called_arg = mock_dialog.call_args[0][0]
        assert called_arg["session_id"] == "target_session"
        assert called_arg["is_rollback_recovery"] is True


@pytest.mark.anyio
async def test_ui_rollback_dialog_resume_and_revert_actions(
    mock_session_base, tmp_path
):
    """Verify that the dialog's Resume and Revert buttons invoke the correct backend recovery methods."""
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "rollback-action-session"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    session_info = {
        "session_id": "target_rollback_session",
        "safety_session_id": "safety_session",
        "base_dir": str(tmp_path / "user_data"),
        "session_dir": str(session_dir),
        "journal_path": str(session_dir / "rollback_journal.json"),
        "is_rollback_recovery": True,
        "status": "interrupted_rollback",
    }

    app = AutoSorterApp(settings)

    # 1. Test clicking "Resume"
    with (
        patch("nicegui.ui.dialog") as mock_dialog,
        patch("nicegui.ui.card"),
        patch("nicegui.ui.label"),
        patch("nicegui.ui.row"),
        patch("nicegui.ui.button") as mock_button,
    ):
        mock_dialog_inst = MagicMock()
        mock_dialog.return_value.__enter__.return_value = mock_dialog_inst

        app.show_rollback_recovery_dialog(session_info)

        resume_on_click = None
        revert_on_click = None
        for call in mock_button.call_args_list:
            args = call[0]
            kwargs = call[1]
            if kwargs.get("on_click") and len(args) > 0:
                if args[0] == "Resume":
                    resume_on_click = kwargs["on_click"]
                elif args[0] == "Revert":
                    revert_on_click = kwargs["on_click"]

        assert resume_on_click is not None
        assert revert_on_click is not None

        # Verify Resume click starts the resume task
        with patch.object(app, "resume_rollback_session") as mock_resume_session:
            resume_on_click()
            mock_resume_session.assert_called_once_with(session_info)

        # Verify Revert click starts the revert task
        with patch.object(app, "revert_rollback_session") as mock_revert_session:
            revert_on_click()
            mock_revert_session.assert_called_once_with(session_info)
