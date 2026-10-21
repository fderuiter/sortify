import asyncio
import os
import shutil

import pytest
from textual.widgets import Button, Checkbox, Label, Select

from app.config import AppSettings
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.store import open_store
from app.ui.tui import (
    AutoSorterTUI,
    HistoryModal,
    RollbackConfirmModal,
    RollbackErrorModal,
)


@pytest.fixture
def tui_rollback_env(tmp_path, monkeypatch):
    """Fixture to set up a test environment with a historical sorting session and step ledger."""
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()

    # Create original files
    file1 = base_dir / "doc1.txt"
    file2 = base_dir / "doc2.txt"
    file1.write_text("Content of doc1")
    file2.write_text("Content of doc2")

    # The TUI opens the default app store, so point it at this test's store.
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app"))
    # The session below stays unfinished, and the startup recovery prompt
    # would otherwise open over the history modal once its scan returns.
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    db_worker = DBWorker()
    autosorter_db = store.database(db_worker)
    history_mgr = store.history(autosorter_db, store.cache(db_worker))
    history_db_path = store.path

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
    # Keep the first-run onboarding wizard from popping over the modals under test.
    settings.AI_CONSENT_GRANTED = False

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
        await pilot.pause(0.05)

        # RollbackConfirmModal should now be the top screen
        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)

        lbl_sid = confirm_screen.query_one("#lbl-confirm-sid", Label)
        assert session_id in str(lbl_sid.render())

        # Step-ledger sessions always skip missing files, so no force option (#742)
        assert not confirm_screen.query("#chk-ignore-missing")
        lbl_missing = confirm_screen.query_one("#lbl-confirm-missing", Label)
        assert "skipped" in str(lbl_missing.render())


@pytest.mark.anyio
async def test_rollback_confirm_modal_offers_force_without_step_ledger(
    tui_rollback_env,
):
    """Verify the force option is offered only for sessions without step-ledger rows (#742)."""
    session_info = dict(tui_rollback_env["sessions"][0], step_count=0)
    settings = tui_rollback_env["settings"]

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        confirm_screen = RollbackConfirmModal(session_info=session_info)
        app.push_screen(confirm_screen)
        await pilot.pause()

        chk_force = confirm_screen.query_one("#chk-ignore-missing", Checkbox)
        assert not chk_force.value
        assert not confirm_screen.query("#lbl-confirm-missing")


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
        await pilot.pause(0.05)

        # Confirm rollback
        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)
        btn_confirm = confirm_screen.query_one("#btn-confirm-rollback", Button)
        btn_confirm.press()

        # Await worker execution
        await pilot.pause()
        await app.workers.wait_for_complete()
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
        await pilot.pause(0.05)

        # Step-ledger sessions skip missing files without a force option
        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)

        btn_confirm = confirm_screen.query_one("#btn-confirm-rollback", Button)
        btn_confirm.press()

        # Await worker execution
        await pilot.pause()
        await app.workers.wait_for_complete()
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
        await pilot.pause(0.05)

        confirm_screen = app.screen
        assert isinstance(confirm_screen, RollbackConfirmModal)

        btn_confirm = confirm_screen.query_one("#btn-confirm-rollback", Button)
        btn_confirm.press()

        # Await worker execution
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        # RollbackErrorModal should now be displayed
        error_screen = app.screen
        assert isinstance(error_screen, RollbackErrorModal)
        lbl_err = error_screen.query_one("#lbl-error-message", Label)
        assert "Access denied" in str(lbl_err.render())


@pytest.mark.anyio
async def test_rollback_uses_the_app_store(tui_rollback_env, monkeypatch):
    """Verify a rollback updates documents and caches in the app store (ADR 0008)."""
    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    settings = tui_rollback_env["settings"]
    store_path = os.path.abspath(tui_rollback_env["history_db_path"])

    used_paths = {}
    real_rollback = HistoryManager.rollback

    def spy_rollback(self, sid, ignore_missing=False):
        used_paths["db"] = os.path.abspath(self.db.db_path)
        used_paths["cache"] = os.path.abspath(self.cache_manager.db_path)
        return real_rollback(self, sid, ignore_missing=ignore_missing)

    monkeypatch.setattr("app.core.history.HistoryManager.rollback", spy_rollback)

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#select-history-session", Select).value = session_id
        await pilot.pause()
        modal.query_one("#btn-rollback-session", Button).press()
        await pilot.pause(0.05)
        app.screen.query_one("#btn-confirm-rollback", Button).press()

        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert used_paths == {"db": store_path, "cache": store_path}


def test_open_session_history_stops_its_db_worker(tmp_path):
    """Verify each rollback releases the DBWorker thread it starts (#742)."""
    import threading

    from app.core.history import open_session_history

    def worker_threads():
        return [
            t
            for t in threading.enumerate()
            if t.name == "DBWorkerThread" and t.is_alive()
        ]

    before = len(worker_threads())
    for _ in range(3):
        store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
        with open_session_history(store) as history_mgr:
            assert history_mgr.db.worker.thread.is_alive()
    assert len(worker_threads()) == before


@pytest.mark.anyio
async def test_escape_does_not_close_history_modal_mid_rollback(
    tui_rollback_env, monkeypatch
):
    """Verify Escape keeps the History modal open until the rollback result is shown (#742)."""
    import threading

    sessions = tui_rollback_env["sessions"]
    session_id = tui_rollback_env["session_id"]
    settings = tui_rollback_env["settings"]

    started = threading.Event()
    release = threading.Event()

    def blocking_rollback(self, sid, ignore_missing=False):
        started.set()
        release.wait(timeout=10)
        return 2

    monkeypatch.setattr("app.core.history.HistoryManager.rollback", blocking_rollback)
    monkeypatch.setattr("app.core.session_catalog.list_sessions", lambda: sessions)

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(sessions=sessions, base_dir=tui_rollback_env["base_dir"])
        app.push_screen(modal)
        await pilot.pause()

        modal.query_one("#select-history-session", Select).value = session_id
        await pilot.pause()
        modal.query_one("#btn-rollback-session", Button).press()
        await pilot.pause(0.05)
        app.screen.query_one("#btn-confirm-rollback", Button).press()
        await pilot.pause()

        try:
            assert await asyncio.to_thread(started.wait, 10)
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is modal
        finally:
            release.set()

        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.screen is modal
        lbl_status = modal.query_one("#lbl-rollback-status", Label)
        assert "successfully rolled back" in str(lbl_status.render())

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not modal
