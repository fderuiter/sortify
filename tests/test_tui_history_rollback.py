import os
import shutil

import pytest
from textual.widgets import Button, Checkbox, Label, Select

from app.config import AppSettings
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.ui.tui import (
    AutoSorterTUI,
    HistoryModal,
    RollbackConfirmModal,
    RollbackErrorModal,
)


@pytest.fixture
def tui_rollback_env(tmp_path):
    """Fixture to set up a test environment with a historical sorting session and step ledger."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()

    # Create original files
    file1 = base_dir / "doc1.txt"
    file2 = base_dir / "doc2.txt"
    file1.write_text("Content of doc1")
    file2.write_text("Content of doc2")

    db_worker = DBWorker()
    autosorter_db = Database(str(tmp_path / "autosorter.db"), db_worker)
    cache_mgr = CacheManager(str(tmp_path / "cache.db"), db_worker)
    history_db_path = tmp_path / "history.db"

    history_mgr = HistoryManager(autosorter_db, cache_mgr, str(history_db_path))

    # Take snapshot
    session_id = history_mgr.create_snapshot(str(base_dir))

    # Simulate moving files to target directory
    target_dir = base_dir / "Sorted"
    target_dir.mkdir()
    target1 = target_dir / "doc1.txt"
    target2 = target_dir / "doc2.txt"

    shutil.move(str(file1), str(target1))
    shutil.move(str(file2), str(target2))

    # Log relocation steps in step ledger
    history_mgr.log_step(
        session_id=session_id,
        source_path=str(file1),
        target_path=str(target1),
        file_hash="hash1",
    )
    history_mgr.log_step(
        session_id=session_id,
        source_path=str(file2),
        target_path=str(target2),
        file_hash="hash2",
    )

    sessions = history_mgr.get_sessions()
    for s in sessions:
        s["history_db_path"] = str(history_db_path)

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))

    return {
        "tmp_path": tmp_path,
        "base_dir": str(base_dir),
        "session_id": session_id,
        "history_db_path": str(history_db_path),
        "file1": str(file1),
        "file2": str(file2),
        "target1": str(target1),
        "target2": str(target2),
        "sessions": sessions,
        "history_mgr": history_mgr,
        "settings": settings,
    }


@pytest.mark.anyio
async def test_history_modal_renders_rollback_button(tui_rollback_env):
    """Verify HistoryModal renders Rollback Session button and handles selection state."""
    sessions = tui_rollback_env["sessions"]
    base_dir = tui_rollback_env["base_dir"]
    settings = tui_rollback_env["settings"]

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=base_dir)
        app.push_screen(modal)
        await pilot.pause()

        btn_rollback = modal.query_one("#btn-rollback-session", Button)
        btn_export = modal.query_one("#btn-export", Button)
        btn_cancel = modal.query_one("#btn-cancel", Button)

        assert btn_rollback is not None
        assert btn_export is not None
        assert btn_cancel is not None

        # When an active/completed session is selected, rollback button is enabled
        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = tui_rollback_env["session_id"]
        await pilot.pause()

        assert not btn_rollback.disabled


@pytest.mark.anyio
async def test_history_modal_disabled_for_rolled_back_session(tui_rollback_env):
    """Verify Rollback Session button is disabled for sessions already marked as rolled_back."""
    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    sessions[0]["status"] = "rolled_back"
    settings = tui_rollback_env["settings"]

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = session_id
        await pilot.pause()

        btn_rollback = modal.query_one("#btn-rollback-session", Button)
        assert btn_rollback.disabled


@pytest.mark.anyio
async def test_rollback_confirm_modal_flow(tui_rollback_env):
    """Verify clicking Rollback Session opens RollbackConfirmModal with session metadata."""
    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    settings = tui_rollback_env["settings"]

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = session_id
        await pilot.pause()

        btn_rollback = modal.query_one("#btn-rollback-session", Button)
        btn_rollback.press()
        await pilot.pause()

        # RollbackConfirmModal should now be the top screen
        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)

        lbl_sid = confirm_screen.query_one("#lbl-confirm-sid", Label)
        assert session_id in str(lbl_sid.render())

        chk_force = confirm_screen.query_one("#chk-ignore-missing", Checkbox)
        assert chk_force is not None
        assert not chk_force.value


@pytest.mark.anyio
async def test_rollback_execution_success(tui_rollback_env):
    """Verify confirming rollback unwinds file relocations and updates session status in DB."""
    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    history_mgr = tui_rollback_env["history_mgr"]
    settings = tui_rollback_env["settings"]

    # Before rollback: targets exist, sources do not
    assert os.path.exists(tui_rollback_env["target1"])
    assert os.path.exists(tui_rollback_env["target2"])
    assert not os.path.exists(tui_rollback_env["file1"])
    assert not os.path.exists(tui_rollback_env["file2"])

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = session_id
        await pilot.pause()

        btn_rollback = modal.query_one("#btn-rollback-session", Button)
        btn_rollback.press()
        await pilot.pause()

        # Confirm rollback
        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)
        btn_confirm = confirm_screen.query_one("#btn-confirm-rollback", Button)
        btn_confirm.press()

        # Await worker execution
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        # Files should now be restored to original locations
        assert os.path.exists(tui_rollback_env["file1"])
        assert os.path.exists(tui_rollback_env["file2"])
        assert not os.path.exists(tui_rollback_env["target1"])

        # Check session status in DB
        conn = get_db_connection(history_mgr.db_path)
        with conn:
            cur = conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            )
            status = cur.fetchone()[0]
        assert status == "rolled_back"


@pytest.mark.anyio
async def test_rollback_execution_missing_files_flow(tui_rollback_env):
    """Verify attempting rollback with missing target files triggers error dialog or force option."""
    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    settings = tui_rollback_env["settings"]

    # Delete one of the target files to simulate missing file
    os.remove(tui_rollback_env["target1"])

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = session_id
        await pilot.pause()

        btn_rollback = modal.query_one("#btn-rollback-session", Button)
        btn_rollback.press()
        await pilot.pause()

        # Confirm WITH ignore_missing checked
        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)

        chk_force = confirm_screen.query_one("#chk-ignore-missing", Checkbox)
        chk_force.value = True

        btn_confirm = confirm_screen.query_one("#btn-confirm-rollback", Button)
        btn_confirm.press()

        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        # Target2 should be restored to file2
        assert os.path.exists(tui_rollback_env["file2"])
        assert not os.path.exists(tui_rollback_env["target2"])


@pytest.mark.anyio
async def test_rollback_execution_error_modal_on_failure(tui_rollback_env, monkeypatch):
    """Verify rollback failure pushes RollbackErrorModal cleanly without closing application."""
    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    settings = tui_rollback_env["settings"]

    def mock_rollback_error(self, sid, ignore_missing=False):
        raise PermissionError("Access denied when restoring files")

    monkeypatch.setattr("app.core.history.HistoryManager.rollback", mock_rollback_error)

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = session_id
        await pilot.pause()

        btn_rollback = modal.query_one("#btn-rollback-session", Button)
        btn_rollback.press()
        await pilot.pause()

        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)

        btn_confirm = confirm_screen.query_one("#btn-confirm-rollback", Button)
        btn_confirm.press()

        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        # RollbackErrorModal should now be displayed
        error_screen = app.screen
        assert isinstance(error_screen, RollbackErrorModal)
        lbl_err = error_screen.query_one("#lbl-error-message", Label)
        assert "Access denied" in str(lbl_err.render())
