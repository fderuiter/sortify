"""Tests for shared CLI parent parsers, scoped sort overrides, daemon JSON, and executable demos."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.cli.undo_cli import register_subparser as register_undo_subparser
from app.config import AppSettings
from app.core.db_conn import clear_connection_cache
from app.core.sample_corpus import SAMPLE_CORPUS_ITEMS
from app.demo import ensure_demo_workspace
from app.main import (
    SortifyArgumentParser,
    _prompt_workspace_selection,
    build_parser,
    handle_config_command,
    main,
)


def _invoke_cli(
    argv: list[str],
    *,
    cwd: Path,
    app_dir: Path,
    keys_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, str, str]:
    """Run ``app.main.main()`` in-process with isolated environment, cwd, and stdio."""
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(keys_dir))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("SMOKE_TEST", "0")
    monkeypatch.setattr("sys.argv", ["smart-autosorter", *argv])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    SortifyArgumentParser._active_cli_args = None
    setattr(AppSettings, "_instance", None)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0
    try:
        with (
            contextlib.redirect_stdout(stdout_buf),
            contextlib.redirect_stderr(stderr_buf),
        ):
            main()
    except SystemExit as exc:
        if exc.code is None:
            exit_code = 0
        elif isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 1
    finally:
        clear_connection_cache(only_current_and_inactive=False)
        setattr(AppSettings, "_instance", None)
        SortifyArgumentParser._active_cli_args = None

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def test_root_parser_retains_deterministic_defaults() -> None:
    """Top-level invocations without subcommands retain deterministic defaults."""
    parser = build_parser()
    args = parser.parse_args([])

    assert args.subcommand is None
    assert args.json is False
    assert args.quiet is False
    assert args.no_color is False
    assert args.demo is False
    assert args.smoke_test is False
    assert args.daemon is False
    assert args.tui is False
    assert args.interactive is False
    assert args.accept_ai_consent is False
    assert args.decline_ai_consent is False
    assert args.skip_wizard is False
    assert args.non_interactive is False
    assert args.settings_file is None
    assert args.profile is None
    assert args.duplicates is None
    assert args.detect_original_by is None


def test_undo_register_subparser_is_self_contained() -> None:
    """undo_cli.register_subparser registers undo with export/format/ledger-db and global parent flags."""
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="subcommand")
    undo_parser = register_undo_subparser(subparsers)

    parsed = root.parse_args(
        [
            "undo",
            "--latest",
            "--export",
            "audit.csv",
            "-f",
            "csv",
            "--ledger-db",
            "/tmp/store.db",
            "--json",
        ]
    )
    assert undo_parser.prog.endswith("undo")
    assert parsed.subcommand == "undo"
    assert parsed.latest is True
    assert parsed.export == "audit.csv"
    assert parsed.format == "csv"
    assert parsed.ledger_db == "/tmp/store.db"
    assert parsed.json is True


@pytest.mark.parametrize(
    ("flag_tokens", "attr", "expected"),
    [
        (["--json"], "json", True),
        (["-q"], "quiet", True),
        (["--no-color"], "no_color", True),
        (["--tui"], "tui", True),
        (["--interactive"], "interactive", True),
        (["--accept-ai-consent"], "accept_ai_consent", True),
        (["--decline-ai-consent"], "decline_ai_consent", True),
        (["--skip-wizard"], "skip_wizard", True),
        (["--non-interactive"], "non_interactive", True),
        (["--profile", "ops"], "profile", "ops"),
    ],
)
@pytest.mark.parametrize(
    "cmd_tokens",
    [
        ["sort", "/tmp/work"],
        ["scan", "/tmp/work"],
        ["config", "--show"],
        ["daemon", "status", "/tmp/work"],
        ["undo", "--list"],
        ["undo", "--export", "/tmp/audit.json"],
        ["drop", "/tmp/work"],
        ["ledger", "status"],
        ["ledger", "reconcile"],
        ["ledger", "purge", "--completed", "--force"],
        ["ledger", "export"],
        ["quarantine", "list"],
        ["quarantine", "inspect", "job-1"],
        ["quarantine", "process", "job-1"],
        ["quarantine", "release", "job-1"],
        ["quarantine", "dismiss", "job-1"],
        ["model", "status"],
        ["model", "verify"],
        ["model", "remove"],
        ["rules", "list"],
        ["rules", "add", "invoice", "Finance/Invoices"],
        ["rules", "remove", "invoice"],
        ["rules", "enable", "invoice"],
        ["rules", "disable", "invoice"],
        ["rules", "import", "pack.json"],
        ["rules", "export", "out.json"],
    ],
)
def test_global_flags_preserved_before_between_and_after_all_subcommands(
    flag_tokens: list[str],
    attr: str,
    expected: Any,
    cmd_tokens: list[str],
) -> None:
    """Global flags work identically before, between, or after subcommands."""
    parser = build_parser()

    before_ns = parser.parse_args([*flag_tokens, *cmd_tokens])
    assert getattr(before_ns, attr) == expected

    after_ns = parser.parse_args([*cmd_tokens, *flag_tokens])
    assert getattr(after_ns, attr) == expected

    if cmd_tokens[0] in ("ledger", "quarantine", "model", "rules"):
        mid_ns = parser.parse_args([cmd_tokens[0], *flag_tokens, *cmd_tokens[1:]])
        assert getattr(mid_ns, attr) == expected


def test_cross_level_consent_mutual_exclusion_exits_2() -> None:
    """Cross-level --accept-ai-consent and --decline-ai-consent remain mutually exclusive."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--accept-ai-consent", "sort", "--decline-ai-consent"])
    assert exc_info.value.code == 2


def test_json_before_and_after_subcommands_emits_valid_stdout_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--json and consent flags work before or after subcommands (including undo --export)."""
    app_dir = tmp_path / "app_state"
    keys_dir = tmp_path / "app_keys"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    sample_dir = work_dir / "docs"
    sample_dir.mkdir()
    (sample_dir / "finance_report.txt").write_text(
        "Quarterly finance invoice banking statement and tax summary.",
        encoding="utf-8",
    )
    export_a = tmp_path / "audit_prefix.json"
    export_b = tmp_path / "audit_suffix.json"

    commands: list[tuple[list[str], type[Any]]] = [
        (["--json", "--decline-ai-consent", "scan", str(sample_dir)], dict),
        (["scan", str(sample_dir), "--decline-ai-consent", "--json"], dict),
        (
            ["--json", "--decline-ai-consent", "sort", str(sample_dir), "--dry-run"],
            dict,
        ),
        (
            ["sort", str(sample_dir), "--dry-run", "--decline-ai-consent", "--json"],
            dict,
        ),
        (["--json", "--accept-ai-consent", "config", "--show"], dict),
        (["config", "--show", "--json"], dict),
        (["--json", "undo", "--list"], list),
        (["undo", "--list", "--json"], list),
        (["--json", "undo", "--export", str(export_a)], dict),
        (["undo", "--export", str(export_b), "--json"], dict),
        (["--json", "ledger", "status"], dict),
        (["ledger", "--json", "status"], dict),
        (["ledger", "status", "--json"], dict),
        (["--json", "quarantine", "list"], dict),
        (["quarantine", "--json", "list"], dict),
        (["quarantine", "list", "--json"], dict),
        (["--json", "--non-interactive", "model", "status"], dict),
        (["--json", "rules", "list"], dict),
    ]

    for argv, expected_type in commands:
        code, stdout, stderr = _invoke_cli(
            argv,
            cwd=work_dir,
            app_dir=app_dir,
            keys_dir=keys_dir,
            monkeypatch=monkeypatch,
        )
        assert code == 0, f"argv={argv} exited {code}. stderr={stderr}"
        parsed = json.loads(stdout)
        assert isinstance(parsed, expected_type), f"argv={argv} returned {type(parsed)}"

    assert export_a.is_file()
    assert export_b.is_file()
    saved_settings = json.loads((app_dir / "settings.json").read_text(encoding="utf-8"))
    assert saved_settings["AI_CONSENT_GRANTED"] is True


@pytest.mark.parametrize(
    ("action", "mock_payload"),
    [
        (
            "status",
            {
                "status": "running",
                "pid": 4242,
                "base_dir": "/tmp/watch",
                "uptime_seconds": 12,
                "queue_depth": 0,
                "max_queue_capacity": 1000,
                "active_workers": 1,
                "is_moving": False,
                "active_triage_paths": [],
            },
        ),
        (
            "health",
            {
                "status": "healthy",
                "pid": 4242,
                "queue_depth": 0,
                "queue_full": False,
                "uptime_seconds": 12,
            },
        ),
        (
            "metrics",
            {
                "events_enqueued": 5,
                "events_processed": 5,
                "queue_depth": 0,
                "active_workers": 0,
                "uptime_seconds": 12,
            },
        ),
        ("pause", {"status": "paused", "message": "Daemon triage pipeline paused"}),
        ("resume", {"status": "resumed", "message": "Daemon triage pipeline resumed"}),
        ("stop", {"status": "stopping", "message": "Daemon shutting down"}),
    ],
)
def test_daemon_actions_emit_structured_json_before_and_after_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    mock_payload: dict[str, Any],
) -> None:
    """All daemon actions emit structured JSON on stdout when --json is passed."""
    app_dir = tmp_path / "app_state"
    keys_dir = tmp_path / "app_keys"
    watch_dir = tmp_path / "watched"
    watch_dir.mkdir()

    for argv in (
        ["--json", "daemon", action, str(watch_dir)],
        ["daemon", action, str(watch_dir), "--json"],
    ):
        with patch(
            "app.core.ipc.DaemonIPCClient.send_command",
            new=AsyncMock(return_value=dict(mock_payload)),
        ) as mocked_send:
            code, stdout, stderr = _invoke_cli(
                argv,
                cwd=tmp_path,
                app_dir=app_dir,
                keys_dir=keys_dir,
                monkeypatch=monkeypatch,
            )
        assert code == 0, f"argv={argv} failed: {stderr}"
        mocked_send.assert_awaited_once_with(action)
        parsed = json.loads(stdout)
        for key, value in mock_payload.items():
            assert parsed[key] == value


@pytest.mark.parametrize(
    "bad_argv",
    [
        ["config", "--max-folders", "25"],
        ["config", "--conflict-policy", "overwrite"],
        ["config", "--strategy", "default"],
        ["config", "--duplicates", "skip"],
        ["config", "--contextual-renaming"],
        ["config", "--export-report", "out.json"],
        ["--duplicates", "skip", "config", "--show"],
        ["--detect-original-by", "created", "config", "--show"],
        ["undo", "--list", "--max-folders", "25"],
        ["undo", "--latest", "--conflict-policy", "overwrite"],
        ["undo", "--export", "audit.json", "--max-folders", "25"],
        ["--duplicates", "skip", "undo", "--list"],
    ],
)
def test_config_and_undo_reject_transient_sort_flags_and_leave_settings_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_argv: list[str],
) -> None:
    """config and undo reject transient sort flags and preserve persisted settings."""
    app_dir = tmp_path / "app_state"
    keys_dir = tmp_path / "app_keys"
    app_dir.mkdir(parents=True, exist_ok=True)
    settings_path = app_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"MAX_FOLDERS": 12, "CONFLICT_POLICY": "rename"}, indent=2),
        encoding="utf-8",
    )
    before_bytes = settings_path.read_text(encoding="utf-8")

    code, _stdout, _stderr = _invoke_cli(
        bad_argv,
        cwd=tmp_path,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code == 2
    assert settings_path.read_text(encoding="utf-8") == before_bytes


def test_handle_config_command_never_prints_transient_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct handle_config_command calls ignore unpersisted transient sort attributes."""
    settings_file = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_file))
    assert settings.MAX_FOLDERS == 12

    synthetic_args = argparse.Namespace(
        subcommand="config",
        show=True,
        set=None,
        json=True,
        quiet=False,
        max_folders=25,
        conflict_policy="overwrite",
        accept_ai_consent=False,
        decline_ai_consent=False,
        skip_wizard=False,
        non_interactive=False,
    )
    stdout_buf = io.StringIO()
    monkeypatch.setenv("NON_INTERACTIVE", "0")
    with pytest.raises(SystemExit) as exc_info, contextlib.redirect_stdout(stdout_buf):
        handle_config_command(synthetic_args, settings)
    assert exc_info.value.code == 0
    dumped = json.loads(stdout_buf.getvalue())
    assert dumped["MAX_FOLDERS"] == 12


def test_sort_scan_and_drop_accept_conflict_policy_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sort, scan, and drop accept --conflict-policy overwrite and undo restores overwritten files."""
    app_dir = tmp_path / "app_state"
    keys_dir = tmp_path / "app_keys"

    scan_dir = tmp_path / "scan_inbox"
    scan_dir.mkdir()
    (scan_dir / "finance.txt").write_text(
        "Quarterly finance invoice payment banking summary.",
        encoding="utf-8",
    )
    code_scan, out_scan, err_scan = _invoke_cli(
        [
            "scan",
            str(scan_dir),
            "--conflict-policy",
            "overwrite",
            "--decline-ai-consent",
            "--json",
        ],
        cwd=tmp_path,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_scan == 0, err_scan
    assert json.loads(out_scan)["files_scanned"] == 1

    code_rule, _, err_rule = _invoke_cli(
        ["rules", "add", "invoice", "Finance", "--json"],
        cwd=tmp_path,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_rule == 0, err_rule

    sort_src = tmp_path / "sort_src"
    sort_dest = tmp_path / "sort_dest"
    sort_src.mkdir()
    (sort_dest / "Finance").mkdir(parents=True)
    (sort_dest / "Finance" / "bill.txt").write_text("OLD CONTENT", encoding="utf-8")
    (sort_src / "bill.txt").write_text(
        "NEW invoice content for finance review",
        encoding="utf-8",
    )

    code_sort, out_sort, err_sort = _invoke_cli(
        [
            "sort",
            str(sort_src),
            "--dest-dir",
            str(sort_dest),
            "--conflict-policy",
            "overwrite",
            "--decline-ai-consent",
            "--json",
        ],
        cwd=tmp_path,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_sort == 0, err_sort
    sort_payload = json.loads(out_sort)
    assert sort_payload["moved_count"] == 1
    assert (
        (sort_dest / "Finance" / "bill.txt")
        .read_text(encoding="utf-8")
        .startswith("NEW invoice")
    )

    code_undo, _, err_undo = _invoke_cli(
        ["undo", "--latest", "--json"],
        cwd=tmp_path,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_undo == 0, err_undo
    assert (sort_dest / "Finance" / "bill.txt").read_text(
        encoding="utf-8"
    ) == "OLD CONTENT"
    assert (sort_src / "bill.txt").exists()

    drop_src = tmp_path / "drop_src"
    drop_dest = tmp_path / "drop_dest"
    drop_src.mkdir()
    (drop_dest / "Finance").mkdir(parents=True)
    (drop_dest / "Finance" / "receipt.txt").write_text("OLD DROP", encoding="utf-8")
    drop_file = drop_src / "receipt.txt"
    drop_file.write_text("UPDATED invoice receipt for finance", encoding="utf-8")

    code_drop, out_drop, err_drop = _invoke_cli(
        [
            "drop",
            str(drop_file),
            "--dest-dir",
            str(drop_dest),
            "--conflict-policy",
            "overwrite",
            "--decline-ai-consent",
            "--json",
        ],
        cwd=tmp_path,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_drop == 0, err_drop
    assert json.loads(out_drop)["moved_count"] == 1
    assert (drop_dest / "Finance" / "receipt.txt").read_text(
        encoding="utf-8"
    ) == "UPDATED invoice receipt for finance"


def test_demo_and_preset_demo_seed_execute_undo_and_preserve_user_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seeded demos (--demo, --preset demo, and interactive choice) sort and undo cleanly."""
    app_dir = tmp_path / "app_state"
    keys_dir = tmp_path / "app_keys"
    expected_count = len(SAMPLE_CORPUS_ITEMS)

    scan_cwd = tmp_path / "empty_scan_cwd"
    scan_cwd.mkdir()
    code_scan, out_scan, err_scan = _invoke_cli(
        ["scan", "--preset", "demo", "--decline-ai-consent", "--json"],
        cwd=scan_cwd,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_scan == 0, err_scan
    scan_data = json.loads(out_scan)
    assert scan_data["files_scanned"] == expected_count
    assert scan_data["files_scanned"] > 0

    demo_cwd = tmp_path / "demo_cwd"
    gitkeep = demo_cwd / "sandbox" / "demo_workspace" / ".gitkeep"
    gitkeep.parent.mkdir(parents=True)
    gitkeep.write_text("", encoding="utf-8")

    code_demo, out_demo, err_demo = _invoke_cli(
        ["--demo", "--decline-ai-consent"],
        cwd=demo_cwd,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_demo == 0, err_demo
    assert out_demo.strip() != ""
    assert "undo --latest" in err_demo

    demo_ws = demo_cwd / "sandbox" / "demo_workspace"
    assert not (demo_ws / "demo_finance.txt").exists()
    moved_finance = list(demo_ws.rglob("demo_finance.txt"))
    assert len(moved_finance) == 1
    assert moved_finance[0].parent != demo_ws

    code_undo, out_undo, err_undo = _invoke_cli(
        ["undo", "--latest", "--json"],
        cwd=demo_cwd,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_undo == 0, err_undo
    undo_payload = json.loads(out_undo)
    assert undo_payload["status"] == "success"
    assert (demo_ws / "demo_finance.txt").is_file()
    assert (demo_ws / "demo_tech.txt").is_file()
    assert (demo_ws / "demo_health.txt").is_file()

    json_demo_cwd = tmp_path / "json_demo_cwd"
    json_demo_cwd.mkdir()
    code_demo_json, out_demo_json, err_demo_json = _invoke_cli(
        ["--json", "--demo", "--decline-ai-consent"],
        cwd=json_demo_cwd,
        app_dir=app_dir,
        keys_dir=keys_dir,
        monkeypatch=monkeypatch,
    )
    assert code_demo_json == 0, err_demo_json
    demo_json_data = json.loads(out_demo_json)
    assert demo_json_data["status"] == "success"
    assert demo_json_data["dry_run"] is False
    assert len(demo_json_data["seeded_files"]) == expected_count
    assert demo_json_data["moved_count"] > 0
    assert demo_json_data["session_id"]

    custom_cwd = tmp_path / "custom_cwd"
    custom_ws = custom_cwd / "sandbox" / "demo_workspace"
    custom_ws.mkdir(parents=True)
    user_file = custom_ws / "my_notes.txt"
    user_file.write_text("User-authored notes must stay untouched.", encoding="utf-8")

    resolved_ws, seeded = ensure_demo_workspace(custom_ws)
    assert resolved_ws == custom_ws.resolve()
    assert seeded == []
    assert not (custom_ws / "demo_finance.txt").exists()
    assert user_file.read_text(encoding="utf-8") == (
        "User-authored notes must stay untouched."
    )

    prompt_cwd = tmp_path / "prompt_cwd"
    prompt_cwd.mkdir()
    monkeypatch.chdir(prompt_cwd)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "1")
    selected_target = _prompt_workspace_selection(
        argparse.Namespace(quiet=True, no_color=True)
    )
    assert selected_target == (prompt_cwd / "sandbox" / "demo_workspace").resolve()
    assert (selected_target / "demo_finance.txt").is_file()
