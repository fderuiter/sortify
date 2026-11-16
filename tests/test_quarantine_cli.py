"""Unit tests for compliance quarantine CLI subcommand handlers."""

import argparse
import json
from unittest import mock

import pytest

from app.cli import handle_cli_command, register_cli_subcommands
from app.cli.quarantine_cli import handle_quarantine_command, register_subparser
from app.config import AppSettings


@pytest.fixture
def mock_quarantine_environment():
    """Mock DatabaseManager and QuarantineInterceptorService for isolated CLI tests."""
    mock_db = mock.MagicMock()
    sample_record = {
        "job_id": "job_001",
        "base_dir": "/workspace",
        "original_filepath": "/workspace/doc1.pdf",
        "staged_filepath": "/vault/job_001/doc1.pdf",
        "status": "QUARANTINED",
        "policy_action": "quarantine",
        "error_message": None,
        "audit_log": [
            {
                "status": "STAGED",
                "details": "Item moved to quarantine staging",
            }
        ],
    }

    mock_db.get_all_quarantine_records.return_value = [sample_record]
    mock_db.get_quarantine_records_by_base_dir.return_value = [sample_record]
    mock_db.get_quarantine_record.side_effect = lambda jid: (
        sample_record if jid == "job_001" else None
    )

    mock_lease = mock.MagicMock()
    mock_lease.db = mock_db

    with (
        mock.patch("app.cli.quarantine_cli.acquire_services", return_value=mock_lease),
        mock.patch(
            "app.cli.quarantine_cli.QuarantineInterceptorService"
        ) as MockServiceCls,
    ):
        mock_svc = MockServiceCls.return_value
        mock_svc.process_quarantine_batch.return_value = [
            {"job_id": "job_001", "status": "RELEASED", "success": True}
        ]
        mock_svc.release_quarantine_batch.return_value = [
            {"job_id": "job_001", "status": "RELEASED", "destination": "/dest/doc1.pdf"}
        ]
        mock_svc.dismiss_quarantine_batch.return_value = [
            {"job_id": "job_001", "status": "DISMISSED"}
        ]
        yield mock_db, mock_svc


def test_register_subparser():
    """Verify quarantine subparser registration and argument parsing."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")
    register_subparser(subparsers)

    args = parser.parse_args(
        ["quarantine", "list", "--status", "QUARANTINED", "--json"]
    )
    assert args.subcommand == "quarantine"
    assert args.quarantine_command == "list"
    assert args.status == "QUARANTINED"
    assert args.json is True

    args_inspect = parser.parse_args(["quarantine", "inspect", "job_123"])
    assert args_inspect.job_id_pos == "job_123"

    args_release = parser.parse_args(
        [
            "quarantine",
            "release",
            "job_1",
            "job_2",
            "--dest-dir",
            "/out",
            "--conflict-policy",
            "overwrite",
            "--allow-external-dest",
        ]
    )
    assert args_release.job_ids_pos == ["job_1", "job_2"]
    assert args_release.dest_dir == "/out"
    assert args_release.conflict_policy == "overwrite"
    assert args_release.allow_external_dest is True


def test_handle_non_quarantine_command():
    """Verify handler returns False when subcommand is not quarantine."""
    args = argparse.Namespace(subcommand="scan")
    settings = AppSettings()
    assert handle_quarantine_command(args, settings) is False


def test_missing_subcommand_exits(capsys):
    """Verify missing quarantine subcommand exits with code 2."""
    args = argparse.Namespace(
        subcommand="quarantine", quarantine_command=None, json=True
    )
    settings = AppSettings()

    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args, settings)
    assert exc.value.code == 2


def test_quarantine_list(mock_quarantine_environment, capsys):
    """Test quarantine list command in text and JSON modes."""
    mock_db, _ = mock_quarantine_environment
    settings = AppSettings()

    # JSON mode
    args_json = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="list",
        status="QUARANTINED",
        base_dir=None,
        db_path=None,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_json, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"
    assert data["count"] == 1
    assert data["items"][0]["job_id"] == "job_001"

    # Text mode with base_dir
    args_text = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="list",
        status=None,
        base_dir="/workspace",
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "job_001" in out
    assert "QUARANTINED" in out

    # Empty list
    mock_db.get_quarantine_records_by_base_dir.return_value = []
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "0 record(s) found" in out


def test_quarantine_inspect(mock_quarantine_environment, capsys):
    """Test quarantine inspect command with existing and missing jobs."""
    mock_db, _ = mock_quarantine_environment
    settings = AppSettings()

    # Missing job ID error
    args_no_id = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="inspect",
        job_id_pos=None,
        job_ids_pos=[],
        job_id=None,
        job_ids=None,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_no_id, settings)
    assert exc.value.code == 2

    # Success in JSON mode
    args_json = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="inspect",
        job_id_pos="job_001",
        job_ids_pos=[],
        job_id=None,
        job_ids=None,
        db_path=None,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_json, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"
    assert data["record"]["job_id"] == "job_001"

    # Success in text mode
    args_text = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="inspect",
        job_id_pos="job_001",
        job_ids_pos=[],
        job_id=None,
        job_ids=None,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Quarantine Job Record: job_001" in out
    assert "Audit Log:" in out

    # Job not found
    args_not_found = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="inspect",
        job_id_pos="missing_job",
        job_ids_pos=[],
        job_id=None,
        job_ids=None,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_not_found, settings)
    assert exc.value.code == 1


def test_quarantine_process(mock_quarantine_environment, capsys):
    """Test quarantine process command."""
    _, mock_svc = mock_quarantine_environment
    settings = AppSettings()

    # Missing job IDs
    args_no_id = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="process",
        job_ids_pos=[],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        timeout=300.0,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_no_id, settings)
    assert exc.value.code == 2

    # Success in JSON mode
    args_json = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="process",
        job_ids_pos=["job_001"],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        timeout=300.0,
        db_path=None,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_json, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert data["status"] == "success"

    # Success in text mode
    args_text = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="process",
        job_ids_pos=[],
        job_id_pos=None,
        job_id=None,
        job_ids="job_001,job_002",
        timeout=300.0,
        db_path=None,
        json=False,
        quiet=False,
    )
    mock_svc.process_quarantine_batch.return_value = [
        {"job_id": "job_001", "status": "RELEASED"},
        {"job_id": "job_002", "status": "FAILED", "error": "Signature mismatch"},
    ]
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 0
    err = capsys.readouterr().err
    assert "Quarantine batch process complete" in err

    # Process failure exception
    mock_svc.process_quarantine_batch.side_effect = RuntimeError("Worker timeout")
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 1


def test_quarantine_release(mock_quarantine_environment, capsys):
    """Test quarantine release command."""
    _, mock_svc = mock_quarantine_environment
    settings = AppSettings()

    # Missing job IDs
    args_no_id = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="release",
        job_ids_pos=[],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        dest_dir=None,
        conflict_policy="rename",
        allow_external_dest=False,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_no_id, settings)
    assert exc.value.code == 2

    # Success in JSON mode
    args_json = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="release",
        job_ids_pos=["job_001"],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        dest_dir="/dest",
        conflict_policy="rename",
        allow_external_dest=False,
        db_path=None,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_json, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"
    assert data["results"][0]["status"] == "RELEASED"

    # Success in text mode
    args_text = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="release",
        job_ids_pos=[],
        job_id_pos=None,
        job_id="job_001",
        job_ids=None,
        dest_dir=None,
        conflict_policy="rename",
        allow_external_dest=False,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 0
    err = capsys.readouterr().err
    assert "Quarantine batch release complete" in err

    # Release failure exception
    mock_svc.release_quarantine_batch.side_effect = PermissionError(
        "Cannot write to dest"
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 1


def test_quarantine_dismiss(mock_quarantine_environment, capsys):
    """Test quarantine dismiss command."""
    _, mock_svc = mock_quarantine_environment
    settings = AppSettings()

    # Missing job IDs
    args_no_id = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="dismiss",
        job_ids_pos=[],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_no_id, settings)
    assert exc.value.code == 2

    # Success in JSON mode
    args_json = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="dismiss",
        job_ids_pos=["job_001"],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        db_path=None,
        json=True,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_json, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"

    # Success in text mode
    args_text = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="dismiss",
        job_ids_pos=["job_001"],
        job_id_pos=None,
        job_id=None,
        job_ids=None,
        db_path=None,
        json=False,
        quiet=False,
    )
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 0
    err = capsys.readouterr().err
    assert "Quarantine batch dismiss complete" in err

    # Dismiss failure exception
    mock_svc.dismiss_quarantine_batch.side_effect = OSError("DB lock")
    with pytest.raises(SystemExit) as exc:
        handle_quarantine_command(args_text, settings)
    assert exc.value.code == 1


def test_quarantine_cli_dispatch(mock_quarantine_environment, capsys):
    """Test dispatch through register_cli_subcommands and handle_cli_command."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="subcommand")
    register_cli_subcommands(subparsers)

    args = parser.parse_args(["quarantine", "list", "--json"])
    settings = AppSettings()
    with pytest.raises(SystemExit) as exc:
        handle_cli_command(args, settings)
    assert exc.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "success"
