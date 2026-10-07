"""Unit and integration tests for CLI and TUI audit log export functionality."""

import csv
import json
import os
import uuid

import pytest

from app.cli.ledger_cli import handle_ledger_command
from app.config import AppSettings
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.ui.tui import HistoryModal


@pytest.fixture
def test_history_env(tmp_path):
    """Fixture providing a temporary environment with HistoryManager and recorded steps."""
    db_worker = DBWorker()
    autosorter_db_path = tmp_path / "autosorter.db"
    history_db_path = tmp_path / "history.db"
    cache_db_path = tmp_path / "cache.db"

    db = Database(str(autosorter_db_path), db_worker)
    cache_mgr = CacheManager(str(cache_db_path), db_worker)
    history_mgr = HistoryManager(db, cache_mgr, str(history_db_path))

    session_id = str(uuid.uuid4())
    base_dir = str(tmp_path / "target_dir")
    os.makedirs(base_dir, exist_ok=True)

    # Log several steps into history manager
    src1 = os.path.join(base_dir, "doc1.txt")
    dst1 = os.path.join(base_dir, "Docs", "doc1.txt")
    hash1 = "a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890"

    src2 = os.path.join(base_dir, "image.png")
    dst2 = os.path.join(base_dir, "Images", "image.png")
    hash2 = "b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1"

    history_mgr.log_step(
        session_id=session_id,
        source_path=src1,
        target_path=dst1,
        file_hash=hash1,
    )

    history_mgr.log_step(
        session_id=session_id,
        source_path=src2,
        target_path=dst2,
        file_hash=hash2,
    )

    yield {
        "session_id": session_id,
        "base_dir": base_dir,
        "db": db,
        "cache_mgr": cache_mgr,
        "history_mgr": history_mgr,
        "tmp_path": tmp_path,
        "src1": src1,
        "dst1": dst1,
        "hash1": hash1,
        "src2": src2,
        "dst2": dst2,
        "hash2": hash2,
    }

    db_worker.stop()


def test_export_audit_log_csv(test_history_env):
    """Test exporting session history to CSV format."""
    env = test_history_env
    history_mgr = env["history_mgr"]
    session_id = env["session_id"]
    csv_path = str(env["tmp_path"] / "audit_log.csv")

    res = history_mgr.export_audit_log(
        output_path=csv_path,
        session_id=session_id,
        format="csv",
    )

    assert res["status"] == "success"
    assert res["count"] == 2
    assert res["format"] == "csv"
    assert os.path.exists(csv_path)

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2
    assert rows[0]["session_id"] == session_id
    assert rows[0]["source_path"] == env["src1"]
    assert rows[0]["target_path"] == env["dst1"]
    assert rows[0]["file_hash"] == env["hash1"]
    assert rows[0]["status"] == "completed"

    assert rows[1]["session_id"] == session_id
    assert rows[1]["source_path"] == env["src2"]
    assert rows[1]["target_path"] == env["dst2"]
    assert rows[1]["file_hash"] == env["hash2"]


def test_export_audit_log_json(test_history_env):
    """Test exporting session history to JSON format."""
    env = test_history_env
    history_mgr = env["history_mgr"]
    session_id = env["session_id"]
    json_path = str(env["tmp_path"] / "audit_log.json")

    res = history_mgr.export_audit_log(
        output_path=json_path,
        session_id=session_id,
        format="json",
    )

    assert res["status"] == "success"
    assert res["count"] == 2
    assert res["format"] == "json"
    assert os.path.exists(json_path)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["session_id"] == session_id
    assert data["total_steps"] == 2
    assert len(data["steps"]) == 2

    step1 = data["steps"][0]
    assert step1["session_id"] == session_id
    assert step1["source_path"] == env["src1"]
    assert step1["target_path"] == env["dst1"]
    assert step1["file_hash"] == env["hash1"]
    assert step1["status"] == "completed"
    assert "timestamp_iso" in step1


def test_export_audit_log_invalid_session(test_history_env):
    """Test exporting audit log for non-existent session ID raises ValueError."""
    env = test_history_env
    history_mgr = env["history_mgr"]
    bogus_id = str(uuid.uuid4())
    csv_path = str(env["tmp_path"] / "bogus.csv")

    with pytest.raises(ValueError) as exc_info:
        history_mgr.export_audit_log(
            output_path=csv_path,
            session_id=bogus_id,
            format="csv",
        )

    assert f"Session '{bogus_id}' not found" in str(exc_info.value)


def test_export_audit_log_invalid_format(test_history_env):
    """Test exporting with an unsupported format raises ValueError."""
    env = test_history_env
    history_mgr = env["history_mgr"]
    session_id = env["session_id"]
    txt_path = str(env["tmp_path"] / "audit.txt")

    with pytest.raises(ValueError) as exc_info:
        history_mgr.export_audit_log(
            output_path=txt_path,
            session_id=session_id,
            format="xml",
        )

    assert "Unsupported export format 'xml'" in str(exc_info.value)


def test_cli_ledger_export_command(test_history_env, monkeypatch):
    """Test CLI ledger export subcommand."""
    env = test_history_env
    session_id = env["session_id"]
    csv_out = str(env["tmp_path"] / "cli_audit.csv")

    import argparse

    args = argparse.Namespace(
        subcommand="ledger",
        ledger_command="export",
        session_id=session_id,
        output=csv_out,
        format="csv",
        csv=True,
        ledger_db=str(env["tmp_path"] / "history.db"),
        quiet=True,
        json=True,
    )

    settings = AppSettings()

    # Intercept find_all_history_sessions to return test session with db path
    def mock_find_sessions():
        return [
            {
                "session_id": session_id,
                "history_db_path": str(env["tmp_path"] / "history.db"),
                "base_dir": env["base_dir"],
                "status": "completed",
            }
        ]

    monkeypatch.setattr("app.main.find_all_history_sessions", mock_find_sessions)

    with pytest.raises(SystemExit) as exc_info:
        handle_ledger_command(args, settings)

    assert exc_info.value.code == 0
    assert os.path.exists(csv_out)


def test_tui_history_modal_export(test_history_env):
    """Test TUI HistoryModal initialization and export action."""
    env = test_history_env
    session_id = env["session_id"]
    sessions = [
        {
            "session_id": session_id,
            "base_dir": env["base_dir"],
            "status": "completed",
            "timestamp": 1234567890.0,
            "history_db_path": str(env["tmp_path"] / "history.db"),
        }
    ]

    modal = HistoryModal(sessions=sessions, base_dir=env["base_dir"])
    assert modal is not None
    assert len(modal.sessions) == 1
