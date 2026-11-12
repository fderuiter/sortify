"""Tests for centralized CLI error emitter, argument parser JSON errors, and exception boundary."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.cli.error_emitter import emit_cli_error


def run_cli(args, env=None, prog="main.py"):
    """Run app/main.py in-process and return (returncode, stdout, stderr)."""
    old_env = os.environ.copy()
    repo_root = str(Path(__file__).parent.parent.resolve())
    os.environ["PYTHONPATH"] = repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")

    if env:
        os.environ.update(env)

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    test_args = [prog] + args

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


class TestCliJsonErrors:
    """Test suite covering CLI JSON error formatting across all commands and boundaries."""

    def test_sort_invalid_path_json(self):
        """Verify 'sort /nonexistent/path --json' outputs structured JSON and exits with code 1."""
        code, stdout, stderr = run_cli(
            ["sort", "/nonexistent/invalid_sort_path_xyz", "--json"]
        )
        assert code == 1
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "does not exist or is not a directory" in data["message"]

    def test_sort_invalid_path_plain_text(self):
        """Verify non-JSON sort error outputs human-readable text to stderr and preserves exit code 1."""
        code, stdout, stderr = run_cli(["sort", "/nonexistent/invalid_sort_path_xyz"])
        assert code == 1
        assert stdout.strip() == ""
        assert "Error: Target directory" in stderr
        assert "does not exist or is not a directory" in stderr

    def test_scan_invalid_path_json(self):
        """Verify 'scan /nonexistent/path --json' outputs structured JSON and exits with code 1."""
        code, stdout, stderr = run_cli(
            ["scan", "/nonexistent/invalid_scan_path_xyz", "--json"]
        )
        assert code == 1
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "does not exist or is not a directory" in data["message"]

    def test_quarantine_inspect_missing_job_json(self):
        """Verify 'quarantine inspect invalid_job --json' outputs structured JSON with job_id."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            code, stdout, stderr = run_cli(
                [
                    "quarantine",
                    "inspect",
                    "invalid_job_999",
                    "--db-path",
                    db_path,
                    "--json",
                ]
            )
            assert code == 1
            data = json.loads(stdout)
            assert data["status"] == "error"
            assert (
                "Quarantine record not found for job ID 'invalid_job_999'"
                in data["message"]
            )
            assert data.get("job_id") == "invalid_job_999"

    def test_quarantine_inspect_missing_job_plain_text(self):
        """Verify non-JSON quarantine inspect error outputs plain text to stderr."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test.db")
            code, stdout, stderr = run_cli(
                ["quarantine", "inspect", "invalid_job_999", "--db-path", db_path]
            )
            assert code == 1
            assert stdout.strip() == ""
            assert (
                "Error: Quarantine record not found for job ID 'invalid_job_999'."
                in stderr
            )

    def test_quarantine_missing_subcommand_json(self):
        """Verify 'quarantine --json' outputs structured JSON error with exit code 2."""
        code, stdout, stderr = run_cli(["quarantine", "--json"])
        assert code == 2
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "Missing quarantine subcommand" in data["message"]

    def test_quarantine_inspect_missing_job_id_argument_json(self):
        """Verify 'quarantine inspect --json' without job ID outputs structured JSON with exit code 2."""
        code, stdout, stderr = run_cli(["quarantine", "inspect", "--json"])
        assert code == 2
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "Job ID is required" in data["message"]

    def test_ledger_missing_subcommand_json(self):
        """Verify 'ledger --json' outputs structured JSON error with exit code 2."""
        code, stdout, stderr = run_cli(["ledger", "--json"])
        assert code == 2
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "Missing ledger subcommand" in data["message"]

    def test_ledger_missing_subcommand_plain_text(self):
        """Verify non-JSON ledger without subcommand outputs plain text to stderr with exit code 2."""
        code, stdout, stderr = run_cli(["ledger"])
        assert code == 2
        assert stdout.strip() == ""
        assert "Error: Missing ledger subcommand." in stderr

    def test_argparse_unknown_argument_json(self):
        """Verify unknown CLI option in --json mode returns structured JSON with exit code 2."""
        code, stdout, stderr = run_cli(["--unrecognized-option-xyz", "--json"])
        assert code == 2
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "unrecognized arguments" in data["message"]

    def test_argparse_unknown_argument_plain_text(self):
        """Verify unknown CLI option in standard mode outputs standard usage error to stderr."""
        code, stdout, stderr = run_cli(["--unrecognized-option-xyz"])
        assert code == 2
        assert stdout.strip() == ""
        assert "unrecognized arguments" in stderr or "usage:" in stderr

    def test_argparse_subcommand_unknown_argument_json(self):
        """Verify subparser argument error in --json mode outputs structured JSON with exit code 2."""
        code, stdout, stderr = run_cli(["sort", "--bad-sub-option", "--json"])
        assert code == 2
        data = json.loads(stdout)
        assert data["status"] == "error"
        assert "unrecognized arguments" in data["message"]

    def test_headless_no_directory_json(self):
        """Verify headless invocation without directory in --json mode returns structured JSON error."""
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = False
        with patch("sys.stdin", mock_stdin):
            code, stdout, stderr = run_cli(["--json"])
            assert code == 2
            data = json.loads(stdout)
            assert data["status"] == "error"
            assert "No target directory or subcommand specified" in data["message"]

    def test_exception_boundary_json_interception(self):
        """Verify uncaught exceptions during command execution are intercepted in --json mode."""
        with patch(
            "app.main.handle_sort_command",
            side_effect=RuntimeError("Simulated unexpected crash"),
        ):
            code, stdout, stderr = run_cli(["sort", "/dummy/path", "--json"])
            assert code == 1
            data = json.loads(stdout)
            assert data["status"] == "error"
            assert "Simulated unexpected crash" in data["message"]

    def test_exception_boundary_non_json_propagates(self):
        """Verify uncaught exceptions in non-JSON mode are not formatted as JSON."""
        with patch(
            "app.main.handle_sort_command",
            side_effect=RuntimeError("Simulated unexpected crash"),
        ):
            code, stdout, stderr = run_cli(["sort", "/dummy/path"])
            assert code == 1
            assert "Simulated unexpected crash" in stderr
            assert stdout.strip() == ""

    def test_emit_cli_error_custom_status_and_extra(self):
        """Verify emit_cli_error helper behavior with custom status, extra dictionary, and stream."""
        stream = io.StringIO()
        with pytest.raises(SystemExit) as exc_info:
            emit_cli_error(
                "Custom operational failure",
                status="failed",
                exit_code=42,
                json_mode=True,
                extra={"retryable": False, "code": "ERR_CUSTOM"},
                stream=stream,
            )
        assert exc_info.value.code == 42
        payload = json.loads(stream.getvalue())
        assert payload["status"] == "failed"
        assert payload["message"] == "Custom operational failure"
        assert payload["retryable"] is False
        assert payload["code"] == "ERR_CUSTOM"
