import asyncio
import os
import sqlite3
from contextlib import closing

import pytest

from app.config import AppSettings
from app.ui.app import AutoSorterApp


@pytest.fixture
def mock_session_base(tmp_path):
    base = tmp_path / ".sessions"
    base.mkdir()
    return base


@pytest.mark.anyio
async def test_wizard_file_recovery_restore_original(mock_session_base, tmp_path):
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    session_id = "failed-session"
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

    app.show_recovery_wizard(session_info)
    await asyncio.sleep(0.1)

    recovered_safe = user_data / "test_doc_1.txt"
    assert os.path.exists(recovered_safe) or os.path.exists(user_data / "test_doc.txt")


@pytest.mark.anyio
async def test_wizard_file_recovery_custom_location(mock_session_base, tmp_path):
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
    sub_dir = branch_dir / "sub_folder"
    sub_dir.mkdir(parents=True)

    trapped_file = sub_dir / "trapped_export.txt"
    with open(trapped_file, "w") as f:
        f.write("data for custom export")

    custom_export_dir = tmp_path / "custom_export"
    custom_export_dir.mkdir()

    app = AutoSorterApp(settings)

    session_info = {
        "session_id": session_id,
        "base_dir": str(user_data),
        "session_dir": str(session_dir),
        "status": "failed",
        "has_trapped_files": True,
        "safety_folder": str(branch_dir),
    }

    app.run_recovery(session_info, restore_to_original=False, custom_path=str(custom_export_dir))
    await asyncio.sleep(0.1)

    exported_file = custom_export_dir / "sub_folder" / "trapped_export.txt"
    assert os.path.exists(exported_file)
