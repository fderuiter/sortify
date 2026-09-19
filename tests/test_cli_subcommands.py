"""Tests for unified CLI subcommands and structured JSON output."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch


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
    with patch("sys.argv", test_args), patch("sys.stdout", stdout_cap), patch("sys.stderr", stderr_cap):
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


def create_sample_corpus(base_dir):
    """Create a sample corpus directory with documents."""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "finance_doc.txt").write_text("Finance, investment, and banking report.")
    (base / "tech_doc.txt").write_text("Software engineering, Python, algorithms, and computers.")
    (base / "health_doc.txt").write_text("Medical science, clinical trial, doctor, and patient data.")
    (base / "empty.txt").write_text("")


def test_sort_subcommand_dry_run_json():
    """Test sort subcommand with --dry-run and --json flags."""
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dest_dir:
        create_sample_corpus(src_dir)
        code, stdout, stderr = run_cli([
            "sort",
            src_dir,
            "--json",
            "--dest-dir",
            dest_dir,
            "--dry-run",
            "--max-folders",
            "5",
        ])

        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert data["dry_run"] is True
        assert "plan" in data
        assert Path(data["target_directory"]).resolve() == Path(src_dir).resolve()
        assert Path(data["destination_directory"]).resolve() == Path(dest_dir).resolve()

        # Ensure source files were not moved during dry run
        assert (Path(src_dir) / "finance_doc.txt").exists()


def test_sort_subcommand_live_execution():
    """Test sort subcommand live batch execution."""
    with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as dest_dir:
        create_sample_corpus(src_dir)
        code, stdout, stderr = run_cli([
            "sort",
            src_dir,
            "--json",
            "--dest-dir",
            dest_dir,
        ])

        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert data["dry_run"] is False
        assert "plan" in data
        assert "summary" in data


def test_scan_subcommand_json():
    """Test scan subcommand with --json output and jq pipeline style structure."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        code, stdout, stderr = run_cli([
            "scan",
            src_dir,
            "--json",
            "--strategy",
            "default",
            "--conflict-policy",
            "rename",
        ])

        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert "plan" in data
        assert data["files_scanned"] >= 3


def test_config_subcommand_show_and_set():
    """Test config subcommand with --show and --set flags."""
    code, stdout, stderr = run_cli(["config", "--show", "--json"])
    assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert "MAX_FOLDERS" in data
    assert "CONFLICT_POLICY" in data

    code_set, stdout_set, stderr_set = run_cli([
        "config",
        "--set",
        "MAX_FOLDERS",
        "10",
        "--json",
    ])
    assert code_set == 0, f"Expected 0 exit code, got {code_set}. Stderr: {stderr_set}"
    data_set = json.loads(stdout_set)
    assert data_set["MAX_FOLDERS"] == 10


def test_subcommand_error_nonexistent_directory():
    """Test non-existent directory error causes non-zero exit code 1."""
    nonexistent = "/nonexistent/path/for/autosorter/test/12345"
    code, stdout, stderr = run_cli(["sort", nonexistent, "--json"])
    assert code == 1
    assert "does not exist" in stderr


def test_subcommand_invalid_argument_exit_code():
    """Test invalid argument causes exit code 2 (usage error)."""
    code, stdout, stderr = run_cli(["sort", "/tmp", "--invalid-argument-xyz"])
    assert code == 2
    assert "unrecognized arguments" in stderr or "invalid" in stderr


def test_sandbox_cli_json():
    """Test sandbox_cli.py analyze --json outputs raw JSON without decorative borders."""
    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()

    try:
        # First reset sandbox
        with patch("sys.argv", ["sandbox_cli.py", "reset"]), patch("sys.stdout", stdout_cap), patch("sys.stderr", stderr_cap):
            import sandbox_cli

            sandbox_cli.main()

        stdout_cap = io.StringIO()
        stderr_cap = io.StringIO()

        # Run analyze with --json
        with patch("sys.argv", ["sandbox_cli.py", "analyze", "--json"]), patch("sys.stdout", stdout_cap), patch("sys.stderr", stderr_cap):
            sandbox_cli.main()

        out = stdout_cap.getvalue()
        assert "--- Analysis Sorting Plan ---" not in out
        data = json.loads(out)
        assert isinstance(data, dict)
    finally:
        try:
            with patch("sys.argv", ["sandbox_cli.py", "reset"]), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                import sandbox_cli

                sandbox_cli.main()
        except Exception:
            pass
