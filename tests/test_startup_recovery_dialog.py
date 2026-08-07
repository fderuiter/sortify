import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.db_conn import get_db_connection
from app.core.session import scan_abandoned_sessions_async
from app.ui.app import AutoSorterApp


def resolve_test_path(path):
    """Resolve and canonicalize paths consistently across different operating systems.

    On Windows (win32), it resolves short 8.3 names and strips the extended-length prefixes.
    On non-Windows platforms (macOS/Linux), it resolves physical symlinks (like /var or /tmp).
    """
    import sys

    path_str = str(path)
    abs_path = os.path.normpath(os.path.abspath(path_str))
    if sys.platform == "win32":
        try:
            abs_path = os.path.normpath(os.path.realpath(abs_path))
        except Exception:
            pass
        if abs_path.startswith("\\\\?\\UNC\\"):
            abs_path = "\\" + abs_path[7:]
        elif abs_path.startswith("\\\\?\\"):
            abs_path = abs_path[4:]
        if len(abs_path) > 1 and abs_path[1] == ":":
            abs_path = abs_path[0].upper() + abs_path[1:]
    else:
        try:
            abs_path = os.path.normpath(os.path.realpath(abs_path))
        except Exception:
            pass
    return abs_path


@pytest.fixture
def mock_session_base(tmp_path, monkeypatch):
    """Isolate the session base directory so it scans from a known clean temp location."""
    session_base_path = tmp_path / "autosorter_sessions"
    session_base_path.mkdir(parents=True, exist_ok=True)
    session_base = Path(resolve_test_path(session_base_path))

    def mock_get_session_base_dir():
        return session_base

    monkeypatch.setattr(
        "app.core.path_utils.get_session_base_dir", mock_get_session_base_dir
    )
    return session_base


@pytest.mark.anyio
async def test_scan_failed_session_with_trapped_files(mock_session_base, tmp_path):
    """Verify that scan_abandoned_sessions_async properly identifies failed sessions with unrecovered files."""
    resolved_tmp = Path(resolve_test_path(tmp_path))
    session_id = "failed-session-123"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    conn = get_db_connection(str(history_db), cached=False)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(resolved_tmp / "user_data"), "failed"),
            )
    finally:
        conn.close()

    # Create safety folder inside user_data
    user_data = resolved_tmp / "user_data"
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
    resolved_tmp = Path(resolve_test_path(tmp_path))
    session_id = "empty-session-456"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    conn = get_db_connection(str(history_db), cached=False)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(resolved_tmp / "user_data"), "failed"),
            )
    finally:
        conn.close()

    # Create safety folder inside user_data but KEEP IT EMPTY
    user_data = resolved_tmp / "user_data"
    user_data.mkdir()
    branch_dir = user_data / ".branches" / session_id
    branch_dir.mkdir(parents=True)

    abandoned = await scan_abandoned_sessions_async()

    assert len(abandoned) == 0


@pytest.mark.anyio
async def test_ui_recovery_wizard_trigger(mock_session_base, tmp_path):
    """Verify that AutoSorterApp triggers the show_recovery_wizard on startup when an eligible session is found."""
    resolved_tmp = Path(resolve_test_path(tmp_path))
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "trigger-session-789"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    conn = get_db_connection(str(history_db), cached=False)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(resolved_tmp / "user_data"), "failed"),
            )
    finally:
        conn.close()

    user_data = resolved_tmp / "user_data"
    user_data.mkdir()
    branch_dir = user_data / ".branches" / session_id
    branch_dir.mkdir(parents=True)
    with open(branch_dir / "trapped.txt", "w") as f:
        f.write("Trapped content")

    app = AutoSorterApp(settings)

    with patch.object(app, "show_recovery_wizard") as mock_wizard:
        app.check_abandoned_sessions()
        # Poll robustly to prevent timing-related race conditions
        import time

        start_time = time.time()
        while time.time() - start_time < 5.0:
            if mock_wizard.call_count > 0:
                break
            await asyncio.sleep(0.02)
        mock_wizard.assert_called_once()
        called_arg = mock_wizard.call_args[0][0]
        assert called_arg["session_id"] == session_id
        assert called_arg["has_trapped_files"] is True


@pytest.mark.anyio
async def test_wizard_file_recovery_original_location(mock_session_base, tmp_path):
    """Verify the file recovery logic (Restore to Original Folders) works correctly and resolves duplicate names."""
    resolved_tmp = Path(resolve_test_path(tmp_path))
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "recover-session"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    # Database Setup
    history_db = session_dir / "history.db"
    conn = get_db_connection(str(history_db), cached=False)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(resolved_tmp / "user_data"), "failed"),
            )
    finally:
        conn.close()

    # Prepare directories & trapped files
    user_data = resolved_tmp / "user_data"
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

        if restore_on_click:
            restore_on_click()

        # Find and await the background recovery task to complete
        recovery_task = None
        for task in asyncio.all_tasks():
            coro = task.get_coro()
            if coro:
                name = getattr(coro, "__name__", "") or ""
                qualname = getattr(coro, "__qualname__", "") or ""
                if "do_work" in name or "do_work" in qualname:
                    recovery_task = task
                    break

        if recovery_task:
            await recovery_task
        else:
            # Fallback sleep if not immediately registered or already finished
            await asyncio.sleep(0.1)

        # Let's check the result inside the mock context:
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
        conn = get_db_connection(str(history_db), cached=False)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = cursor.fetchone()
            assert row[0] == "resolved"
        finally:
            conn.close()

        # Let any final background tasks (like do_work's last lines) finish under the mocks
        await asyncio.sleep(0.1)


@pytest.mark.anyio
async def test_wizard_file_recovery_custom_location(mock_session_base, tmp_path):
    """Verify that exporting to a custom folder works correctly, preserving structure."""
    resolved_tmp = Path(resolve_test_path(tmp_path))
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "export-session"
    session_dir = mock_session_base / session_id
    session_dir.mkdir()

    history_db = session_dir / "history.db"
    conn = get_db_connection(str(history_db), cached=False)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE sessions (session_id TEXT, timestamp REAL, base_dir TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (session_id, 100.0, str(resolved_tmp / "user_data"), "failed"),
            )
    finally:
        conn.close()

    user_data = resolved_tmp / "user_data"
    user_data.mkdir()

    branch_dir = user_data / ".branches" / session_id
    sub_branch = branch_dir / "sub_folder"
    sub_branch.mkdir(parents=True)

    with open(sub_branch / "trapped_export.txt", "w") as f:
        f.write("data for custom export")

    app = AutoSorterApp(settings)

    custom_export_dir = resolved_tmp / "my_custom_recovery"

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

        if export_on_click:
            export_on_click()

        # Find and await the background recovery task to complete
        recovery_task = None
        for task in asyncio.all_tasks():
            coro = task.get_coro()
            if coro:
                name = getattr(coro, "__name__", "") or ""
                qualname = getattr(coro, "__qualname__", "") or ""
                if "do_work" in name or "do_work" in qualname:
                    recovery_task = task
                    break

        if recovery_task:
            await recovery_task
        else:
            # Fallback sleep if not immediately registered or already finished
            await asyncio.sleep(0.1)

        # Verify the custom export:
        exported_file = custom_export_dir / "sub_folder" / "trapped_export.txt"
        assert os.path.exists(exported_file)
        with open(exported_file, "r") as f:
            assert f.read() == "data for custom export"

        # Verify safety folder is cleaned up
        assert not os.path.exists(branch_dir)

        # Verify status is resolved
        conn = get_db_connection(str(history_db), cached=False)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = cursor.fetchone()
            assert row[0] == "resolved"
        finally:
            conn.close()

        # Let any final background tasks finish under the mocks
        await asyncio.sleep(0.1)
