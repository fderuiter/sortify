import asyncio
import os
import sqlite3
from contextlib import closing
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
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


@pytest.mark.anyio
async def test_scan_failed_session_with_trapped_files(mock_session_base, tmp_path):
    """Verify that scan_abandoned_sessions_async properly identifies failed sessions with unrecovered files."""
    session_id = "failed-session-123"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            cursor.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(tmp_path / "user_data"), "failed"),
            )
        conn.commit()

    # Create safety folder inside user_data
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    branch_dir = user_data / ".branches" / session_id
    branch_dir.mkdir(parents=True)

    # Put a trapped file inside
    trapped_file = branch_dir / "trapped.txt"
    with open(trapped_file, "w") as f:
        f.write("I am trapped!")

    abandoned = await scan_abandoned_sessions_async()

    assert len(abandoned) == 1
    assert abandoned[0]["session_id"] == session_id
    assert abandoned[0]["has_trapped_files"] is True
    assert abandoned[0]["status"] == "failed"
    assert abandoned[0]["safety_folder"] == str(branch_dir)


@pytest.mark.anyio
async def test_scan_does_not_prompt_if_safety_folder_empty(mock_session_base, tmp_path):
    """Verify that the system does not identify failed sessions if the safety folder contains no files."""
    session_id = "empty-session-456"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            cursor.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(tmp_path / "user_data"), "failed"),
            )
        conn.commit()

    # Create safety folder inside user_data but KEEP IT EMPTY
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    branch_dir = user_data / ".branches" / session_id
    branch_dir.mkdir(parents=True)

    abandoned = await scan_abandoned_sessions_async()

    assert len(abandoned) == 0


@pytest.mark.anyio
async def test_ui_recovery_wizard_trigger(mock_session_base, tmp_path):
    """Verify that AutoSorterApp triggers the show_recovery_wizard on startup when an eligible session is found."""
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "trigger-session-789"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            cursor.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(tmp_path / "user_data"), "failed"),
            )
        conn.commit()

    user_data = tmp_path / "user_data"
    user_data.mkdir()
    branch_dir = user_data / ".branches" / session_id
    branch_dir.mkdir(parents=True)
    with open(branch_dir / "trapped.txt", "w") as f:
        f.write("Trapped content")

    app = AutoSorterApp(settings)

    with patch.object(app, "show_recovery_wizard") as mock_wizard:
        app.check_abandoned_sessions()
        await asyncio.sleep(0.1)
        mock_wizard.assert_called_once()
        called_arg = mock_wizard.call_args[0][0]
        assert called_arg["session_id"] == session_id
        assert called_arg["has_trapped_files"] is True


@pytest.mark.anyio
async def test_wizard_file_recovery_original_location(mock_session_base, tmp_path):
    """Verify the file recovery logic (Restore to Original Folders) works correctly and resolves duplicate names."""
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "recover-session"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    # Database Setup
    history_db = session_dir / "history.db"
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            cursor.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(tmp_path / "user_data"), "failed"),
            )
        conn.commit()

    # Prepare directories & trapped files
    user_data = tmp_path / "user_data"
    user_data.mkdir()

    # Pre-create a file in user_data to trigger duplicate resolving name conflict rule
    existing_file = user_data / "test_doc.txt"
    with open(existing_file, "w") as f:
        f.write("already exists")

    branch_dir = user_data / ".branches" / session_id
    branch_dir.mkdir(parents=True)

    trapped_file = branch_dir / "test_doc.txt"
    with open(trapped_file, "w") as f:
        f.write("trapped file data")

    app = AutoSorterApp(settings)

    session_info = {
        "session_id": session_id,
        "base_dir": str(user_data),
        "session_dir": str(session_dir),
        "status": "failed",
        "has_trapped_files": True,
        "safety_folder": str(branch_dir),
    }

    # We mock NiceGUI elements/methods inside show_recovery_wizard so it runs headlessly
    with (
        patch("nicegui.ui.dialog") as mock_dialog,
        patch("nicegui.ui.card"),
        patch("nicegui.ui.label"),
        patch("nicegui.ui.column"),
        patch("nicegui.ui.row"),
        patch("nicegui.ui.button") as mock_button,
        patch("nicegui.ui.input"),
        patch("nicegui.ui.linear_progress"),
        patch("nicegui.ui.scroll_area"),
    ):
        mock_dialog_inst = MagicMock()
        mock_dialog.return_value.__enter__.return_value = mock_dialog_inst

        # Trigger show_recovery_wizard
        app.show_recovery_wizard(session_info)

        # Retrieve the inner run_recovery function by inspecting mock call to button
        restore_on_click = None
        for call in mock_button.call_args_list:
            kwargs = call[1]
            if kwargs.get("on_click"):
                args = call[0]
                if len(args) > 0 and "Restore" in args[0]:
                    restore_on_click = kwargs["on_click"]

        # Intercept and capture the background recovery task to await it directly.
        # This prevents any timing-related race conditions or database locking on slow Windows CI runners.
        created_tasks = []
        original_create_task = asyncio.create_task

        def mock_create_task(coro, *args, **kwargs):
            task = original_create_task(coro, *args, **kwargs)
            created_tasks.append(task)
            return task

        with patch("app.ui.app.asyncio.create_task", side_effect=mock_create_task):
            if restore_on_click:
                restore_on_click()

            if created_tasks:
                await asyncio.gather(*created_tasks)

    # Let's check the result:
    # 1. Trapped file should be recovered. Since test_doc.txt existed, it should be named test_doc_1.txt
    recovered_safe = user_data / "test_doc_1.txt"
    assert os.path.exists(recovered_safe)
    with open(recovered_safe, "r") as f:
        assert f.read() == "trapped file data"

    # 2. Existing file should be untouched
    assert os.path.exists(existing_file)
    with open(existing_file, "r") as f:
        assert f.read() == "already exists"

    # 3. Hidden safety folder and its parents should be cleaned up / deleted
    assert not os.path.exists(branch_dir)

    # 4. Status in database should be updated to 'resolved'
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = cursor.fetchone()
            assert row[0] == "resolved"


@pytest.mark.anyio
async def test_wizard_file_recovery_custom_location(mock_session_base, tmp_path):
    """Verify that exporting to a custom folder works correctly, preserving structure."""
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "export-session"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            cursor.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(tmp_path / "user_data"), "failed"),
            )
        conn.commit()

    user_data = tmp_path / "user_data"
    user_data.mkdir()

    branch_dir = user_data / ".branches" / session_id
    sub_branch = branch_dir / "sub_folder"
    sub_branch.mkdir(parents=True)

    with open(sub_branch / "trapped_export.txt", "w") as f:
        f.write("data for custom export")

    app = AutoSorterApp(settings)

    custom_export_dir = tmp_path / "my_custom_recovery"

    session_info = {
        "session_id": session_id,
        "base_dir": str(user_data),
        "session_dir": str(session_dir),
        "status": "failed",
        "has_trapped_files": True,
        "safety_folder": str(branch_dir),
    }

    # We patch show_recovery_wizard's UI part to use run_recovery(restore_to_original=False, custom_path=...)
    with (
        patch("nicegui.ui.dialog") as mock_dialog,
        patch("nicegui.ui.card"),
        patch("nicegui.ui.label"),
        patch("nicegui.ui.column"),
        patch("nicegui.ui.row"),
        patch("nicegui.ui.button") as mock_button,
        patch("nicegui.ui.input") as mock_input,
        patch("nicegui.ui.linear_progress"),
        patch("nicegui.ui.scroll_area"),
    ):
        mock_dialog_inst = MagicMock()
        mock_dialog.return_value.__enter__.return_value = mock_dialog_inst

        mock_input_inst = MagicMock()
        mock_input_inst.classes.return_value = mock_input_inst
        mock_input_inst.props.return_value = mock_input_inst
        mock_input_inst.value = str(custom_export_dir)
        mock_input.return_value = mock_input_inst

        # Let's run show_recovery_wizard
        app.show_recovery_wizard(session_info)

        # Let's find button clicks
        export_on_click = None
        for call in mock_button.call_args_list:
            kwargs = call[1]
            if kwargs.get("on_click"):
                args = call[0]
                if len(args) > 0 and "Export" in args[0]:
                    export_on_click = kwargs["on_click"]

        # Intercept and capture the background recovery task to await it directly.
        # This prevents any timing-related race conditions or database locking on slow Windows CI runners.
        created_tasks = []
        original_create_task = asyncio.create_task

        def mock_create_task(coro, *args, **kwargs):
            task = original_create_task(coro, *args, **kwargs)
            created_tasks.append(task)
            return task

        with patch("app.ui.app.asyncio.create_task", side_effect=mock_create_task):
            if export_on_click:
                export_on_click()

            if created_tasks:
                await asyncio.gather(*created_tasks)

    # Verify the custom export:
    exported_file = custom_export_dir / "sub_folder" / "trapped_export.txt"
    assert os.path.exists(exported_file)
    with open(exported_file, "r") as f:
        assert f.read() == "data for custom export"

    # Verify safety folder is cleaned up
    assert not os.path.exists(branch_dir)

    # Verify status is resolved
    with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = cursor.fetchone()
            assert row[0] == "resolved"
