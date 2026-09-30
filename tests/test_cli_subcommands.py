"""Tests for unified CLI subcommands and structured JSON output."""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def mock_non_interactive_stdin():
    """Return patch for sys.stdin as a non-interactive stream."""
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    return patch("sys.stdin", mock_stdin)


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


def create_sample_corpus(base_dir):
    """Create a sample corpus directory with documents."""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    (base / "finance_doc.txt").write_text("Finance, investment, and banking report.")
    (base / "tech_doc.txt").write_text(
        "Software engineering, Python, algorithms, and computers."
    )
    (base / "health_doc.txt").write_text(
        "Medical science, clinical trial, doctor, and patient data."
    )
    (base / "empty.txt").write_text("")


@pytest.mark.xdist_group(name="cli_subcommands")
def test_sort_subcommand_dry_run_json():
    """Test sort subcommand with --dry-run and --json flags."""
    with (
        tempfile.TemporaryDirectory() as src_dir,
        tempfile.TemporaryDirectory() as dest_dir,
    ):
        create_sample_corpus(src_dir)
        code, stdout, stderr = run_cli(
            [
                "sort",
                src_dir,
                "--json",
                "--dest-dir",
                dest_dir,
                "--dry-run",
                "--max-folders",
                "5",
            ]
        )

        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert data["dry_run"] is True
        assert "plan" in data
        assert Path(data["target_directory"]).resolve() == Path(src_dir).resolve()
        assert Path(data["destination_directory"]).resolve() == Path(dest_dir).resolve()

        # Ensure source files were not moved during dry run
        assert (Path(src_dir) / "finance_doc.txt").exists()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_sort_subcommand_live_execution():
    """Test sort subcommand live batch execution."""
    with (
        tempfile.TemporaryDirectory() as src_dir,
        tempfile.TemporaryDirectory() as dest_dir,
    ):
        create_sample_corpus(src_dir)
        code, stdout, stderr = run_cli(
            [
                "sort",
                src_dir,
                "--json",
                "--dest-dir",
                dest_dir,
            ]
        )

        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert data["dry_run"] is False
        assert "plan" in data
        assert "summary" in data


@pytest.mark.xdist_group(name="cli_subcommands")
def test_scan_subcommand_json():
    """Test scan subcommand with --json output and jq pipeline style structure."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        code, stdout, stderr = run_cli(
            [
                "scan",
                src_dir,
                "--json",
                "--strategy",
                "default",
                "--conflict-policy",
                "rename",
            ]
        )

        assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
        data = json.loads(stdout)
        assert data["status"] == "success"
        assert "plan" in data
        assert data["files_scanned"] >= 3


@pytest.mark.xdist_group(name="cli_subcommands")
def test_config_subcommand_show_and_set():
    """Test config subcommand with --show and --set flags."""
    code, stdout, stderr = run_cli(["config", "--show", "--json"])
    assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert "MAX_FOLDERS" in data
    assert "CONFLICT_POLICY" in data

    code_set, stdout_set, stderr_set = run_cli(
        [
            "config",
            "--set",
            "MAX_FOLDERS",
            "10",
            "--json",
        ]
    )
    assert code_set == 0, f"Expected 0 exit code, got {code_set}. Stderr: {stderr_set}"
    data_set = json.loads(stdout_set)
    assert data_set["MAX_FOLDERS"] == 10


@pytest.mark.xdist_group(name="cli_subcommands")
def test_subcommand_error_nonexistent_directory():
    """Test non-existent directory error causes non-zero exit code 1."""
    nonexistent = "/nonexistent/path/for/autosorter/test/12345"
    code, stdout, stderr = run_cli(["sort", nonexistent, "--json"])
    assert code == 1
    assert "does not exist" in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_subcommand_invalid_argument_exit_code():
    """Test invalid argument causes exit code 2 (usage error)."""
    code, stdout, stderr = run_cli(["sort", "/tmp", "--invalid-argument-xyz"])
    assert code == 2
    assert "unrecognized arguments" in stderr or "invalid" in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_sandbox_cli_json():
    """Test sandbox_cli.py analyze --json outputs raw JSON without decorative borders."""
    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()

    try:
        # First reset sandbox
        with (
            patch("sys.argv", ["sandbox_cli.py", "reset"]),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
        ):
            import sandbox_cli

            sandbox_cli.main()

        stdout_cap = io.StringIO()
        stderr_cap = io.StringIO()

        # Run analyze with --json
        with (
            patch("sys.argv", ["sandbox_cli.py", "analyze", "--json"]),
            patch("sys.stdout", stdout_cap),
            patch("sys.stderr", stderr_cap),
        ):
            sandbox_cli.main()

        out = stdout_cap.getvalue()
        assert "--- Analysis Sorting Plan ---" not in out
        data = json.loads(out)
        assert isinstance(data, dict)
    finally:
        try:
            with (
                patch("sys.argv", ["sandbox_cli.py", "reset"]),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                import sandbox_cli

                sandbox_cli.main()
        except Exception:
            pass


@pytest.mark.xdist_group(name="cli_subcommands")
def test_quiet_flag_suppresses_informational_prints():
    """Test that --quiet / -q flag silences non-essential progress and banner prints."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)

        # Test sort with --quiet
        code, stdout, stderr = run_cli(["sort", src_dir, "--quiet", "--dry-run"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "Batch sorting completed successfully" not in stdout
        assert "Dry-run mode" not in stdout
        assert "finance_doc.txt" in stdout

        # Test scan with -q
        code, stdout, stderr = run_cli(["scan", src_dir, "-q"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "Scan analysis completed" not in stdout
        assert "finance_doc.txt" in stdout

        # Test config with --quiet
        code, stdout, stderr = run_cli(["config", "--show", "--quiet"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "Application Settings:" not in stdout
        assert "MAX_FOLDERS" in stdout


@pytest.mark.xdist_group(name="cli_subcommands")
def test_no_color_flag_and_env_var_strips_ansi():
    """Test that --no-color flag and NO_COLOR env var strip ANSI escape sequences."""
    from app.main import strip_ansi_codes

    ansi_text = "\x1b[31mRed Alert\x1b[0m \x1b[1mBold Text\x1b[0m"
    assert strip_ansi_codes(ansi_text) == "Red Alert Bold Text"

    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)

        # Test with --no-color flag
        code, stdout, stderr = run_cli(["scan", src_dir, "--no-color", "--json"])
        assert code == 0
        assert "\x1b[" not in stdout
        assert "\x1b[" not in stderr

        # Test with NO_COLOR env var
        code, stdout, stderr = run_cli(
            ["scan", src_dir, "--json"], env={"NO_COLOR": "1"}
        )
        assert code == 0
        assert "\x1b[" not in stdout
        assert "\x1b[" not in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_build_parser_factory():
    """Test that build_parser() returns a fully configured ArgumentParser instance."""
    import argparse

    from app.main import build_parser

    parser = build_parser()
    assert isinstance(parser, argparse.ArgumentParser)
    assert parser.prog == "app/main.py"

    help_text = parser.format_help()
    assert "--demo" in help_text
    assert "--smoke-test" in help_text
    assert "--tui" in help_text
    assert "sort" in help_text
    assert "scan" in help_text
    assert "config" in help_text
    assert "daemon" in help_text

    subparsers_action = next(
        (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)),
        None,
    )
    assert subparsers_action is not None
    assert set(subparsers_action.choices.keys()) == {
        "sort",
        "scan",
        "config",
        "daemon",
        "crypto",
        "ledger",
        "quarantine",
        "cro",
    }

    sort_parser = subparsers_action.choices["sort"]
    sort_help = sort_parser.format_help()
    assert "directory" in sort_help
    assert "--dest-dir" in sort_help
    assert "--dry-run" in sort_help


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_no_args_exits_code_2_with_guidance():
    """Test non-interactive invocation without subcommands or path exits with code 2 and guidance."""
    with mock_non_interactive_stdin():
        code, stdout, stderr = run_cli([])
        assert code == 2, f"Expected exit code 2, got {code}. Stderr: {stderr}"
        assert "usage:" in stderr or "subcommand" in stderr
        assert "sort" in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_positional_path_autoroutes_to_sort():
    """Test non-interactive invocation with target path auto-routes to batch sorting."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        with mock_non_interactive_stdin():
            code, stdout, stderr = run_cli([src_dir, "-q"])
            assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
            assert "finance_doc.txt" in stdout


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_gui_flag_honored():
    """Test non-interactive invocation with explicit --gui flag calls UI app launcher."""
    with mock_non_interactive_stdin(), patch("app.ui.tui.run_tui") as mock_run_tui:
        code, stdout, stderr = run_cli(["--gui"])
        assert code == 0
        mock_run_tui.assert_called_once()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_explicit_sort_subcommand():
    """Test non-interactive invocation with explicit sort subcommand executes batch sorting."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        with mock_non_interactive_stdin():
            code, stdout, stderr = run_cli(["sort", src_dir, "-q"])
            assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
            assert "finance_doc.txt" in stdout


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_no_color_guidance_strips_ansi():
    """Test non-interactive guidance output strips ANSI escape codes when --no-color is set."""
    with mock_non_interactive_stdin():
        code, stdout, stderr = run_cli(["--no-color"])
        assert code == 2
        assert "\x1b[" not in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_gui_exe_name_honored():
    """Test non-interactive invocation via smart-autosorter-gui executable launches UI app."""
    with mock_non_interactive_stdin(), patch("app.ui.tui.run_tui") as mock_run_tui:
        code, stdout, stderr = run_cli([], prog="smart-autosorter-gui.exe")
        assert code == 0
        mock_run_tui.assert_called_once()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_format_table_scan_and_sort():
    """Test --format table displays ASCII table for scan and sort subcommands."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)

        # Test scan --format table
        code, stdout, stderr = run_cli(["scan", src_dir, "--format", "table"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "+---" in stdout
        assert "| Original Path" in stdout
        assert "| Target Path" in stdout
        assert "finance_doc.txt" in stdout

        # Test sort --format table --dry-run
        code, stdout, stderr = run_cli(["sort", src_dir, "-f", "table", "--dry-run"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "+---" in stdout
        assert "| Original Path" in stdout
        assert "finance_doc.txt" in stdout


@pytest.mark.xdist_group(name="cli_subcommands")
def test_format_text_scan_and_sort():
    """Test --format text outputs line-delimited tab-separated mappings."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)

        # Test sort --format text --dry-run
        code, stdout, stderr = run_cli(["sort", src_dir, "--format", "text", "--dry-run"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "+---" not in stdout
        assert "\t" in stdout
        assert "finance_doc.txt" in stdout

        # Test scan --format text
        code, stdout, stderr = run_cli(["scan", src_dir, "-f", "text"])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "+---" not in stdout
        assert "\t" in stdout
        assert "finance_doc.txt" in stdout


@pytest.mark.xdist_group(name="cli_subcommands")
def test_stdin_piped_file_paths_ingestion():
    """Test reading line-delimited file paths from sys.stdin without positional directory argument."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        p1 = os.path.join(src_dir, "finance_doc.txt")
        p2 = os.path.join(src_dir, "tech_doc.txt")
        piped_input = f"{p1}\n{p2}\n"

        mock_stdin = io.StringIO(piped_input)
        mock_stdin.isatty = lambda: False  # type: ignore

        with patch("sys.stdin", mock_stdin):
            code, stdout, stderr = run_cli(["scan", "--format", "text"])
            assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
            assert "finance_doc.txt" in stdout
            assert "tech_doc.txt" in stdout
            assert "\t" in stdout


@pytest.mark.xdist_group(name="cli_subcommands")
def test_missing_directory_and_empty_stdin_error():
    """Test error message and exit code when positional directory and stdin paths are missing."""
    mock_stdin = io.StringIO("")
    mock_stdin.isatty = lambda: False  # type: ignore

    with patch("sys.stdin", mock_stdin):
        code, stdout, stderr = run_cli(["scan"])
        assert code == 1, f"Expected exit code 1, got {code}. Stdout: {stdout}"
        assert "Error:" in stderr
        assert "standard input" in stderr or "directory" in stderr
