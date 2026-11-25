"""Unit tests for transaction ledger CLI subcommand handlers."""

import argparse
import json
import sys
from unittest import mock

import pytest

from app.cli import handle_cli_command, register_cli_subcommands
from app.cli.ledger_cli import handle_ledger_command, register_subparser
from app.config import AppSettings
from app.core.exceptions import AuditExportError
from app.core.moves import STATUS_FAILED_CONFLICT, MoveLedger, format_move_failure


@pytest.fixture
def temp_ledger(tmp_path):
    """Create isolated SQLite move ledger for CLI testing."""
    db_path = str(tmp_path / "test_ledger.db")
    ledger = MoveLedger(db_path=db_path, auto_init=True)
    # Seed sample records
    ledger.record_intent(
        session_id="sess_alpha",
        src=str(tmp_path / "file1.txt"),
        dst=str(tmp_path / "dest1.txt"),
    )
    seq2 = ledger.record_intent(
        session_id="sess_beta",
        src=str(tmp_path / "file2.txt"),
        dst=str(tmp_path / "dest2.txt"),
    )
    # Mark second move as failed
    ledger.mark_failed("sess_beta", seq2, format_move_failure(STATUS_FAILED_CONFLICT))
    return db_path, ledger


def test_register_subparser():
    """Verify ledger subparser registration with all arguments."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")
    register_subparser(subparsers)

    # Test status parsing
    args = parser.parse_args(["ledger", "status", "--failed", "--json"])
    assert args.subcommand == "ledger"
    assert args.ledger_command == "status"
    assert args.failed is True
    assert args.json is True

    # Test reconcile parsing
    args_rec = parser.parse_args(["ledger", "reconcile", "--quiet"])
    assert args_rec.ledger_command == "reconcile"
    assert args_rec.quiet is True

    # Test purge parsing
    args_purge = parser.parse_args(["ledger", "purge", "--completed", "--force"])
    assert args_purge.ledger_command == "purge"
    assert args_purge.completed is True
    assert args_purge.force is True

    # Test export parsing
    args_exp = parser.parse_args(
        ["ledger", "export", "--format", "json", "-o", "out.json"]
    )
    assert args_exp.ledger_command == "export"
    assert args_exp.format == "json"
    assert args_exp.output == "out.json"


def test_handle_non_ledger_command():
    """Verify handler returns False for non-ledger commands."""
    args = argparse.Namespace(subcommand="scan")
    settings = AppSettings()
    assert handle_ledger_command(args, settings) is False


def test_missing_subcommand_exits(capsys):
    """Verify missing ledger subcommand triggers exit_code 2."""
    args = argparse.Namespace(subcommand="ledger", ledger_command=None, json=True)
    settings = AppSettings()

    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args, settings)
    assert exc.value.code == 2


def test_status_text_and_json(temp_ledger, capsys):
    """Test ledger status command output in both text and JSON formats."""
    db_path, _ = temp_ledger
    settings = AppSettings()

    # JSON output
    args_json = argparse.Namespace(
        subcommand="ledger",
        ledger_command="status",
        ledger_db=db_path,
        session_id=None,
        failed=False,
        aborted=False,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_json, settings)
    assert exc.value.code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "success"
    assert data["count"] == 2

    # Filtered by session_id in text mode
    args_text = argparse.Namespace(
        subcommand="ledger",
        ledger_command="status",
        ledger_db=db_path,
        session_id="sess_beta",
        failed=False,
        aborted=False,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_text, settings)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "sess_beta" in out or "dest2.txt" in out

    # Filtered for failed only
    args_failed = argparse.Namespace(
        subcommand="ledger",
        ledger_command="status",
        ledger_db=db_path,
        session_id=None,
        failed=True,
        aborted=False,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_failed, settings)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "failed or aborted" in out

    # Filtered for aborted only
    args_aborted = argparse.Namespace(
        subcommand="ledger",
        ledger_command="status",
        ledger_db=db_path,
        session_id=None,
        failed=False,
        aborted=True,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_aborted, settings)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "aborted" in out


def test_reconcile_text_and_json(temp_ledger, capsys):
    """Test ledger reconcile command in text and JSON modes."""
    db_path, _ = temp_ledger
    settings = AppSettings()

    # JSON mode
    args_json = argparse.Namespace(
        subcommand="ledger",
        ledger_command="reconcile",
        ledger_db=db_path,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_json, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"
    assert "reconciled_count" in data

    # Text mode
    args_text = argparse.Namespace(
        subcommand="ledger",
        ledger_command="reconcile",
        ledger_db=db_path,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_text, settings)
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "reconciliation completed" in captured.err.lower()


def test_purge_command(temp_ledger, capsys, monkeypatch):
    """Test ledger purge command with filters and confirmation."""
    db_path, ledger = temp_ledger
    settings = AppSettings()

    # Purge failed with force in JSON mode
    args_failed = argparse.Namespace(
        subcommand="ledger",
        ledger_command="purge",
        ledger_db=db_path,
        session_id=None,
        completed=False,
        failed=True,
        aborted=False,
        force=True,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_failed, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"
    assert data["failed_only"] is True

    # Purge aborted with force in text mode
    args_aborted = argparse.Namespace(
        subcommand="ledger",
        ledger_command="purge",
        ledger_db=db_path,
        session_id=None,
        completed=False,
        failed=False,
        aborted=True,
        force=True,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_aborted, settings)
    assert exc.value.code == 0
    assert "purged successfully" in capsys.readouterr().err.lower()

    # Purge completed
    args_completed = argparse.Namespace(
        subcommand="ledger",
        ledger_command="purge",
        ledger_db=db_path,
        session_id=None,
        completed=True,
        failed=False,
        aborted=False,
        force=True,
        json=False,
        quiet=True,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_completed, settings)
    assert exc.value.code == 0

    # Purge by session_id
    args_session = argparse.Namespace(
        subcommand="ledger",
        ledger_command="purge",
        ledger_db=db_path,
        session_id="sess_alpha",
        completed=False,
        failed=False,
        aborted=False,
        force=True,
        json=False,
        quiet=True,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_session, settings)
    assert exc.value.code == 0

    # Purge all without explicit filter, non-tty raises error
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args_prompt = argparse.Namespace(
        subcommand="ledger",
        ledger_command="purge",
        ledger_db=db_path,
        session_id=None,
        completed=False,
        failed=False,
        aborted=False,
        force=False,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_prompt, settings)
    assert exc.value.code == 1

    # Purge prompt cancelled
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_prompt, settings)
    assert exc.value.code == 1

    # Purge prompt confirmed
    monkeypatch.setattr("builtins.input", lambda _: "y")
    with pytest.raises(SystemExit) as exc:
        handle_ledger_command(args_prompt, settings)
    assert exc.value.code == 0


def test_export_command(temp_ledger, tmp_path, capsys):
    """Test ledger export command in CSV and JSON formats."""
    db_path, _ = temp_ledger
    settings = AppSettings()
    out_file = str(tmp_path / "audit.json")

    mock_history = mock.MagicMock()
    mock_history.export_audit_log.return_value = {
        "status": "success",
        "format": "json",
        "output_path": out_file,
        "count": 5,
    }

    mock_lease = mock.MagicMock()
    mock_lease.history = mock_history

    with mock.patch("app.core.services.acquire_services", return_value=mock_lease):
        args_export = argparse.Namespace(
            subcommand="ledger",
            ledger_command="export",
            ledger_db=db_path,
            session_id="sess_test",
            output=out_file,
            format="json",
            csv=False,
            json=True,
            quiet=False,
        )
        with pytest.raises(SystemExit) as exc:
            handle_ledger_command(args_export, settings)
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["format"] == "json"
        assert data["count"] == 5

        # CSV text mode
        args_csv = argparse.Namespace(
            subcommand="ledger",
            ledger_command="export",
            ledger_db=db_path,
            session_id=None,
            output=None,
            format=None,
            csv=True,
            json=False,
            quiet=False,
        )
        mock_history.export_audit_log.return_value = {
            "status": "success",
            "format": "csv",
            "output_path": "audit_log_all.csv",
            "count": 3,
        }
        with pytest.raises(SystemExit) as exc:
            handle_ledger_command(args_csv, settings)
        assert exc.value.code == 0
        err = capsys.readouterr().err
        assert "audit log exported" in err.lower()

        # Export error handling
        mock_history.export_audit_log.side_effect = AuditExportError(
            "disk write failure"
        )
        with pytest.raises(SystemExit) as exc:
            handle_ledger_command(args_csv, settings)
        assert exc.value.code == 1


def test_cli_subcommands_dispatch(temp_ledger, capsys):
    """Test dispatch through register_cli_subcommands and handle_cli_command."""
    db_path, _ = temp_ledger
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")
    register_cli_subcommands(subparsers)

    args = parser.parse_args(["ledger", "status", "--ledger-db", db_path, "--json"])
    settings = AppSettings()
    with pytest.raises(SystemExit) as exc:
        handle_cli_command(args, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"


def test_reconcile_warns_when_acquire_services_fails_in_text_and_json(
    tmp_path, capsys, caplog
):
    """Verify standalone ledger reconcile logs acquire_services errors, warns about skipped path sync, and keeps stdout valid JSON."""
    import logging

    db_path = str(tmp_path / "standalone_ledger.db")
    ledger = MoveLedger(db_path=db_path, auto_init=True)
    src = tmp_path / "src_doc.txt"
    dst = tmp_path / "dst_doc.txt"
    dst.write_text("reconciled content", encoding="utf-8")
    ledger.record_intent(
        session_id="sess_standalone",
        src=str(src),
        dst=str(dst),
    )

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    caplog.set_level(logging.WARNING, logger="app.cli.ledger_cli")

    with mock.patch(
        "app.core.services.acquire_services",
        side_effect=RuntimeError("store locked"),
    ):
        args_json = argparse.Namespace(
            subcommand="ledger",
            ledger_command="reconcile",
            ledger_db=db_path,
            json=True,
            quiet=False,
        )
        with pytest.raises(SystemExit) as exc_json:
            handle_ledger_command(args_json, settings)
        assert exc_json.value.code == 0

        captured_json = capsys.readouterr()
        payload = json.loads(captured_json.out)
        assert payload["status"] == "success"
        assert payload["reconciled_count"] == 1
        assert any(
            "skipping document path sync" in w for w in payload.get("warnings", [])
        )
        assert "skipping document path sync" in captured_json.err
        assert any(
            "skipping document path sync" in r.getMessage() and r.exc_info is not None
            for r in caplog.records
        )

        # Re-seed another incomplete move to verify text mode warning as well
        dst2 = tmp_path / "dst_doc2.txt"
        dst2.write_text("reconciled text mode", encoding="utf-8")
        ledger.record_intent(
            session_id="sess_standalone_2",
            src=str(tmp_path / "src_doc2.txt"),
            dst=str(dst2),
        )
        args_text = argparse.Namespace(
            subcommand="ledger",
            ledger_command="reconcile",
            ledger_db=db_path,
            json=False,
            quiet=False,
        )
        with pytest.raises(SystemExit) as exc_text:
            handle_ledger_command(args_text, settings)
        assert exc_text.value.code == 0

        captured_text = capsys.readouterr()
        assert "skipping document path sync" in captured_text.err
        assert "reconciled 1 entry" in captured_text.err.lower()
