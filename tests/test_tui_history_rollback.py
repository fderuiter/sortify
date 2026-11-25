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


async def _wait_for_screen(app, screen_cls, pilot, timeout_steps=50):
    """Wait asynchronously for app.screen to match the expected screen class."""
    for _ in range(timeout_steps):
        if isinstance(app.screen, screen_cls):
            return app.screen
        await pilot.pause(0.05)
    return app.screen


async def _wait_for_widget(
    container, selector, widget_type=None, pilot=None, timeout_steps=50
):
    """Wait asynchronously for a child widget matching selector to be mounted in container."""
    for _ in range(timeout_steps):
        try:
            if widget_type:
                return container.query_one(selector, widget_type)
            return container.query_one(selector)
        except Exception:
            if pilot:
                await pilot.pause(0.05)
            else:
                break
    if widget_type:
        return container.query_one(selector, widget_type)
    return container.query_one(selector)


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

    # Start session
    session_id = history_mgr.start_session(str(base_dir))

    # Simulate moving files to target directory and recording in canonical move ledger
    import hashlib

    target_dir = base_dir / "Sorted"
    target_dir.mkdir()
    target1 = target_dir / "doc1.txt"
    target2 = target_dir / "doc2.txt"

    hash1 = hashlib.sha256(file1.read_bytes()).hexdigest()
    hash2 = hashlib.sha256(file2.read_bytes()).hexdigest()

    seq1 = history_mgr.moves.record_intent(
        session_id, str(file1), str(target1), src_sha256=hash1
    )
    shutil.move(str(file1), str(target1))
    history_mgr.moves.mark_done(session_id, seq1, str(target1))

    seq2 = history_mgr.moves.record_intent(
        session_id, str(file2), str(target2), src_sha256=hash2
    )
    shutil.move(str(file2), str(target2))
    history_mgr.moves.mark_done(session_id, seq2, str(target2))

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
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)

        lbl_sid = await _wait_for_widget(
            confirm_screen, "#lbl-confirm-sid", Label, pilot=pilot
        )
        assert session_id in str(lbl_sid.render())

        # Step-ledger sessions always skip missing files, so no force option (#742)
        assert not confirm_screen.query("#chk-ignore-missing")
        lbl_missing = await _wait_for_widget(
            confirm_screen, "#lbl-confirm-missing", Label, pilot=pilot
        )
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
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
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
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
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
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
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
    store_path = os.path.normcase(os.path.abspath(tui_rollback_env["history_db_path"]))

    used_paths = {}
    real_rollback = HistoryManager.rollback

    def spy_rollback(self, sid, ignore_missing=False):
        used_paths["db"] = os.path.normcase(os.path.abspath(self.db.db_path))
        used_paths["cache"] = os.path.normcase(
            os.path.abspath(self.cache_manager.db_path)
        )
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
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
        btn_confirm.press()

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
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
        btn_confirm.press()
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
        assert "successfully undone" in str(lbl_status.render())

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not modal


@pytest.mark.anyio
async def test_rollback_confirm_modal_history_load_failure(monkeypatch, caplog):
    """Verify RollbackConfirmModal displays error banner, disables confirm button, and logs error on history retrieval failure."""
    import logging
    import os

    from textual.widgets import Button, Static

    from app.ui.modals.session import RollbackConfirmModal

    home = os.environ.get("HOME", "/app")

    def mock_open_session_history():
        raise RuntimeError(f"database is locked: {home}/.secret/history.db")

    monkeypatch.setattr(
        "app.core.history.open_session_history", mock_open_session_history
    )

    session_info = {
        "session_id": "sess_locked_db",
        "base_dir": "/tmp/workspace",
        "timestamp": 1700000000,
        "items": [],
    }

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )
    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = RollbackConfirmModal(session_info=session_info)
        with caplog.at_level(logging.ERROR):
            app.push_screen(modal)
            await pilot.pause()

        # 1. Verify history_error captured and sanitized (home path scrubbed to <USER_HOME>)
        assert modal.history_error is not None
        assert "database is locked" in modal.history_error
        assert home not in modal.history_error
        assert "<USER_HOME>" in modal.history_error

        # 2. Verify error banner widget is displayed
        banner = modal.query_one("#rollback-error-banner", Static)
        assert banner.display is True
        assert "database is locked" in str(banner.render())

        # 3. Verify confirm undo button is disabled with explanatory tooltip
        btn_confirm = modal.query_one("#btn-confirm-rollback", Button)
        assert btn_confirm.disabled is True
        assert "Cannot confirm undo" in str(btn_confirm.tooltip)

        # 4. Verify announcement and logging
        assert any(
            "database is locked" in ann["message"] for ann in modal.announcements
        )
        assert any(
            "Failed to load session history for rollback modal" in rec.message
            for rec in caplog.records
        )
        assert any(rec.levelno == logging.ERROR for rec in caplog.records)

        # 5. Verify action_confirm does not dismiss modal when disabled / error present
        modal.action_confirm()
        await pilot.pause()
        assert app.screen is modal


@pytest.mark.anyio
async def test_rollback_confirm_modal_healthy_load():
    """Verify RollbackConfirmModal loads normally without error banner when history is available."""
    from textual.widgets import Button, Static

    from app.ui.modals.session import RollbackConfirmModal

    session_info = {
        "session_id": "sess_healthy",
        "base_dir": "/tmp/workspace",
        "timestamp": 1700000000,
        "items": [
            {
                "step_id": 1,
                "source_path": "/tmp/workspace/doc.txt",
                "target_path": "/tmp/workspace/Sorted/doc.txt",
                "size": 100,
                "status": "completed",
            }
        ],
    }

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = RollbackConfirmModal(session_info=session_info)
        app.push_screen(modal)
        await pilot.pause()

        assert modal.history_error is None

        banner = modal.query_one("#rollback-error-banner", Static)
        assert banner.display is False

        btn_confirm = modal.query_one("#btn-confirm-rollback", Button)
        assert btn_confirm.disabled is False


@pytest.mark.anyio
async def test_undo_last_sort_no_completed_session(tmp_path, monkeypatch):
    """Verify action_undo_last_sort announces error and does not open modal when no completed session exists."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )
    monkeypatch.setattr("app.core.session_catalog.list_sessions", lambda: [])

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        announced: list[str] = []

        def _mock_announce(
            message: str,
            priority: str = "polite",
            help_url: str | None = None,
        ) -> str:
            announced.append(message)
            return message

        app.announce = _mock_announce

        await pilot.press("ctrl+z")
        await pilot.pause()

        assert not isinstance(app.screen, RollbackConfirmModal)
        assert any("No completed sorting session" in m for m in announced)


@pytest.mark.anyio
async def test_undo_last_sort_opens_modal_and_executes_rollback(tui_rollback_env):
    """Verify Ctrl+Z triggers RollbackConfirmModal for latest completed session and executes restoration."""
    session_id = tui_rollback_env["session_id"]
    history_mgr = tui_rollback_env["history_mgr"]
    settings = tui_rollback_env["settings"]

    # Mark session completed in DB
    conn = get_db_connection(history_mgr.db_path)
    with conn:
        conn.execute(
            "UPDATE sessions SET status = 'completed' WHERE session_id = ?",
            (session_id,),
        )

    # Before rollback: targets exist, sources do not
    assert os.path.exists(tui_rollback_env["target1"])
    assert os.path.exists(tui_rollback_env["target2"])
    assert not os.path.exists(tui_rollback_env["file1"])
    assert not os.path.exists(tui_rollback_env["file2"])

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+z")
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        assert isinstance(confirm_screen, RollbackConfirmModal)

        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
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
        with conn:
            cur = conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            )
            status = cur.fetchone()[0]
        assert status == "rolled_back"


@pytest.mark.anyio
async def test_undo_last_sort_rollback_failure(tui_rollback_env, monkeypatch):
    """Verify Ctrl+Z rollback failure displays error announcement and pushes RollbackErrorModal."""
    session_id = tui_rollback_env["session_id"]
    history_mgr = tui_rollback_env["history_mgr"]
    settings = tui_rollback_env["settings"]

    conn = get_db_connection(history_mgr.db_path)
    with conn:
        conn.execute(
            "UPDATE sessions SET status = 'completed' WHERE session_id = ?",
            (session_id,),
        )

    def mock_rollback_error(self, sid, **kwargs):
        raise PermissionError("Disk failure during undo restoration")

    monkeypatch.setattr("app.core.history.HistoryManager.rollback", mock_rollback_error)

    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+z")
        confirm_screen = await _wait_for_screen(app, RollbackConfirmModal, pilot=pilot)
        btn_confirm = await _wait_for_widget(
            confirm_screen, "#btn-confirm-rollback", Button, pilot=pilot
        )
        btn_confirm.press()

        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        error_screen = await _wait_for_screen(app, RollbackErrorModal, pilot=pilot)
        assert isinstance(error_screen, RollbackErrorModal)


@pytest.mark.anyio
async def test_pipeline_execution_announces_undo_available(tmp_path):
    """Verify executing moves announces '[Ctrl+Z] Undo available'."""
    from unittest.mock import MagicMock, patch

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    async with app.run_test() as pilot:
        announced = []
        orig_announce = app.announce

        def track_announce(
            message: str,
            priority: str = "polite",
            help_url: str | None = None,
        ) -> str:
            announced.append(str(message))
            return orig_announce(message, priority=priority, help_url=help_url)

        app.announce = track_announce
        app.app_session = MagicMock()
        app.app_session.execute_moves.return_value = {"moves": 1}
        app.app_session.base_dir = str(tmp_path)
        app.plan = {"doc.txt": {}}

        with (
            patch.object(app, "_get_executable_plan", return_value={"doc.txt": {}}),
            patch.object(app, "_start_scan"),
            patch.object(app, "action_open_audit_report"),
        ):
            app.run_execute_worker()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert any("[Ctrl+Z] Undo available" in msg for msg in announced)


def test_legacy_import_skips_undo_snapshot_rows_without_bumping_schema_version(
    tmp_path,
):
    """Legacy import excludes undo_snapshot rows and child records while keeping STORE_MIGRATIONS at head version."""
    import sqlite3

    from app.core.session_catalog import can_undo_session, list_sessions
    from app.core.store import STORE_MIGRATIONS, open_store

    expected_head = STORE_MIGRATIONS[-1].version

    legacy_root = tmp_path / "legacy_sessions"
    legacy_dir = legacy_root / "sess_dir"
    legacy_dir.mkdir(parents=True)
    legacy_db = legacy_dir / "history.db"
    conn = sqlite3.connect(str(legacy_db))
    with conn:
        conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, timestamp REAL, base_dir TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE step_ledger (step_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, source_path TEXT, target_path TEXT, file_hash TEXT, status TEXT, timestamp REAL)"
        )
        conn.execute(
            "CREATE TABLE snapshot_files (session_id TEXT, filepath TEXT, mtime REAL, size INTEGER)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('real_sess', 1700000000.0, '/tmp/sorted', 'completed')"
        )
        conn.execute(
            "INSERT INTO step_ledger (session_id, source_path, target_path, file_hash, status, timestamp) VALUES ('real_sess', '/tmp/sorted/a.txt', '/tmp/sorted/Docs/a.txt', 'h1', 'completed', 1700000001.0)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES ('snap_sess', 1700000010.0, '/tmp/sorted', 'undo_snapshot')"
        )
        conn.execute(
            "INSERT INTO step_ledger (session_id, source_path, target_path, file_hash, status, timestamp) VALUES ('snap_sess', '/tmp/sorted/Docs/a.txt', '/tmp/sorted/a.txt', 'h1', 'completed', 1700000011.0)"
        )
        conn.execute(
            "INSERT INTO snapshot_files VALUES ('snap_sess', '/tmp/sorted/a.txt', 1.0, 10)"
        )
    conn.close()

    store = open_store(tmp_path / "app_store", legacy_session_root=legacy_root)
    listed = list_sessions(store)
    assert [s["session_id"] for s in listed] == ["real_sess"]
    assert can_undo_session(listed[0]) is True

    store_conn = get_db_connection(str(store.path))
    with store_conn:
        assert store_conn.execute("PRAGMA user_version").fetchone()[0] == expected_head
        sids = {
            r[0]
            for r in store_conn.execute("SELECT session_id FROM sessions").fetchall()
        }
        move_sids = {
            r[0] for r in store_conn.execute("SELECT session_id FROM moves").fetchall()
        }
        snap_sids = {
            r[0]
            for r in store_conn.execute(
                "SELECT session_id FROM snapshot_files"
            ).fetchall()
        }
    assert "snap_sess" not in sids
    assert "snap_sess" not in move_sids
    assert "snap_sess" not in snap_sids


@pytest.mark.anyio
async def test_history_modal_disables_undo_for_non_undoable_sessions_and_aligns_terminology(
    tui_rollback_env,
):
    """HistoryModal disables Undo Session for 0-move/abandoned/undo_snapshot sessions and preserves Rollback on recovery modals."""
    from app.core.session_catalog import can_undo_session
    from app.ui.modals.session import MultiSessionRecoveryModal, SessionRecoveryModal

    assert (
        can_undo_session(
            {
                "session_id": "s1",
                "status": "completed",
                "step_count": 0,
                "file_count": 0,
            }
        )
        is False
    )
    assert (
        can_undo_session(
            {
                "session_id": "s2",
                "status": "completed",
                "step_count": 0,
                "trashed_count": 1,
            }
        )
        is True
    )
    assert (
        can_undo_session({"session_id": "s3", "status": "abandoned", "step_count": 3})
        is False
    )
    assert (
        can_undo_session({"session_id": "s4", "status": "rolled_back", "step_count": 3})
        is False
    )
    assert (
        can_undo_session(
            {"session_id": "s5", "status": "undo_snapshot", "step_count": 3}
        )
        is False
    )

    zero_move_session = {
        "session_id": "zero_move_12345678",
        "base_dir": tui_rollback_env["base_dir"],
        "status": "completed",
        "timestamp": 1700000500.0,
        "step_count": 0,
        "file_count": 0,
    }
    abandoned_session = {
        "session_id": "abandoned_12345678",
        "base_dir": tui_rollback_env["base_dir"],
        "status": "abandoned",
        "timestamp": 1700000400.0,
        "step_count": 2,
        "file_count": 2,
    }
    snapshot_session = {
        "session_id": "snapshot_12345678",
        "base_dir": tui_rollback_env["base_dir"],
        "status": "undo_snapshot",
        "timestamp": 1700000600.0,
        "step_count": 2,
        "file_count": 2,
    }

    settings = tui_rollback_env["settings"]
    app = AutoSorterTUI(settings=settings)
    async with app.run_test() as pilot:
        modal = HistoryModal(
            sessions=[zero_move_session, abandoned_session, snapshot_session],
            base_dir=tui_rollback_env["base_dir"],
        )
        app.push_screen(modal)
        await pilot.pause()

        # undo_snapshot is filtered out of HistoryModal
        assert [s["session_id"] for s in modal.sessions] == [
            "zero_move_12345678",
            "abandoned_12345678",
        ]
        assert "Undo" in str(modal.query_one("#modal-title").render())
        btn_undo = modal.query_one("#btn-rollback-session", Button)
        assert "Undo Session" in str(btn_undo.label)

        select_sess = modal.query_one("#select-history-session", Select)
        select_sess.value = "zero_move_12345678"
        await pilot.pause()
        assert btn_undo.disabled is True

        # Even direct action invocation refuses cleanly without raising InvalidStateTransitionError
        modal.action_rollback_session()
        await pilot.pause()
        assert app.screen is modal

        select_sess.value = "abandoned_12345678"
        await pilot.pause()
        assert btn_undo.disabled is True

        # Recovery modals retain Rollback terminology per CONTEXT.md
        rec_modal = SessionRecoveryModal(session_info=abandoned_session)
        app.push_screen(rec_modal)
        await pilot.pause()
        assert "Rollback" in str(rec_modal.query_one("#btn-rollback", Button).label)
        app.pop_screen()
        await pilot.pause()

        multi_rec = MultiSessionRecoveryModal(sessions=[abandoned_session])
        app.push_screen(multi_rec)
        await pilot.pause()
        assert "Rollback" in str(multi_rec.query_one("#btn-rollback-all", Button).label)
