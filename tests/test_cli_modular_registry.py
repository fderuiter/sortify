"""Tests for modular CLI subcommand registry and domain handlers."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def run_cli(args, env=None):
    """Run app/main.py in-process and return (returncode, stdout, stderr)."""
    old_env = os.environ.copy()
    repo_root = str(Path(__file__).parent.parent.resolve())
    os.environ["PYTHONPATH"] = repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")

    if env:
        os.environ.update(env)

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    test_args = ["main.py"] + args

    code = 0
    with (
        patch("sys.argv", test_args),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
    ):
        try:
            from app.main import main

            main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except Exception as e:
            stderr_cap.write(str(e))
            code = 1
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    return code, stdout_cap.getvalue(), stderr_cap.getvalue()


@pytest.mark.xdist_group(name="cli_registry")
def test_ledger_status_and_reconcile():
    """Test 'sortify ledger status' and 'sortify ledger reconcile' commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        ledger_db = str(tmp_path / "ledger.db")

        try:
            # 1. Status empty
            code, stdout, stderr = run_cli(
                ["ledger", "status", "--ledger-db", ledger_db, "--json"]
            )
            assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
            data = json.loads(stdout)
            assert data["status"] == "success"
            assert data["count"] == 0

            # Create an entry directly in transaction ledger
            from app.core.ledger import TransactionLedger

            ledger_inst = TransactionLedger(db_path=ledger_db)
            entry_id = ledger_inst.log_intent(
                session_id="sess_101",
                base_dir=str(tmp_path),
                source_path=str(tmp_path / "src.txt"),
                dest_path=str(tmp_path / "dest.txt"),
                source_rel_path="src.txt",
                dest_rel_path="dest.txt",
            )

            # 2. Status with pending entry
            code_pend, stdout_pend, stderr_pend = run_cli(
                ["ledger", "status", "--ledger-db", ledger_db, "--json"]
            )
            assert code_pend == 0, (
                f"Expected 0 exit code, got {code_pend}. Stderr: {stderr_pend}"
            )
            data_pend = json.loads(stdout_pend)
            assert data_pend["count"] == 1
            assert data_pend["pending_entries"][0]["entry_id"] == entry_id

            # 3. Reconcile
            code_rec, stdout_rec, stderr_rec = run_cli(
                ["ledger", "reconcile", "--ledger-db", ledger_db, "--json"]
            )
            assert code_rec == 0, (
                f"Expected 0 exit code, got {code_rec}. Stderr: {stderr_rec}"
            )
            data_rec = json.loads(stdout_rec)
            assert data_rec["status"] == "success"
            assert data_rec["reconciled_count"] == 1

            # 4. Status after reconciliation
            code_post, stdout_post, stderr_post = run_cli(
                ["ledger", "status", "--ledger-db", ledger_db, "--json"]
            )
            assert code_post == 0, (
                f"Expected 0 exit code, got {code_post}. Stderr: {stderr_post}"
            )
            data_post = json.loads(stdout_post)
            assert data_post["count"] == 0
        finally:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.xdist_group(name="cli_registry")
def test_ledger_purge():
    """Test 'sortify ledger purge' command."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        ledger_db = str(tmp_path / "ledger.db")

        try:
            # Purge with --completed
            code, stdout, stderr = run_cli(
                ["ledger", "purge", "--ledger-db", ledger_db, "--completed", "--json"]
            )
            assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
            data = json.loads(stdout)
            assert data["status"] == "success"
        finally:
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.xdist_group(name="cli_registry")
def test_quarantine_lifecycle():
    """Test 'sortify quarantine list', 'inspect', 'process', and 'release' commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir).resolve()
        db_path = str(tmp_path / "quarantine.db")
        from app.core.db import Database
        from app.core.db_worker import DBWorker
        from app.core.quarantine_interceptor import QuarantineInterceptorService

        db = Database(Path(db_path), DBWorker())

        try:
            # Create source test file
            src_file = tmp_path / "sample_quarantine_doc.txt"
            src_file.write_text("Confidential report with SSN 123-45-6789.")

            # Stage file
            service = QuarantineInterceptorService(db=db)
            staged_info = service.stage_incoming_file(
                str(src_file), base_dir=str(tmp_path)
            )
            job_id = staged_info["job_id"]

            # 1. List
            code_list, stdout_list, stderr_list = run_cli(
                [
                    "quarantine",
                    "list",
                    "--db-path",
                    db_path,
                    "--status",
                    "STAGED",
                    "--json",
                ]
            )
            assert code_list == 0, (
                f"Expected 0 exit code, got {code_list}. Stderr: {stderr_list}"
            )
            data_list = json.loads(stdout_list)
            assert data_list["count"] == 1
            assert data_list["items"][0]["job_id"] == job_id

            # 2. Inspect
            code_insp, stdout_insp, stderr_insp = run_cli(
                ["quarantine", "inspect", job_id, "--db-path", db_path, "--json"]
            )
            assert code_insp == 0, (
                f"Expected 0 exit code, got {code_insp}. Stderr: {stderr_insp}"
            )
            data_insp = json.loads(stdout_insp)
            assert data_insp["record"]["job_id"] == job_id

            # 3. Process
            code_proc, stdout_proc, stderr_proc = run_cli(
                [
                    "quarantine",
                    "process",
                    "--job-id",
                    job_id,
                    "--db-path",
                    db_path,
                    "--json",
                ]
            )
            assert code_proc == 0, (
                f"Expected 0 exit code, got {code_proc}. Stderr: {stderr_proc}"
            )
            data_proc = json.loads(stdout_proc)
            assert data_proc["status"] == "success"

            # 4. Release
            code_rel, stdout_rel, stderr_rel = run_cli(
                [
                    "quarantine",
                    "release",
                    job_id,
                    "--db-path",
                    db_path,
                    "--dest-dir",
                    str(tmp_path),
                    "--json",
                ]
            )
            assert code_rel == 0, (
                f"Expected 0 exit code, got {code_rel}. Stderr: {stderr_rel}"
            )
            data_rel = json.loads(stdout_rel)
            assert data_rel["status"] == "success"
            assert data_rel["status_code"] == "RELEASED"
        finally:
            if db and hasattr(db, "worker") and db.worker:
                db.worker.stop()
            from app.core.db_conn import clear_connection_cache

            clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.xdist_group(name="cli_registry")
def test_cli_usage_error_code():
    """Test missing or invalid subcommand arguments return exit code 2."""
    code, stdout, stderr = run_cli(["ledger"])
    assert code == 2
    assert "Missing ledger subcommand" in stderr
