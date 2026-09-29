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
def test_crypto_info():
    """Test 'sortify crypto info' command."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_crypto.db"

        # Standard text output
        code, stdout, stderr = run_cli(["crypto", "info", "--db", str(db_path)])
        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        assert "Encryption Key Path:" in stdout
        assert "Storage Backend:" in stdout

        # JSON output
        code_json, stdout_json, stderr_json = run_cli(
            ["crypto", "info", "--db", str(db_path), "--json"]
        )
        assert code_json == 0, (
            f"Expected 0 exit code, got {code_json}. Stderr: {stderr_json}"
        )
        data = json.loads(stdout_json)
        assert data["status"] == "success"
        assert "key_path" in data
        assert "storage_backend" in data
        assert data["db_path"] == str(db_path)


@pytest.mark.xdist_group(name="cli_registry")
def test_crypto_rotate_key():
    """Test 'sortify crypto rotate-key' command."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_crypto.db"

        # Without --force in non-interactive environment should fail
        code_fail, stdout_fail, stderr_fail = run_cli(
            ["crypto", "rotate-key", "--db", str(db_path)]
        )
        assert code_fail == 1
        assert "requires explicit confirmation" in stderr_fail

        # With --force and --json
        code, stdout, stderr = run_cli(
            ["crypto", "rotate-key", "--db", str(db_path), "--force", "--json"]
        )
        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert "rotated successfully" in data["message"]


@pytest.mark.xdist_group(name="cli_registry")
def test_crypto_export_key():
    """Test 'sortify crypto export-key' command."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_crypto.db"
        out_file = Path(tmpdir) / "exported_secret.key"

        # Export to file
        code, stdout, stderr = run_cli(
            [
                "crypto",
                "export-key",
                "--db",
                str(db_path),
                "--output",
                str(out_file),
                "--json",
            ]
        )
        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert data["exported_to"] == str(out_file)
        assert out_file.exists()
        assert len(out_file.read_text()) > 0

        # Export to stdout JSON
        code_out, stdout_out, stderr_out = run_cli(
            ["crypto", "export-key", "--db", str(db_path), "--json"]
        )
        assert code_out == 0
        data_out = json.loads(stdout_out)
        assert "key" in data_out


@pytest.mark.xdist_group(name="cli_registry")
def test_ledger_status_and_reconcile():
    """Test 'sortify ledger status' and 'sortify ledger reconcile' commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_db = str(Path(tmpdir) / "ledger.db")

        # 1. Status empty
        code, stdout, stderr = run_cli(
            ["ledger", "status", "--ledger-db", ledger_db, "--json"]
        )
        assert code == 0
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert data["count"] == 0

        # Create an entry directly in transaction ledger
        from app.core.ledger import TransactionLedger

        ledger_inst = TransactionLedger(db_path=ledger_db)
        entry_id = ledger_inst.log_intent(
            session_id="sess_101",
            base_dir=tmpdir,
            source_path=str(Path(tmpdir) / "src.txt"),
            dest_path=str(Path(tmpdir) / "dest.txt"),
            source_rel_path="src.txt",
            dest_rel_path="dest.txt",
        )

        # 2. Status with pending entry
        code_pend, stdout_pend, stderr_pend = run_cli(
            ["ledger", "status", "--ledger-db", ledger_db, "--json"]
        )
        assert code_pend == 0
        data_pend = json.loads(stdout_pend)
        assert data_pend["count"] == 1
        assert data_pend["pending_entries"][0]["entry_id"] == entry_id

        # 3. Reconcile
        code_rec, stdout_rec, stderr_rec = run_cli(
            ["ledger", "reconcile", "--ledger-db", ledger_db, "--json"]
        )
        assert code_rec == 0
        data_rec = json.loads(stdout_rec)
        assert data_rec["status"] == "success"
        assert data_rec["reconciled_count"] == 1

        # 4. Status after reconciliation
        code_post, stdout_post, stderr_post = run_cli(
            ["ledger", "status", "--ledger-db", ledger_db, "--json"]
        )
        assert code_post == 0
        data_post = json.loads(stdout_post)
        assert data_post["count"] == 0


@pytest.mark.xdist_group(name="cli_registry")
def test_ledger_purge():
    """Test 'sortify ledger purge' command."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_db = str(Path(tmpdir) / "ledger.db")

        # Purge with --completed
        code, stdout, stderr = run_cli(
            ["ledger", "purge", "--ledger-db", ledger_db, "--completed", "--json"]
        )
        assert code == 0
        data = json.loads(stdout)
        assert data["status"] == "success"


@pytest.mark.xdist_group(name="cli_registry")
def test_quarantine_lifecycle():
    """Test 'sortify quarantine list', 'inspect', 'process', and 'release' commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "quarantine.db")
        from app.core.db import Database
        from app.core.db_worker import DBWorker
        from app.core.quarantine_interceptor import QuarantineInterceptorService

        db = Database(Path(db_path), DBWorker())

        # Create source test file
        src_file = Path(tmpdir) / "sample_quarantine_doc.txt"
        src_file.write_text("Confidential report with SSN 123-45-6789.")

        # Stage file
        service = QuarantineInterceptorService(db=db)
        staged_info = service.stage_incoming_file(str(src_file), base_dir=tmpdir)
        job_id = staged_info["job_id"]

        # 1. List
        code_list, stdout_list, stderr_list = run_cli(
            ["quarantine", "list", "--db-path", db_path, "--status", "STAGED", "--json"]
        )
        assert code_list == 0
        data_list = json.loads(stdout_list)
        assert data_list["count"] == 1
        assert data_list["items"][0]["job_id"] == job_id

        # 2. Inspect
        code_insp, stdout_insp, stderr_insp = run_cli(
            ["quarantine", "inspect", job_id, "--db-path", db_path, "--json"]
        )
        assert code_insp == 0
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
        assert code_proc == 0
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
                tmpdir,
                "--json",
            ]
        )
        assert code_rel == 0
        data_rel = json.loads(stdout_rel)
        assert data_rel["status"] == "success"
        assert data_rel["status_code"] == "RELEASED"


@pytest.mark.xdist_group(name="cli_registry")
def test_cro_ingest_and_manifest():
    """Test 'sortify cro ingest' and 'sortify cro manifest' commands."""
    with (
        tempfile.TemporaryDirectory() as src_dir,
        tempfile.TemporaryDirectory() as target_dir,
    ):
        # Create sample study documents in source
        src_path = Path(src_dir)
        (src_path / "Protocol_Study123.txt").write_text(
            "Clinical trial protocol for Study Protocol-123."
        )
        (src_path / "Informed_Consent_01.txt").write_text(
            "Subject consent form for Study Protocol-123."
        )

        # 1. CRO Ingest
        code_ingest, stdout_ingest, stderr_ingest = run_cli(
            [
                "cro",
                "ingest",
                "--source",
                src_dir,
                "--target",
                target_dir,
                "--mode",
                "tmf",
                "--json",
            ]
        )
        assert code_ingest == 0, (
            f"Expected 0 exit code, got {code_ingest}. Stderr: {stderr_ingest}"
        )
        data_ingest = json.loads(stdout_ingest)
        assert data_ingest["status"] == "success"
        pipeline_res = data_ingest["pipeline_result"]
        assert pipeline_res["total_scanned_files"] >= 2
        assert Path(pipeline_res["chain_of_custody_manifest_path"]).exists()

        # 2. CRO Manifest
        code_man, stdout_man, stderr_man = run_cli(
            ["cro", "manifest", target_dir, "--json"]
        )
        assert code_man == 0, (
            f"Expected 0 exit code, got {code_man}. Stderr: {stderr_man}"
        )
        data_man = json.loads(stdout_man)
        assert data_man["status"] == "success"
        assert "manifest" in data_man


@pytest.mark.xdist_group(name="cli_registry")
def test_cli_usage_error_code():
    """Test missing or invalid subcommand arguments return exit code 2."""
    code, stdout, stderr = run_cli(["crypto"])
    assert code == 2
    assert "Missing crypto subcommand" in stderr
