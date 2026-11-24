"""Tests for unified CLI subcommands and structured JSON output."""

import io
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def mock_non_interactive_stdin():
    """Return patch for sys.stdin as a non-interactive stream."""
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    # pandas reads the console encoding from stdin on first import.
    mock_stdin.encoding = "utf-8"
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
def test_sort_subcommand_live_execution_high_entropy_tmpdir(tmp_path):
    """Regression test: sort --dest-dir succeeds when TMPDIR contains macOS-style high-entropy segments."""
    macos_tmp = (
        tmp_path / "var" / "folders" / "24" / "8k48jl6d249rn9qfxwsl6xvm0000gn" / "T"
    )
    src_dir = macos_tmp / "src"
    dest_dir = macos_tmp / "dest"
    src_dir.mkdir(parents=True)
    dest_dir.mkdir(parents=True)
    create_sample_corpus(src_dir)

    code, stdout, stderr = run_cli(
        [
            "sort",
            str(src_dir),
            "--json",
            "--dest-dir",
            str(dest_dir),
        ]
    )

    assert code == 0, (
        f"Expected 0 exit code, got {code}. Stdout: {stdout} Stderr: {stderr}"
    )
    data = json.loads(stdout)
    assert data["status"] == "success"
    assert data["dry_run"] is False


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
    """Test non-existent directory error causes non-zero exit code 1 and structured JSON payload."""
    nonexistent = "/nonexistent/path/for/autosorter/test/12345"
    code, stdout, stderr = run_cli(["sort", nonexistent, "--json"])
    assert code == 1
    data = json.loads(stdout)
    assert data["status"] == "error"
    assert "does not exist" in data["message"]


@pytest.mark.xdist_group(name="cli_subcommands")
def test_subcommand_invalid_argument_exit_code():
    """Test invalid argument causes exit code 2 (usage error)."""
    code, stdout, stderr = run_cli(["sort", "/tmp", "--invalid-argument-xyz"])
    assert code == 2
    assert "unrecognized arguments" in stderr or "invalid" in stderr


@pytest.mark.xdist_group(name="cli_subcommands")
def test_sandbox_cli_json(tmp_path, monkeypatch):
    """Test sandbox_cli.py analyze --json outputs raw JSON without decorative borders."""
    import sandbox_cli

    golden_dir = tmp_path / "sandbox" / "dataset_golden"
    dataset_dir = tmp_path / "sandbox" / "dataset"
    sandbox_db = tmp_path / "sandbox" / "sandbox.db"
    golden_dir.mkdir(parents=True)
    create_sample_corpus(str(golden_dir))

    monkeypatch.setattr(sandbox_cli, "GOLDEN_DIR", str(golden_dir))
    monkeypatch.setattr(sandbox_cli, "SANDBOX_DIR", str(dataset_dir))
    monkeypatch.setattr(sandbox_cli, "SANDBOX_DB_PATH", str(sandbox_db))

    # First reset sandbox
    with (
        patch("sys.argv", ["sandbox_cli.py", "reset"]),
        patch("sys.stdout", io.StringIO()),
        patch("sys.stderr", io.StringIO()),
    ):
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

    # Reset removes isolated .key files and pipeline-state
    with (
        patch("sys.argv", ["sandbox_cli.py", "reset"]),
        patch("sys.stdout", io.StringIO()),
        patch("sys.stderr", io.StringIO()),
    ):
        sandbox_cli.main()
    assert list((tmp_path / "sandbox").rglob("*.key")) == []


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
    assert parser.prog == "smart-autosorter"

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
        "model",
        "quarantine",
        "undo",
        "drop",
        "rules",
    }

    rules_parser = subparsers_action.choices["rules"]
    rules_help = rules_parser.format_help()
    assert "import" in rules_help
    assert "export" in rules_help

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
def test_headless_positional_path_only_previews():
    """Test a bare path with no terminal previews the plan and moves nothing."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        before = sorted(p.relative_to(src_dir) for p in Path(src_dir).rglob("*"))
        with mock_non_interactive_stdin():
            code, stdout, stderr = run_cli([src_dir])
        assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
        assert "previewing the plan only" in stderr
        assert "no files were moved" in stderr
        after = sorted(p.relative_to(src_dir) for p in Path(src_dir).rglob("*"))
        assert after == before


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_tui_flag_honored():
    """Test non-interactive invocation with explicit --tui flag calls TUI app launcher."""
    with mock_non_interactive_stdin(), patch("app.ui.tui.run_tui") as mock_run_tui:
        code, stdout, stderr = run_cli(["--tui"])
        assert code == 0
        mock_run_tui.assert_called_once()


@pytest.mark.xdist_group(name="cli_subcommands")
@pytest.mark.parametrize(
    "argv",
    [
        ["--tui", "{dir}"],
        ["{dir}", "--tui"],
        ["--skip-wizard", "--tui", "{dir}"],
    ],
)
def test_tui_flag_accepts_directory_in_any_position(tmp_path, argv):
    """Test top-level flags may come before or after the bare directory."""
    argv = [a.format(dir=tmp_path) for a in argv]
    with mock_non_interactive_stdin(), patch("app.ui.tui.run_tui") as mock_run_tui:
        code, stdout, stderr = run_cli(argv)
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    mock_run_tui.assert_called_once()
    assert mock_run_tui.call_args.args[1] == str(tmp_path)


@pytest.mark.xdist_group(name="cli_subcommands")
def test_headless_explicit_sort_subcommand():
    """Test non-interactive invocation with explicit sort subcommand executes batch sorting."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        with mock_non_interactive_stdin():
            code, stdout, stderr = run_cli(["sort", src_dir, "-q"])
            assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
            assert "finance_doc.txt" in stdout
            assert not (Path(src_dir) / "finance_doc.txt").exists()


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


@pytest.mark.xdist_group(name="cli_subcommands")
def test_drop_subcommand_dry_run_json(tmp_path):
    """Test drop subcommand with --dry-run and --json flags."""
    test_file = tmp_path / "financial_report.pdf"
    test_file.write_text("Q3 Financial results and accounting projections")

    code, stdout, stderr = run_cli(["drop", str(test_file), "--dry-run", "--json"])
    assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert data["status"] == "success"
    assert data["command"] == "drop"
    assert data["dry_run"] is True
    assert data["processed_count"] == 1
    assert len(data["results"]) == 1


def test_format_plan_tree_renders_folders_files_and_renames():
    """Plans print as an indented tree with counts, renames shown inline."""
    from app.main import format_plan_tree

    plan = {
        "Finance": {
            "2024": {
                "q1.pdf": {"__type__": "file", "relative_source": "q1.pdf"},
            },
            "Invoice-ACME.pdf": {
                "__type__": "file",
                "target_filename": "Invoice-ACME.pdf",
                "relative_source": "scan_0001.pdf",
            },
        },
        "notes.txt": None,
    }
    lines, n_files, n_folders = format_plan_tree(plan)
    assert lines == [
        "Finance/",
        "  2024/",
        "    q1.pdf",
        "  Invoice-ACME.pdf  (was scan_0001.pdf)",
        "notes.txt",
    ]
    assert (n_files, n_folders) == (3, 2)


@pytest.mark.xdist_group(name="cli_subcommands")
def test_sort_dry_run_prints_readable_plan_not_json():
    """Without --json, a dry run prints a folder tree and says nothing moved."""
    with tempfile.TemporaryDirectory() as src_dir:
        create_sample_corpus(src_dir)
        before = sorted(os.listdir(src_dir))

        code, stdout, stderr = run_cli(["sort", src_dir, "--dry-run"])
        assert code == 0, stderr
        assert not stdout.lstrip().startswith("{")
        assert "finance_doc.txt" in stdout
        assert "Would move" in stderr
        assert "no files were moved" in stderr
        assert sorted(os.listdir(src_dir)) == before


@pytest.mark.xdist_group(name="cli_subcommands")
def test_config_set_persists_to_disk(tmp_path, monkeypatch):
    """Test `config --set` writes the value to settings.json before exiting."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    import subprocess
    import sys

    # A real process: the bug is the debounced save dying with the interpreter.
    repo_root = Path(__file__).parent.parent.resolve()
    result = subprocess.run(
        [sys.executable, "-m", "app.main", "config", "--set", "MAX_FOLDERS", "7"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    saved = json.loads((app_dir / "settings.json").read_text(encoding="utf-8"))
    assert saved["MAX_FOLDERS"] == 7


@pytest.mark.xdist_group(name="cli_subcommands")
def test_one_off_flags_are_not_persisted(tmp_path, monkeypatch):
    """Test per-run flags on `scan` don't become the saved defaults."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    src_dir = tmp_path / "workspace"
    create_sample_corpus(src_dir)

    code, _, stderr = run_cli(["config", "--set", "MAX_FOLDERS", "7"])
    assert code == 0, stderr
    code, _, stderr = run_cli(
        ["scan", str(src_dir), "--max-folders", "3", "--decline-ai-consent", "--json"]
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"

    # Let any debounced save from the scan run (0.5 s timer) land first.
    time.sleep(1.0)
    saved = json.loads((app_dir / "settings.json").read_text(encoding="utf-8"))
    assert saved["MAX_FOLDERS"] == 7
    assert saved.get("AI_CONSENT_GRANTED") is False


@pytest.mark.xdist_group(name="cli_subcommands")
def test_drop_preset_is_hidden_and_warns(tmp_path):
    """drop --preset never did anything: it is hidden from help and warns if used."""
    from app.main import build_parser

    drop_parser = build_parser()._subparsers._group_actions[0].choices["drop"]
    assert "--preset" not in drop_parser.format_help()

    test_file = tmp_path / "financial_report.pdf"
    test_file.write_text("Q3 Financial results and accounting projections")
    code, stdout, stderr = run_cli(
        ["drop", str(test_file), "--preset", "demo", "--dry-run", "--json"]
    )
    assert code == 0, stderr
    assert "--preset has no effect with drop" in stderr
    assert json.loads(stdout)["processed_count"] == 1


@pytest.mark.xdist_group(name="cli_subcommands")
def test_cli_ledger_status_and_purge_failure_flags(tmp_path):
    """Test ledger status and purge CLI commands with --failed, --aborted, and --json flags."""
    from app.core.moves import MoveLedger

    db_path = str(tmp_path / "ledger.db")
    ledger = MoveLedger(db_path, auto_init=True)

    # Log an aborted transaction intent
    src_file = tmp_path / "abort_src.txt"
    src_file.write_text("aborted file content")
    dst_file = tmp_path / "abort_dst.txt"

    ledger.record_intent(
        session_id="sess-cli",
        src=str(src_file),
        dst=str(dst_file),
    )

    # Run reconciliation to transition the entry to ABORTED
    ledger.reconcile_incomplete_transactions()

    # Test 'ledger status --failed --json'
    code, stdout, stderr = run_cli(
        ["ledger", "status", "--ledger-db", db_path, "--failed", "--json"]
    )
    assert code == 0, f"Error: {stderr}"
    res = json.loads(stdout)
    assert res["status"] == "success"
    assert res["count"] == 1
    assert res["pending_entries"][0]["status"] == "ABORTED"
    assert "Move aborted" in res["pending_entries"][0]["failure_reason"]

    # Test 'ledger status --aborted'
    code, stdout, stderr = run_cli(
        ["ledger", "status", "--ledger-db", db_path, "--aborted", "--json"]
    )
    assert code == 0, f"Error: {stderr}"
    res = json.loads(stdout)
    assert res["count"] == 1

    # Test 'ledger purge --failed'
    code, stdout, stderr = run_cli(
        ["ledger", "purge", "--ledger-db", db_path, "--failed", "--json"]
    )
    assert code == 0, f"Error: {stderr}"
    res = json.loads(stdout)
    assert res["status"] == "success"

    # Verify ledger is now empty
    code, stdout, stderr = run_cli(
        ["ledger", "status", "--ledger-db", db_path, "--failed", "--json"]
    )
    assert code == 0, f"Error: {stderr}"
    res = json.loads(stdout)
    assert res["count"] == 0


@pytest.mark.xdist_group(name="cli_subcommands")
def test_config_set_collection_and_optional_bool_parsing(tmp_path):
    """Test config --set with comma-separated and JSON collections plus null optional booleans."""
    env = {"AUTOSORTER_APP_DIR": str(tmp_path / "app_cfg")}

    # Comma-separated set[str] (exact user_guide.md example)
    code, stdout, stderr = run_cli(
        ["config", "--set", "STOP_WORDS", "word1,word2,word3", "--json"],
        env=env,
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert set(data["STOP_WORDS"]) == {"word1", "word2", "word3"}

    # JSON array for set[str]
    code, stdout, stderr = run_cli(
        ["config", "--set", "STOP_WORDS", '["alpha", "beta"]', "--json"],
        env=env,
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    assert set(json.loads(stdout)["STOP_WORDS"]) == {"alpha", "beta"}

    # Comma-separated list[str]
    code, stdout, stderr = run_cli(
        ["config", "--set", "IGNORED_EXTENSIONS", "tmp,bak", "--json"],
        env=env,
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    assert json.loads(stdout)["IGNORED_EXTENSIONS"] == [".tmp", ".bak"]

    # Optional bool: true then null reset
    code, stdout, stderr = run_cli(
        ["config", "--set", "AI_CONSENT_GRANTED", "true", "--json"],
        env=env,
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    assert json.loads(stdout)["AI_CONSENT_GRANTED"] is True

    code, stdout, stderr = run_cli(
        ["config", "--set", "AI_CONSENT_GRANTED", "null", "--json"],
        env=env,
    )
    assert code == 0, f"Expected exit code 0, got {code}. Stderr: {stderr}"
    assert json.loads(stdout)["AI_CONSENT_GRANTED"] is None


@pytest.mark.xdist_group(name="cli_subcommands")
def test_drop_subcommand_dest_dir_live_and_undo(tmp_path):
    """drop --dest-dir moves dropped files and folders into dest_dir and undo --latest restores them."""
    env = {"AUTOSORTER_APP_DIR": str(tmp_path / "app_drop_dest")}
    src_dir = tmp_path / "drop_sources"
    (src_dir / "nested").mkdir(parents=True)
    standalone_doc = tmp_path / "standalone_invoice.txt"
    standalone_doc.write_text("Quarterly invoice and accounting payment balance")
    nested_zip = src_dir / "nested" / "release_bundle.zip"
    nested_zip.write_bytes(b"PK\x03\x04")

    dest_dir = tmp_path / "sorted_target"
    dest_dir.mkdir()

    code, stdout, stderr = run_cli(
        [
            "drop",
            str(standalone_doc),
            str(src_dir),
            "--dest-dir",
            str(dest_dir),
            "--json",
        ],
        env=env,
    )
    assert code == 0, f"drop --dest-dir failed ({code}): {stderr}"
    payload = json.loads(stdout)
    assert payload["status"] == "success"
    assert payload["processed_count"] == 2
    for res_entry in payload["results"]:
        audit = res_entry["summary"]["audit_report"]
        assert audit["mismatch_fail_count"] == 0
        assert audit["failed_items"] == []

    dest_files = {p.name for p in dest_dir.rglob("*") if p.is_file()}
    assert dest_files == {"standalone_invoice.txt", "release_bundle.zip"}
    assert not standalone_doc.exists()
    assert not nested_zip.exists()

    # Single undo --latest restores all dropped files and folders from the unified batch session
    assert payload.get("session_id")
    u_code, u_out, u_err = run_cli(["undo", "--latest", "--json"], env=env)
    assert u_code == 0, f"undo failed: {u_err}"
    assert json.loads(u_out)["status"] == "success"

    assert standalone_doc.exists()
    assert nested_zip.exists()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_drop_subcommand_dest_dir_blocks_protected_path(tmp_path):
    """drop --dest-dir exits with code 1 when destination is inside PROTECTED_PATHS."""
    app_dir = tmp_path / "app_prot_dest"
    env = {"AUTOSORTER_APP_DIR": str(app_dir)}
    prot_dir = tmp_path / "protected_zone"
    prot_dir.mkdir()

    cfg_code, _, cfg_err = run_cli(
        ["config", "--set", "PROTECTED_PATHS", json.dumps([str(prot_dir)]), "--json"],
        env=env,
    )
    assert cfg_code == 0, cfg_err

    src_file = tmp_path / "note.txt"
    src_file.write_text("Some notes")

    code, stdout, stderr = run_cli(
        [
            "drop",
            str(src_file),
            "--dest-dir",
            str(prot_dir / "subdir"),
            "--json",
        ],
        env=env,
    )
    assert code == 1
    err_data = json.loads(stdout or stderr)
    assert err_data["status"] == "error"
    assert "protected" in err_data["message"].lower()
    assert src_file.exists()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_drop_subcommand_tui_resolves_directory_and_passes_dropzone_args(
    tmp_path, monkeypatch
):
    """drop <file> --tui passes parent/dest directory (never file path) and open_dropzone=True to run_tui."""
    import app.ui.tui

    captured: list[dict] = []

    def fake_run_tui(settings, initial_dir=None, **kwargs):
        captured.append({"initial_dir": initial_dir, **kwargs})

    monkeypatch.setattr(app.ui.tui, "run_tui", fake_run_tui)
    env = {"AUTOSORTER_APP_DIR": str(tmp_path / "app_drop_tui")}

    src_dir = tmp_path / "inbox"
    src_dir.mkdir()
    dropped_file = src_dir / "report.pdf"
    dropped_file.write_text("Report body")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    code1, _, stderr1 = run_cli(
        ["drop", str(dropped_file), "--tui", "--skip-wizard"],
        env=env,
    )
    assert code1 == 0, stderr1
    assert len(captured) == 1
    assert captured[0]["initial_dir"] == str(src_dir.resolve())
    assert captured[0]["open_dropzone"] is True
    assert captured[0]["initial_drop_paths"] == [str(dropped_file)]
    assert captured[0].get("initial_dest_dir") is None

    code2, _, stderr2 = run_cli(
        [
            "drop",
            str(dropped_file),
            "--dest-dir",
            str(dest_dir),
            "--tui",
            "--skip-wizard",
        ],
        env=env,
    )
    assert code2 == 0, stderr2
    assert len(captured) == 2
    assert captured[1]["initial_dir"] == str(dest_dir.resolve())
    assert captured[1]["open_dropzone"] is True
    assert captured[1]["initial_drop_paths"] == [str(dropped_file)]
    assert captured[1]["initial_dest_dir"] == str(dest_dir.resolve())


@pytest.mark.xdist_group(name="cli_subcommands")
def test_drop_subcommand_blocks_bundle_and_quarantine_and_sorts_multi_folder_in_place(
    tmp_path,
):
    """CLI drop rejects .app/quarantine targets and sorts multi-folder .sketch/.fig in place under one undo."""
    env = {"AUTOSORTER_APP_DIR": str(tmp_path / "app_drop_guards")}
    calc_app = tmp_path / "Calculator.app"
    (calc_app / "Contents" / "MacOS").mkdir(parents=True)
    bin_file = calc_app / "Contents" / "MacOS" / "Calculator"
    bin_file.write_text("binary")

    quarantine_dir = tmp_path / "_Compliance_Quarantine"
    quarantine_dir.mkdir()
    quar_file = quarantine_dir / "item.pdf"
    quar_file.write_text("quarantined")

    # Dropping only blocked bundle/quarantine exits with code 1 and moves zero internal files
    b_code, b_out, b_err = run_cli(
        ["drop", str(calc_app), str(quarantine_dir), "--json"],
        env=env,
    )
    assert b_code == 1
    assert json.loads(b_out or b_err)["status"] == "error"
    assert bin_file.exists()
    assert quar_file.exists()

    # Multi-folder drop without --dest-dir sorts each folder in place under one session_id
    folder_a = tmp_path / "folder_a"
    folder_b = tmp_path / "folder_b"
    folder_a.mkdir()
    folder_b.mkdir()
    sketch_a = folder_a / "mockup.sketch"
    fig_b = folder_b / "prototype.fig"
    sketch_a.write_bytes(b"\x89SKETCH")
    fig_b.write_bytes(b"\x89FIGMA")

    m_code, m_out, m_err = run_cli(
        [
            "drop",
            str(calc_app),
            str(quarantine_dir),
            str(sketch_a),
            str(fig_b),
            "--json",
        ],
        env=env,
    )
    assert m_code == 0, m_err
    m_payload = json.loads(m_out)
    assert m_payload["status"] == "success"
    assert m_payload["session_id"]
    assert bin_file.exists()
    assert quar_file.exists()
    assert (folder_a / "Miscellaneous" / "mockup.sketch").exists()
    assert (folder_b / "Miscellaneous" / "prototype.fig").exists()

    u_code, u_out, u_err = run_cli(["undo", "--latest", "--json"], env=env)
    assert u_code == 0, u_err
    assert json.loads(u_out)["status"] == "success"
    assert sketch_a.exists()
    assert fig_b.exists()


@pytest.mark.xdist_group(name="cli_subcommands")
def test_sort_conflict_policy_overwrite_displaces_existing_file_and_undo_restores(
    tmp_path,
):
    """sort --conflict-policy overwrite replaces an existing destination file via .sortify_overwritten backup and undo --latest restores both files."""
    from app.core.mover import OVERWRITE_BACKUP_DIR

    env = {"AUTOSORTER_APP_DIR": str(tmp_path / "app_overwrite_cli")}
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Source file in workspace root that routes via keyword rule to Finance/invoice.txt
    src_invoice = ws / "invoice.txt"
    src_invoice.write_text("NEW_INVOICE_FROM_ROOT quarterly invoice payment billing")

    # Pre-existing stationary file at Finance/invoice.txt
    finance_dir = ws / "Finance"
    finance_dir.mkdir()
    existing_dest = finance_dir / "invoice.txt"
    existing_dest.write_text("ORIGINAL_EXISTING_DESTINATION_BYTES")

    # Configure deterministic keyword rule routing 'invoice' -> 'Finance'
    code_cfg, _, stderr_cfg = run_cli(
        ["config", "--set", "KEYWORD_RULES", '{"invoice": "Finance"}'],
        env=env,
    )
    assert code_cfg == 0, stderr_cfg

    code_sort, stdout_sort, stderr_sort = run_cli(
        [
            "sort",
            str(ws),
            "--conflict-policy",
            "overwrite",
            "--no-contextual-renaming",
            "--json",
        ],
        env=env,
    )
    assert code_sort == 0, f"Overwrite sort failed: {stderr_sort}"
    sort_data = json.loads(stdout_sort)
    assert sort_data["status"] == "success"

    assert not src_invoice.exists()
    assert existing_dest.exists()
    assert (
        existing_dest.read_text()
        == "NEW_INVOICE_FROM_ROOT quarterly invoice payment billing"
    )
    assert not (finance_dir / "invoice_1.txt").exists()
    assert (ws / OVERWRITE_BACKUP_DIR).is_dir()

    # Undo restores both the displaced original file and the moved source file
    code_undo, stdout_undo, stderr_undo = run_cli(
        ["undo", "--latest", "--json"],
        env=env,
    )
    assert code_undo == 0, f"Undo failed: {stderr_undo}"
    undo_data = json.loads(stdout_undo)
    assert undo_data["status"] == "success"
    assert existing_dest.read_text() == "ORIGINAL_EXISTING_DESTINATION_BYTES"
    assert (
        src_invoice.read_text()
        == "NEW_INVOICE_FROM_ROOT quarterly invoice payment billing"
    )
