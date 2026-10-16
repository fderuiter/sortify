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


def test_export_audit_log_all_sessions_consolidation(tmp_path, monkeypatch):
    """Test exporting without session_id consolidates entries across multiple databases."""
    db_worker = DBWorker()
    autosorter_db_path = tmp_path / "autosorter.db"
    cache_db_path = tmp_path / "cache.db"
    db = Database(str(autosorter_db_path), db_worker)
    cache_mgr = CacheManager(str(cache_db_path), db_worker)

    db1_path = tmp_path / "sess1" / "history.db"
    db2_path = tmp_path / "sess2" / "history.db"
    os.makedirs(db1_path.parent, exist_ok=True)
    os.makedirs(db2_path.parent, exist_ok=True)

    hm1 = HistoryManager(db, cache_mgr, str(db1_path))
    hm2 = HistoryManager(db, cache_mgr, str(db2_path))

    s1_id = str(uuid.uuid4())
    s2_id = str(uuid.uuid4())

    hm1.log_step(s1_id, "/src/a.txt", "/dst/a.txt", file_hash="hash_a")
    hm1.log_step(s1_id, "/src/b.txt", "/dst/b.txt", file_hash="hash_b")

    hm2.log_step(s2_id, "/src/c.txt", "/dst/c.txt", file_hash="hash_c")

    def mock_find_sessions():
        return [
            {
                "session_id": s1_id,
                "history_db_path": str(db1_path),
                "base_dir": "/dst",
                "status": "completed",
            },
            {
                "session_id": s2_id,
                "history_db_path": str(db2_path),
                "base_dir": "/dst",
                "status": "completed",
            },
        ]

    monkeypatch.setattr("app.main.find_all_history_sessions", mock_find_sessions)

    json_out = str(tmp_path / "aggregated.json")
    res = hm1.export_audit_log(json_out, session_id=None, format="json")

    assert res["status"] == "success"
    assert res["count"] == 3

    with open(json_out, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["total_steps"] == 3
    sids = {step["session_id"] for step in data["steps"]}
    assert sids == {s1_id, s2_id}

    db_worker.stop()


def test_export_audit_log_secondary_db_by_session_id(tmp_path, monkeypatch):
    """Test exporting specific session_id searches secondary databases if primary db_path does not contain it."""
    db_worker = DBWorker()
    autosorter_db_path = tmp_path / "autosorter.db"
    cache_db_path = tmp_path / "cache.db"
    db = Database(str(autosorter_db_path), db_worker)
    cache_mgr = CacheManager(str(cache_db_path), db_worker)

    primary_db = tmp_path / "primary" / "history.db"
    secondary_db = tmp_path / "secondary" / "history.db"
    os.makedirs(primary_db.parent, exist_ok=True)
    os.makedirs(secondary_db.parent, exist_ok=True)

    hm_primary = HistoryManager(db, cache_mgr, str(primary_db))
    hm_secondary = HistoryManager(db, cache_mgr, str(secondary_db))

    s2_id = str(uuid.uuid4())
    hm_secondary.log_step(s2_id, "/src/sec.txt", "/dst/sec.txt", file_hash="sec_hash")

    def mock_find_sessions():
        return [
            {
                "session_id": s2_id,
                "history_db_path": str(secondary_db),
                "base_dir": "/dst",
                "status": "completed",
            }
        ]

    monkeypatch.setattr("app.main.find_all_history_sessions", mock_find_sessions)

    csv_out = str(tmp_path / "secondary.csv")
    # Export using primary HistoryManager, searching for s2_id in secondary database
    res = hm_primary.export_audit_log(csv_out, session_id=s2_id, format="csv")

    assert res["status"] == "success"
    assert res["count"] == 1

    with open(csv_out, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["session_id"] == s2_id
    assert rows[0]["source_path"] == "/src/sec.txt"

    db_worker.stop()


def test_export_audit_log_corrupted_db_handling(tmp_path, monkeypatch):
    """Test that corrupted or unreadable database files are safely ignored during aggregation."""
    db_worker = DBWorker()
    autosorter_db_path = tmp_path / "autosorter.db"
    cache_db_path = tmp_path / "cache.db"
    db = Database(str(autosorter_db_path), db_worker)
    cache_mgr = CacheManager(str(cache_db_path), db_worker)

    valid_db = tmp_path / "valid" / "history.db"
    corrupt_db = tmp_path / "corrupt" / "history.db"
    os.makedirs(valid_db.parent, exist_ok=True)
    os.makedirs(corrupt_db.parent, exist_ok=True)

    hm = HistoryManager(db, cache_mgr, str(valid_db))
    s_id = str(uuid.uuid4())
    hm.log_step(s_id, "/src/v.txt", "/dst/v.txt", file_hash="hash_v")

    # Create corrupted database file
    with open(corrupt_db, "w", encoding="utf-8") as f:
        f.write("this is not a valid sqlite database file")

    def mock_find_sessions():
        return [
            {
                "session_id": s_id,
                "history_db_path": str(valid_db),
                "base_dir": "/dst",
                "status": "completed",
            },
            {
                "session_id": "corrupt-id",
                "history_db_path": str(corrupt_db),
                "base_dir": "/dst",
                "status": "completed",
            },
        ]

    monkeypatch.setattr("app.main.find_all_history_sessions", mock_find_sessions)

    csv_out = str(tmp_path / "corrupt_test.csv")
    res = hm.export_audit_log(csv_out, session_id=None, format="csv")

    assert res["status"] == "success"
    assert res["count"] == 1

    db_worker.stop()


def test_cli_ledger_export_without_session_id(tmp_path, monkeypatch):
    """Test CLI ledger export when session_id is omitted (aggregates all sessions)."""
    db_worker = DBWorker()
    autosorter_db_path = tmp_path / "autosorter.db"
    cache_db_path = tmp_path / "cache.db"
    db = Database(str(autosorter_db_path), db_worker)
    cache_mgr = CacheManager(str(cache_db_path), db_worker)

    db_path = tmp_path / "history.db"
    hm = HistoryManager(db, cache_mgr, str(db_path))
    s1_id = str(uuid.uuid4())
    s2_id = str(uuid.uuid4())
    hm.log_step(s1_id, "/src/1.txt", "/dst/1.txt", file_hash="h1")
    hm.log_step(s2_id, "/src/2.txt", "/dst/2.txt", file_hash="h2")

    csv_out = str(tmp_path / "cli_all.csv")

    import argparse

    args = argparse.Namespace(
        subcommand="ledger",
        ledger_command="export",
        session_id=None,
        output=csv_out,
        format="csv",
        csv=True,
        ledger_db=str(db_path),
        quiet=True,
        json=False,
    )

    settings = AppSettings()

    def mock_find_sessions():
        return [
            {
                "session_id": s1_id,
                "history_db_path": str(db_path),
                "base_dir": "/dst",
                "status": "completed",
            },
            {
                "session_id": s2_id,
                "history_db_path": str(db_path),
                "base_dir": "/dst",
                "status": "completed",
            },
        ]

    monkeypatch.setattr("app.main.find_all_history_sessions", mock_find_sessions)

    with pytest.raises(SystemExit) as exc_info:
        handle_ledger_command(args, settings)

    assert exc_info.value.code == 0
    assert os.path.exists(csv_out)

    with open(csv_out, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2

    db_worker.stop()

