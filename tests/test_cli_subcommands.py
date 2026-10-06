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
        "ledger",
        "quarantine",
        "undo",
    }

    undo_parser = subparsers_action.choices["undo"]
    undo_help = undo_parser.format_help()
    assert "--session-id" in undo_help
    assert "--list" in undo_help
    assert "--latest" in undo_help
    assert "--force" in undo_help
    assert "--json" in undo_help

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
def test_headless_tui_flag_honored():
    """Test non-interactive invocation with explicit --tui flag calls TUI app launcher."""
    with mock_non_interactive_stdin(), patch("app.ui.tui.run_tui") as mock_run_tui:
        code, stdout, stderr = run_cli(["--tui"])
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
def test_preset_demo_sorting(tmp_path, monkeypatch):
    """Test sort --preset demo runs batch sorting against sandbox/demo_workspace."""
    demo_dir = tmp_path / "sandbox" / "demo_workspace"
    create_sample_corpus(demo_dir)
    monkeypatch.chdir(tmp_path)

    code, stdout, stderr = run_cli(["sort", "--preset", "demo", "--json"])
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert data["status"] == "success"
    assert Path(data["target_directory"]).resolve() == demo_dir.resolve()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_preset_downloads_scan_json(tmp_path, monkeypatch):
    """Test scan --preset downloads --json outputs directory scan JSON for ~/Downloads."""
    fake_home = tmp_path / "user_home"
    downloads_dir = fake_home / "Downloads"
    create_sample_corpus(downloads_dir)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    code, stdout, stderr = run_cli(["scan", "--preset", "downloads", "--json"])
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert data["status"] == "success"
    assert Path(data["target_directory"]).resolve() == downloads_dir.resolve()
    assert "plan" in data


@pytest.mark.xdist_group(name="cli_subcommands")
def test_preset_nonexistent_directory_error(tmp_path, monkeypatch):
    """Test preset with non-existent directory fails gracefully with descriptive error."""
    fake_home = tmp_path / "nonexistent_home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    code, stdout, stderr = run_cli(["scan", "--preset", "downloads"])
    assert code == 1
    assert "does not exist" in stderr or "Error" in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_undo_subcommand_list_and_rollback(tmp_path, monkeypatch):
    """Test undo --list and undo --latest / --session-id commands."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    src_dir = tmp_path / "workspace"
    create_sample_corpus(src_dir)

    # First, run live sort to create a historical session
    code_sort, stdout_sort, stderr_sort = run_cli(["sort", str(src_dir), "--json"])
    assert code_sort == 0, f"Sort failed: {stderr_sort}"

    # Test undo --list --json
    code_list, stdout_list, stderr_list = run_cli(["undo", "--list", "--json"])
    assert code_list == 0, f"Undo list failed: {stderr_list}"
    sessions = json.loads(stdout_list)
    assert isinstance(sessions, list)
    assert len(sessions) >= 1
    session_id = sessions[0]["session_id"]

    # Test undo --list human readable text
    code_list_txt, stdout_list_txt, stderr_list_txt = run_cli(["undo", "--list"])
    assert code_list_txt == 0
    assert "Session ID:" in stdout_list_txt or "Session ID:" in stderr_list_txt

    # Test undo --session-id <UUID> --json
    code_undo, stdout_undo, stderr_undo = run_cli(
        ["undo", "--session-id", session_id, "--json"]
    )
    assert code_undo == 0, f"Undo failed: {stderr_undo}"
    data_undo = json.loads(stdout_undo)
    assert data_undo["status"] == "success"
    assert data_undo["session_id"] == session_id
