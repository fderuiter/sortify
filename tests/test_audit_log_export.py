"""Unit and integration tests for CLI and TUI audit log export functionality."""

import csv
import json
import os
import uuid

import pytest

from app.cli.ledger_cli import handle_ledger_command
from app.config import AppSettings
from app.core.db_worker import DBWorker
from app.core.store import open_store
from app.ui.tui import HistoryModal


def _store_history(tmp_path, monkeypatch, db_worker):
    """Return a HistoryManager on a test app store that the CLI opens too."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app"))
    store = open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")
    db = store.database(db_worker)
    return store.history(db, store.cache(db_worker))


@pytest.fixture
def test_history_env(tmp_path, monkeypatch):
    """Fixture providing a temporary environment with HistoryManager and recorded steps."""
    db_worker = DBWorker()
    history_mgr = _store_history(tmp_path, monkeypatch, db_worker)
    db = history_mgr.db
    cache_mgr = history_mgr.cache_manager

    session_id = str(uuid.uuid4())
    base_dir = str(tmp_path / "target_dir")
    os.makedirs(base_dir, exist_ok=True)
    history_mgr.start_session(base_dir, session_id=session_id)

    # Record moves in the canonical move ledger
    src1 = os.path.join(base_dir, "doc1.txt")
    dst1 = os.path.join(base_dir, "Docs", "doc1.txt")
    hash1 = "a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890"

    src2 = os.path.join(base_dir, "image.png")
    dst2 = os.path.join(base_dir, "Images", "image.png")
    hash2 = "b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1"

    seq1 = history_mgr.moves.record_intent(session_id, src1, dst1, src_sha256=hash1)
    history_mgr.moves.mark_done(session_id, seq1, dst1)

    seq2 = history_mgr.moves.record_intent(session_id, src2, dst2, src_sha256=hash2)
    history_mgr.moves.mark_done(session_id, seq2, dst2)
    history_mgr.complete_session(session_id)

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

    with open(csv_path, encoding="utf-8") as f:
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

    with open(json_path, encoding="utf-8") as f:
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
        ledger_db=None,
        quiet=True,
        json=True,
    )

    settings = AppSettings()

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


def test_export_audit_log_all_sessions(tmp_path, monkeypatch):
    """Exporting without a session id covers every session in the store."""
    db_worker = DBWorker()
    hm = _store_history(tmp_path, monkeypatch, db_worker)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    s1_id = hm.start_session(str(src_dir))
    s2_id = hm.start_session(str(src_dir))
    seq_a = hm.moves.record_intent(
        s1_id, "/src/a.txt", "/dst/a.txt", src_sha256="hash_a"
    )
    hm.moves.mark_done(s1_id, seq_a, "/dst/a.txt")
    seq_b = hm.moves.record_intent(
        s1_id, "/src/b.txt", "/dst/b.txt", src_sha256="hash_b"
    )
    hm.moves.mark_done(s1_id, seq_b, "/dst/b.txt")
    seq_c = hm.moves.record_intent(
        s2_id, "/src/c.txt", "/dst/c.txt", src_sha256="hash_c"
    )
    hm.moves.mark_done(s2_id, seq_c, "/dst/c.txt")

    json_out = str(tmp_path / "aggregated.json")
    res = hm.export_audit_log(json_out, session_id=None, format="json")

    assert res["status"] == "success"
    assert res["count"] == 3

    with open(json_out, encoding="utf-8") as f:
        data = json.load(f)

    assert data["total_steps"] == 3
    sids = {step["session_id"] for step in data["steps"]}
    assert sids == {s1_id, s2_id}

    db_worker.stop()


def test_cli_ledger_export_without_session_id(tmp_path, monkeypatch):
    """Test CLI ledger export when session_id is omitted (aggregates all sessions)."""
    db_worker = DBWorker()
    hm = _store_history(tmp_path, monkeypatch, db_worker)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    s1_id = hm.start_session(str(src_dir))
    s2_id = hm.start_session(str(src_dir))
    seq1 = hm.moves.record_intent(s1_id, "/src/1.txt", "/dst/1.txt", src_sha256="h1")
    hm.moves.mark_done(s1_id, seq1, "/dst/1.txt")
    seq2 = hm.moves.record_intent(s2_id, "/src/2.txt", "/dst/2.txt", src_sha256="h2")
    hm.moves.mark_done(s2_id, seq2, "/dst/2.txt")

    csv_out = str(tmp_path / "cli_all.csv")

    import argparse

    args = argparse.Namespace(
        subcommand="ledger",
        ledger_command="export",
        session_id=None,
        output=csv_out,
        format="csv",
        csv=True,
        ledger_db=None,
        quiet=True,
        json=False,
    )

    settings = AppSettings()

    with pytest.raises(SystemExit) as exc_info:
        handle_ledger_command(args, settings)

    assert exc_info.value.code == 0
    assert os.path.exists(csv_out)

    with open(csv_out, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2

    db_worker.stop()


def test_export_audit_log_path_traversal(test_history_env):
    """Test that export_audit_log rejects path traversal attempts escaping base_dir."""
    from app.core.exceptions import AuditExportError

    env = test_history_env
    history_mgr = env["history_mgr"]
    session_id = env["session_id"]
    base_dir = env["base_dir"]

    # Traversal relative path escaping base_dir
    with pytest.raises(AuditExportError) as exc_info:
        history_mgr.export_audit_log(
            output_path="../escaped.csv",
            session_id=session_id,
            format="csv",
            base_dir=base_dir,
        )
    assert "escapes allowed base directory boundary" in str(
        exc_info.value
    ) or "contains invalid directory traversal" in str(exc_info.value)


def test_export_audit_log_0o600_permissions_and_sorted_folder_blocking(
    test_history_env,
):
    """Test that export_audit_log creates external output files with 0o600 permissions and blocks writes inside base_dir."""
    import stat

    from app.core.exceptions import AuditExportError

    env = test_history_env
    history_mgr = env["history_mgr"]
    session_id = env["session_id"]
    base_dir = env["base_dir"]
    out_file = str(env["tmp_path"] / "exports" / "secure_export.csv")

    with pytest.raises(AuditExportError) as exc_info:
        history_mgr.export_audit_log(
            output_path=os.path.join(base_dir, "inside.csv"),
            session_id=session_id,
            format="csv",
            base_dir=base_dir,
        )
    assert "sorted folder" in str(exc_info.value).lower()

    res = history_mgr.export_audit_log(
        output_path=out_file,
        session_id=session_id,
        format="csv",
        base_dir=base_dir,
    )

    assert os.path.exists(res["output_path"])
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(res["output_path"]).st_mode)
        assert mode == 0o600


def test_export_audit_log_excludes_undo_snapshot_rows(test_history_env):
    """Legacy undo_snapshot rows are excluded from all-session exports and rejected by explicit session_id."""
    from app.core.db_conn import get_db_connection

    env = test_history_env
    history_mgr = env["history_mgr"]
    snap_id = str(uuid.uuid4())
    conn = get_db_connection(history_mgr.db_path)
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            (snap_id, 1700000100.0, env["base_dir"], "undo_snapshot"),
        )
        conn.execute(
            "INSERT INTO moves (session_id, seq, src, dst, src_sha256, state, created_at, updated_at) "
            "VALUES (?, 1, ?, ?, 'snaphash', 'done', 1700000100.0, 1700000100.0)",
            (snap_id, "/snap/src.txt", "/snap/dst.txt"),
        )

    json_out = str(env["tmp_path"] / "exports" / "filtered_all.json")
    res = history_mgr.export_audit_log(json_out, session_id=None, format="json")
    assert res["count"] == 2
    with open(json_out, encoding="utf-8") as f:
        payload = json.load(f)
    assert {step["session_id"] for step in payload["steps"]} == {env["session_id"]}

    with pytest.raises(ValueError, match="not found"):
        history_mgr.export_audit_log(
            str(env["tmp_path"] / "exports" / "snap.json"),
            session_id=snap_id,
            format="json",
        )


def test_cli_ledger_export_and_undo_export_honor_custom_ledger_db(
    tmp_path, monkeypatch
):
    """Both `ledger export --ledger-db` and `undo --export --ledger-db` export from the custom DB path."""
    import argparse

    from app.cli.undo_cli import execute_undo_command
    from app.core.services import acquire_services

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "empty_default_app"))
    custom_db = tmp_path / "custom_store" / "custom.db"
    sorted_dir = tmp_path / "sorted_workspace"
    sorted_dir.mkdir()

    with acquire_services(db_path=custom_db) as lease:
        sid = lease.history.start_session(str(sorted_dir))
        seq = lease.history.moves.record_intent(
            sid,
            str(sorted_dir / "alpha.txt"),
            str(sorted_dir / "Docs" / "alpha.txt"),
            src_sha256="hash_alpha",
        )
        lease.history.moves.mark_done(sid, seq, str(sorted_dir / "Docs" / "alpha.txt"))
        lease.history.complete_session(sid)

    ledger_csv = tmp_path / "exports" / "custom_ledger.csv"
    ledger_args = argparse.Namespace(
        subcommand="ledger",
        ledger_command="export",
        session_id=None,
        output=str(ledger_csv),
        format="csv",
        csv=True,
        ledger_db=str(custom_db),
        quiet=True,
        json=True,
    )
    with pytest.raises(SystemExit) as ledger_exit:
        handle_ledger_command(ledger_args, AppSettings())
    assert ledger_exit.value.code == 0
    with open(ledger_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid

    undo_json = tmp_path / "exports" / "undo_export.json"
    undo_args = argparse.Namespace(
        subcommand="undo",
        list_sessions=False,
        export=str(undo_json),
        format="json",
        ledger_db=str(custom_db),
        session_id=None,
        latest=True,
        dry_run=False,
        force=False,
        step_ids=None,
        files=None,
        json=True,
        quiet=True,
    )
    with pytest.raises(SystemExit) as undo_exit:
        execute_undo_command(undo_args, AppSettings())
    assert undo_exit.value.code == 0
    with open(undo_json, encoding="utf-8") as f:
        undo_data = json.load(f)
    assert undo_data["session_id"] == sid
    assert undo_data["total_steps"] == 1
    assert list(sorted_dir.iterdir()) == []


def test_history_modal_defaults_to_external_exports_and_blocks_sorted_folder(
    test_history_env,
):
    """HistoryModal defaults to <app_dir>/exports/audit_log.csv and blocks exports inside base_dir."""
    import asyncio

    from textual.widgets import Button, Input, Select

    from app.config import get_app_dir
    from app.core.session_catalog import list_sessions
    from app.ui.tui import AutoSorterTUI

    env = test_history_env

    async def _test():
        settings = AppSettings(filepath=str(env["tmp_path"] / "settings.json"))
        settings.AI_CONSENT_GRANTED = False
        app = AutoSorterTUI(settings=settings, base_dir=env["base_dir"])
        async with app.run_test() as pilot:
            app.action_open_history()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)

            path_inp = modal.query_one("#input-history-path", Input)
            expected_default = os.path.join(get_app_dir(), "exports", "audit_log.csv")
            assert path_inp.value == expected_default

            # Changing format updates default filename suffix automatically
            fmt_select = modal.query_one("#select-history-format", Select)
            fmt_select.value = "json"
            await pilot.pause()
            expected_json = os.path.join(get_app_dir(), "exports", "audit_log.json")
            assert path_inp.value == expected_json

            modal.query_one("#btn-export", Button).press()
            await pilot.pause()

            assert os.path.exists(expected_json)
            assert os.listdir(env["base_dir"]) == []

            # Re-open HistoryModal and attempt to export inside the sorted folder -> blocked
            app.push_screen(
                HistoryModal(sessions=list_sessions(), base_dir=env["base_dir"]),
                callback=None,
            )
            await pilot.pause()
            app.pop_screen()
            await pilot.pause()

            app.action_open_history()
            await pilot.pause()
            modal2 = app.screen
            assert isinstance(modal2, HistoryModal)
            inside_target = os.path.join(env["base_dir"], "inside_sorted.csv")
            modal2.query_one("#input-history-path", Input).value = inside_target
            modal2.query_one("#btn-export", Button).press()
            await pilot.pause()
            assert not os.path.exists(inside_target)
            assert os.listdir(env["base_dir"]) == []

    asyncio.run(_test())
