"""Comprehensive tests for central folder-scoped rules, CLI profiles, learned rule toggles, and lossless sync."""

from __future__ import annotations

import asyncio
import configparser
import io
import json
import os
import plistlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from textual.widgets import Checkbox, DataTable, Input

from app.config import (
    AppSettings,
    Settings,
    get_app_dir,
    normalize_folder_scope_key,
    resolve_profile_settings_path,
)
from app.core.db_conn import clear_connection_cache
from app.core.downloader import resolve_default_model_dir
from app.core.metadata import MetadataPass, matches_learned_rule_pattern
from app.core.pipeline import SortPipeline
from app.core.rule_templates import apply_starter_templates, export_active_rules
from app.core.scanner import get_files_recursively
from app.core.services import acquire_services
from app.core.session import AppSession
from app.core.store import STORE_FILENAME
from app.main import build_parser
from app.ui.modals.edit import DirectorySelectModal
from app.ui.modals.rules import ActiveRuleManagerModal, RuleImportModal
from app.ui.tui import AutoSorterTUI


def run_cli(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Execute ``app.main.main()`` in-process and capture ``(returncode, stdout, stderr)``."""
    old_env = os.environ.copy()
    if env:
        os.environ.update(env)

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    mock_stdin.encoding = "utf-8"

    code = 0
    with (
        patch("sys.argv", ["smart-autosorter", *args]),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch("sys.stdin", mock_stdin),
    ):
        try:
            from app.main import main

            main()
        except SystemExit as exc:
            code = (
                exc.code
                if isinstance(exc.code, int)
                else (0 if exc.code is None else 1)
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    return code, stdout_cap.getvalue(), stderr_cap.getvalue()


def _flatten_plan_targets(plan: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Map source filename -> planned folder category from a SortPipeline tree plan."""
    mapping: dict[str, str] = {}
    for key, val in (plan or {}).items():
        if not isinstance(val, dict):
            continue
        if val.get("__type__") == "file":
            src = os.path.basename(str(val.get("source") or key))
            mapping[src] = prefix.strip("/")
        elif val.get("__type__") == "directory":
            continue
        else:
            next_prefix = f"{prefix}/{key}" if prefix else str(key)
            mapping.update(_flatten_plan_targets(val, next_prefix))
    return mapping


def test_settings_schema_validates_scoped_and_disabled_learned_rules(tmp_path: Path):
    """Requirements 1 & 2: Settings validates DISABLED_LEARNED_RULES, FOLDER_RULE_SCOPES, and ScopedSettingsView."""
    with pytest.raises(ValidationError):
        Settings(DISABLED_LEARNED_RULES={"q1 report": "../Escape"})

    with pytest.raises(ValidationError):
        Settings(
            FOLDER_RULE_SCOPES={
                str(tmp_path / "dl"): {"KEYWORD_RULES": {"invoice": "/abs/escape"}}
            }
        )

    settings_file = tmp_path / "central_settings.json"
    settings = AppSettings(filepath=str(settings_file))
    settings.KEYWORD_RULES = {"global_note": "Notes/Global"}

    dl_dir = tmp_path / "Downloads"
    code_dir = tmp_path / "code_project"
    dl_dir.mkdir()
    code_dir.mkdir()

    dl_scope = settings.for_folder(str(dl_dir))
    dl_scope.KEYWORD_RULES = {"invoice": "Finances/Invoices"}
    dl_scope.LEARNED_RULES = {"q1 tax report": "Finances/Taxes"}
    dl_scope.DISABLED_LEARNED_RULES = {"draft memo": "Admin/Drafts"}
    settings.flush()

    # Scoped lookup merges global fallback with folder overrides without polluting sibling folders
    assert dl_scope.KEYWORD_RULES == {
        "global_note": "Notes/Global",
        "invoice": "Finances/Invoices",
    }
    code_scope = settings.for_folder(str(code_dir))
    assert "invoice" not in code_scope.KEYWORD_RULES
    assert code_scope.LEARNED_RULES == {}
    assert code_scope.KEYWORD_RULES == {"global_note": "Notes/Global"}

    # Guardrail 1: Zero hidden config files written inside user-sorted folders
    assert list(dl_dir.iterdir()) == []
    assert list(code_dir.iterdir()) == []
    assert settings_file.is_file()


def test_folder_scoped_templates_and_learned_rules_do_not_leak_across_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Requirements 3 & 4 / Acceptance Criterion 1: Downloads starter packs & learned rules never misroute code_project."""
    app_dir = tmp_path / "autosorter_app"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    clear_connection_cache(only_current_and_inactive=False)

    downloads_dir = tmp_path / "Downloads"
    code_dir = tmp_path / "code_project"
    downloads_dir.mkdir()
    code_dir.mkdir()

    (downloads_dir / "vendor_invoice_2026.txt").write_text(
        "Invoice #4012 amount due", encoding="utf-8"
    )
    (downloads_dir / "q1-board-deck.txt").write_text(
        "Quarterly summary", encoding="utf-8"
    )

    (code_dir / "invoice_parser_module.py").write_text(
        "def parse_invoice_line(raw: str) -> dict:\n    return {'utility': raw}\n",
        encoding="utf-8",
    )
    (code_dir / "q1_board_deck.py").write_text(
        "def compute_report() -> int:\n    return 42\n",
        encoding="utf-8",
    )
    (code_dir / "invoice_architecture_notes.md").write_text(
        "# Parser Architecture\nHandles raw billing CSV streams.\n",
        encoding="utf-8",
    )

    settings = AppSettings(filepath=str(app_dir / "settings.json"))
    settings.AI_CONSENT_GRANTED = False

    # Apply finance & admin starter packs AND a learned reassignment strictly to ~/Downloads
    apply_starter_templates(
        settings,
        ["financial_tax", "personal_admin"],
        folder=downloads_dir,
    )
    dl_only = settings.for_folder(str(downloads_dir), fallback_to_global=False)
    dl_only.LEARNED_RULES = {"q1 board deck": "Leadership/Decks"}
    settings.flush()

    assert settings.KEYWORD_RULES == {}
    assert settings.LEARNED_RULES == {}

    # 1. SortPipeline.plan routes ~/Downloads using its finance/learned rules
    session_dl = AppSession(settings, base_dir=str(downloads_dir))
    try:
        dl_files = get_files_recursively(str(downloads_dir), settings=settings)
        dl_plan = SortPipeline(session_dl).plan(dl_files)
        dl_targets = _flatten_plan_targets(dl_plan)
        assert dl_targets["vendor_invoice_2026.txt"] == "Financial/Invoices"
        assert dl_targets["q1-board-deck.txt"] == "Leadership/Decks"
    finally:
        session_dl.close()

    # 2. SortPipeline.plan & MetadataPass.run on ~/code_project have ZERO finance/admin/learned matches!
    mock_db = MagicMock()
    mock_db.get_all_documents.return_value = []
    mock_db.get_all_document_ratings.return_value = {}
    meta_pass = MetadataPass()
    code_bypassed = meta_pass.run(
        str(code_dir),
        [
            "invoice_parser_module.py",
            "q1_board_deck.py",
            "invoice_architecture_notes.md",
        ],
        settings,
        mock_db,
    )
    assert "invoice_architecture_notes.md" not in code_bypassed
    upserted_rows = (
        mock_db.upsert_documents.call_args[0][0]
        if mock_db.upsert_documents.called
        else []
    )
    assert all(row[3] != "[STATUS:BYPASSED]" for row in upserted_rows)

    session_code = AppSession(settings, base_dir=str(code_dir))
    try:
        code_files = get_files_recursively(str(code_dir), settings=settings)
        code_plan = SortPipeline(session_code).plan(code_files)
        code_targets = _flatten_plan_targets(code_plan)
        for routed_folder in code_targets.values():
            assert not routed_folder.startswith("Financial/")
            assert not routed_folder.startswith("Leadership/")
            assert not routed_folder.startswith("Personal/")
    finally:
        session_code.close()

    # 3. Verify TUI DirectorySelectModal + action_select_dir switches active rule scope cleanly
    async def _verify_tui_switch() -> None:
        app = AutoSorterTUI(
            settings=settings,
            base_dir=str(downloads_dir),
            skip_wizard=True,
        )
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            assert "invoice" in app.get_active_settings().KEYWORD_RULES

            dir_modal = DirectorySelectModal(str(downloads_dir), settings=settings)
            assert (
                normalize_folder_scope_key(str(downloads_dir))
                in dir_modal._configured_scopes
            )

            # Simulate switching directory to ~/code_project via DirectorySelectModal callback
            with patch.object(app, "action_scan_directory"):
                app.push_screen(dir_modal)
                await pilot.pause(0.05)
                # Switch active directory directly as action_select_dir's callback does
                app.base_dir = os.path.abspath(str(code_dir))
                app.pop_screen()
                await pilot.pause(0.05)

            assert "invoice" not in app.get_active_settings().KEYWORD_RULES
            assert app.get_active_settings().LEARNED_RULES == {}

    asyncio.run(_verify_tui_switch())
    clear_connection_cache(only_current_and_inactive=False)


def test_all_ten_cli_subcommands_and_tui_accept_profile_and_settings_file_sharing_store_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Requirements 5, 6, 7 / Acceptance Criterion 2: All 10 subcommands + --tui accept --profile & --settings-file and share store.db/model/."""
    app_dir = tmp_path / "shared_autosorter"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    clear_connection_cache(only_current_and_inactive=False)

    parser = build_parser()

    # Verify argparse accepts --profile and --settings-file across all 10 subcommands and --tui
    sample_invocations = [
        ["--tui", "--profile", "work"],
        ["--tui", "--settings-file", str(tmp_path / "custom.json")],
        ["sort", str(tmp_path), "--profile", "work", "--dry-run"],
        ["scan", str(tmp_path), "--profile", "work"],
        ["config", "--show", "--profile", "work"],
        ["daemon", "status", str(tmp_path), "--profile", "work"],
        ["undo", "--list", "--profile", "work"],
        ["drop", str(tmp_path), "--profile", "work", "--dry-run"],
        ["ledger", "status", "--profile", "work"],
        ["model", "status", "--profile", "work"],
        ["quarantine", "list", "--profile", "work"],
        ["rules", "list", "--profile", "work"],
        ["--profile", "work", "sort", str(tmp_path), "--dry-run"],
        ["--settings-file", str(tmp_path / "custom.json"), "rules", "list"],
    ]
    for argv in sample_invocations:
        parsed = parser.parse_args(argv)
        assert getattr(parsed, "profile", None) == "work" or getattr(
            parsed, "settings_file", None
        ) == str(tmp_path / "custom.json")

    # Mutual exclusion error returns exit code 2
    code_err, out_err, _ = run_cli(
        [
            "--profile",
            "work",
            "config",
            "--settings-file",
            str(tmp_path / "other.json"),
            "--json",
        ],
        env={"AUTOSORTER_APP_DIR": str(app_dir)},
    )
    assert code_err == 2
    assert (
        "both --settings-file and --profile" in json.loads(out_err)["message"].lower()
    )

    # Configure "work" profile rules via `rules add --profile work`
    env = {"AUTOSORTER_APP_DIR": str(app_dir)}
    code_add, _, stderr_add = run_cli(
        [
            "rules",
            "add",
            "sprint_notes",
            "Work/Engineering",
            "--profile",
            "work",
            "--json",
        ],
        env=env,
    )
    assert code_add == 0, stderr_add
    expected_profile_path = resolve_profile_settings_path("work", app_dir=app_dir)
    assert expected_profile_path.is_file()
    # Default settings.json remains untouched by profile edits
    assert not (app_dir / "settings.json").exists()

    # Sort a workspace with `--profile work`
    ws = tmp_path / "inbox"
    ws.mkdir()
    doc_file = ws / "sprint_notes_q4.txt"
    doc_file.write_text("Sprint retrospective action items", encoding="utf-8")

    code_sort, out_sort, stderr_sort = run_cli(
        [
            "sort",
            str(ws),
            "--profile",
            "work",
            "--decline-ai-consent",
            "--json",
        ],
        env=env,
    )
    assert code_sort == 0, stderr_sort
    sort_payload = json.loads(out_sort)
    assert sort_payload["status"] == "success"
    moved_file = ws / "Work" / "Engineering" / "sprint_notes_q4.txt"
    assert moved_file.is_file()
    assert not doc_file.exists()

    # Verify get_app_dir(), shared SQLite store, and model/ stayed anchored to AUTOSORTER_APP_DIR!
    assert get_app_dir() == app_dir.resolve()
    assert Path(resolve_default_model_dir()).resolve() == (app_dir / "model").resolve()
    lease = acquire_services()
    try:
        assert Path(lease.db.db_path).resolve() == (app_dir / STORE_FILENAME).resolve()
    finally:
        lease.close()

    # Undo the move from a DIFFERENT `--settings-file` — shared MoveLedger rolls it back!
    alt_settings = tmp_path / "alt_settings.json"
    code_undo, out_undo, stderr_undo = run_cli(
        [
            "undo",
            "--latest",
            "--settings-file",
            str(alt_settings),
            "--json",
        ],
        env=env,
    )
    assert code_undo == 0, stderr_undo
    assert json.loads(out_undo)["status"] == "success"
    assert doc_file.is_file()
    assert not moved_file.exists()

    # Also verify `model status` and `ledger status` succeed with `--profile work`
    code_model, out_model, _ = run_cli(
        ["model", "status", "--profile", "work", "--json"], env=env
    )
    assert code_model == 0
    assert (
        Path(json.loads(out_model)["model_dir"]).resolve()
        == (app_dir / "model").resolve()
    )

    code_ledger, out_ledger, _ = run_cli(
        ["ledger", "status", "--profile", "work", "--json"], env=env
    )
    assert code_ledger == 0
    assert json.loads(out_ledger)["status"] == "success"

    clear_connection_cache(only_current_and_inactive=False)


def test_sortify_json_v2_lossless_roundtrip_preserves_all_rule_tables_and_states(
    tmp_path: Path,
):
    """Requirements 8 & 9 / Acceptance Criterion 3: sortify_json v2 round-trip preserves active, disabled, learned, extension, and folder scopes without --enable."""
    source_settings_path = tmp_path / "source_settings.json"
    scoped_dir = tmp_path / "ScopedDownloads"
    scoped_dir.mkdir()
    norm_scoped_dir = normalize_folder_scope_key(str(scoped_dir))

    src = AppSettings(filepath=str(source_settings_path))
    src.KEYWORD_RULES = {"invoice": "Finances/Invoices"}
    src.DISABLED_KEYWORD_RULES = {"old_receipt": "Finances/Archive"}
    src.LEARNED_RULES = {"q1 tax report": "Finances/Taxes"}
    src.DISABLED_LEARNED_RULES = {"paused client brief": "Projects/Paused"}
    src.EXTENSION_CATEGORIES = {"Creative/Design": [".psd", ".ai", ".fig"]}
    src.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.dmg",
            "target_path": "Installers",
            "priority": 88,
            "halting": True,
            "enabled": True,
        },
        {
            "type": "keyword",
            "expression": "draft_contract",
            "target_path": "Legal/Drafts",
            "priority": 72,
            "halting": True,
            "enabled": False,
        },
    ]
    folder_view = src.for_folder(norm_scoped_dir, fallback_to_global=False)
    folder_view.KEYWORD_RULES = {"boarding pass": "Travel/Flights"}
    folder_view.DISABLED_KEYWORD_RULES = {"hotel_voucher": "Travel/Hotels"}
    folder_view.LEARNED_RULES = {"bank_stmt_2026": "Finances/Banking"}
    folder_view.DISABLED_LEARNED_RULES = {"temp_scan": "Scans/Temp"}
    folder_view.EXTENSION_CATEGORIES = {"Archives/DiskImages": [".iso"]}
    folder_view.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.ics",
            "target_path": "Calendar",
            "priority": 80,
            "halting": True,
            "enabled": True,
        }
    ]
    src.flush()

    backup_file = tmp_path / "backup_v2.json"
    code_exp, _, stderr_exp = run_cli(
        [
            "rules",
            "export",
            str(backup_file),
            "--settings-file",
            str(source_settings_path),
            "--json",
        ]
    )
    assert code_exp == 0, stderr_exp
    raw_backup = json.loads(backup_file.read_text(encoding="utf-8"))
    assert raw_backup["format"] == "sortify"
    assert raw_backup["version"] == 2
    assert norm_scoped_dir in raw_backup["FOLDER_RULE_SCOPES"]

    # Re-import into brand-new settings WITHOUT `--enable`
    restored_path = tmp_path / "restored_settings.json"
    restored_init = AppSettings(filepath=str(restored_path))
    restored_init.EXTENSION_CATEGORIES = {}
    restored_init.flush()

    code_imp, out_imp, stderr_imp = run_cli(
        [
            "rules",
            "import",
            str(backup_file),
            "--settings-file",
            str(restored_path),
            "--conflict-strategy",
            "overwrite",
            "--json",
        ]
    )
    assert code_imp == 0, stderr_imp
    imp_payload = json.loads(out_imp)
    assert imp_payload["preserve_states"] is True
    assert imp_payload["import_disabled"] is False

    restored = AppSettings(filepath=str(restored_path))
    assert restored.KEYWORD_RULES == {"invoice": "Finances/Invoices"}
    assert restored.DISABLED_KEYWORD_RULES == {"old_receipt": "Finances/Archive"}
    assert restored.LEARNED_RULES == {"q1 tax report": "Finances/Taxes"}
    assert restored.DISABLED_LEARNED_RULES == {"paused client brief": "Projects/Paused"}
    assert restored.EXTENSION_CATEGORIES["Creative/Design"] == [".psd", ".ai", ".fig"]

    dmg_pol = next(p for p in restored.POLICIES if p["expression"] == "*.dmg")
    draft_pol = next(
        p for p in restored.POLICIES if p["expression"] == "draft_contract"
    )
    assert dmg_pol["enabled"] is True
    assert draft_pol["enabled"] is False

    restored_scope = restored.for_folder(norm_scoped_dir, fallback_to_global=False)
    assert restored_scope.KEYWORD_RULES == {"boarding pass": "Travel/Flights"}
    assert restored_scope.DISABLED_KEYWORD_RULES == {"hotel_voucher": "Travel/Hotels"}
    assert restored_scope.LEARNED_RULES == {"bank_stmt_2026": "Finances/Banking"}
    assert restored_scope.DISABLED_LEARNED_RULES == {"temp_scan": "Scans/Temp"}
    assert restored_scope.EXTENSION_CATEGORIES == {"Archives/DiskImages": [".iso"]}
    assert len(restored_scope.POLICIES) == 1
    assert restored_scope.POLICIES[0]["enabled"] is True

    # Also verify TUI RuleImportModal auto-unchecks #chk-import-disabled for Sortify JSON backups
    async def _verify_import_modal_preserves_state() -> None:
        tui_settings_path = tmp_path / "tui_restored.json"
        tui_settings = AppSettings(filepath=str(tui_settings_path))
        tui_settings.AI_CONSENT_GRANTED = False
        app = AutoSorterTUI(
            settings=tui_settings, base_dir=str(tmp_path), skip_wizard=True
        )
        async with app.run_test() as pilot:
            modal = RuleImportModal(tui_settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            modal.query_one("#input-rule-file-path", Input).value = str(backup_file)
            modal.action_parse_file()
            await pilot.pause(0.05)

            chk = modal.query_one("#chk-import-disabled", Checkbox)
            assert chk.value is False

            modal.action_merge_rules()
            await pilot.pause(0.05)

            assert tui_settings.KEYWORD_RULES == {"invoice": "Finances/Invoices"}
            assert tui_settings.DISABLED_KEYWORD_RULES == {
                "old_receipt": "Finances/Archive"
            }
            assert tui_settings.LEARNED_RULES == {"q1 tax report": "Finances/Taxes"}
            assert tui_settings.DISABLED_LEARNED_RULES == {
                "paused client brief": "Projects/Paused"
            }

    asyncio.run(_verify_import_modal_preserves_state())


def test_hazel_and_dropit_exports_handle_trash_duplicate_and_disabled_policies(
    tmp_path: Path,
):
    """Requirement 10 / Acceptance Criterion 4: trash & duplicate policies export as NSRuleActionTrash / Delete, and DropIt omits disabled policies."""
    settings = AppSettings(filepath=str(tmp_path / "export_settings.json"))
    settings.KEYWORD_RULES = {"invoice": "Finances/Invoices"}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "*.tmp_bak",
            "target_path": "",
            "action": "trash",
            "priority": 95,
            "enabled": True,
        },
        {
            "type": "duplicate",
            "expression": "*",
            "target_path": "",
            "action": "trash",
            "priority": 90,
            "enabled": True,
        },
        {
            "type": "keyword",
            "expression": "disabled_secret_rule",
            "target_path": "Vault/Disabled",
            "priority": 85,
            "enabled": False,
        },
    ]

    hazel_out = export_active_rules(settings, "hazel", tmp_path / "out.hazelrules")
    with open(hazel_out, "rb") as fh:
        plist_data = plistlib.load(fh)

    rules_by_name = {r["NSRuleName"]: r for r in plist_data["Rules"]}
    assert rules_by_name["Policy 3: disabled_secret_rule"]["NSRuleIsEnabled"] is False

    trash_rule = rules_by_name["Policy 1: *.tmp_bak"]
    assert trash_rule["NSRuleActions"][0]["NSRuleActionType"] == "NSRuleActionTrash"
    assert trash_rule["NSRuleActions"][0]["DestinationPath"] == ""

    dup_rule = rules_by_name["Policy 2: *"]
    assert dup_rule["NSRuleActions"][0]["NSRuleActionType"] == "NSRuleActionTrash"
    assert dup_rule["NSRuleActions"][0]["DestinationPath"] == ""
    assert dup_rule["NSRuleConditions"][0]["target"] == "Duplicate"

    dropit_out = export_active_rules(settings, "dropit_ini", tmp_path / "out.ini")
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(dropit_out, encoding="utf-8")

    sections = [cp[s] for s in cp.sections() if s.startswith("Association_")]
    rules_map = {sec["Rules"]: sec for sec in sections}

    # Disabled policy is omitted in DropIt INI
    assert "*disabled_secret_rule*" not in rules_map

    # Trash and Duplicate policies map to Action = Delete with empty Destination
    assert rules_map["*.tmp_bak"]["Action"] == "Delete"
    assert rules_map["*.tmp_bak"]["Destination"] == ""
    assert rules_map["*"]["Action"] == "Delete"
    assert rules_map["*"]["Destination"] == ""
    assert rules_map["invoice"]["Action"] == "Move"


def test_learned_rule_toggle_preserves_kind_and_separator_normalization_in_tui_and_cli(
    tmp_path: Path,
):
    """Requirement 11 / Acceptance Criterion 5: Disabling and re-enabling LEARNED_RULES retains learned kind and matches hyphenated/underscored filenames."""
    assert matches_learned_rule_pattern("q1 tax report", "q1-tax-report_final")
    assert matches_learned_rule_pattern("q1-tax-report", "q1_tax_report_2026")

    settings_path = tmp_path / "learned_settings.json"
    settings = AppSettings(filepath=str(settings_path))
    settings.AI_CONSENT_GRANTED = False
    settings.LEARNED_RULES = {"q1 tax report": "Finances/Taxes"}
    settings.flush()

    ws = tmp_path / "learned_ws"
    ws.mkdir()
    (ws / "q1-tax-report.pdf").write_bytes(b"%PDF-1.4 sample")
    (ws / "q1_tax_report_v2.txt").write_text("Tax figures", encoding="utf-8")

    # 1. TUI ActiveRuleManagerModal toggle: Active -> Disabled -> Active
    async def _verify_tui_toggle() -> None:
        app = AutoSorterTUI(settings=settings, base_dir=str(ws), skip_wizard=True)
        async with app.run_test() as pilot:
            modal = ActiveRuleManagerModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            table = modal.query_one("#rules-table", DataTable)
            assert table.row_count == 1
            assert modal._rules_data[0]["kind"] == "learned"
            assert modal._rules_data[0]["enabled"] is True

            # Disable via toggle
            modal.action_toggle_rule()
            await pilot.pause(0.05)
            assert settings.LEARNED_RULES == {}
            assert settings.DISABLED_LEARNED_RULES == {
                "q1 tax report": "Finances/Taxes"
            }
            assert settings.KEYWORD_RULES == {}
            assert settings.DISABLED_KEYWORD_RULES == {}
            assert modal._rules_data[0]["kind"] == "learned"
            assert modal._rules_data[0]["enabled"] is False

            # While disabled, MetadataPass does not bypass supported .txt files via learned rule
            assert (
                MetadataPass().run(
                    str(ws),
                    ["q1_tax_report_v2.txt"],
                    settings,
                    None,
                )
                == []
            )

            # Re-enable via toggle
            modal.action_toggle_rule()
            await pilot.pause(0.05)
            assert settings.LEARNED_RULES == {"q1 tax report": "Finances/Taxes"}
            assert settings.DISABLED_LEARNED_RULES == {}
            assert settings.KEYWORD_RULES == {}
            assert modal._rules_data[0]["kind"] == "learned"
            assert modal._rules_data[0]["enabled"] is True

    asyncio.run(_verify_tui_toggle())
    settings.flush()

    # 2. CLI `rules disable` & `rules enable` preserve `learned` kind and separator matching
    code_dis, out_dis, stderr_dis = run_cli(
        [
            "rules",
            "disable",
            "q1-tax-report",
            "--settings-file",
            str(settings_path),
            "--json",
        ]
    )
    assert code_dis == 0, stderr_dis
    dis_payload = json.loads(out_dis)
    assert dis_payload["status"] == "disabled"
    assert dis_payload["updated"][0]["kind"] == "learned"

    after_dis = AppSettings(filepath=str(settings_path))
    assert after_dis.LEARNED_RULES == {}
    assert after_dis.DISABLED_LEARNED_RULES == {"q1 tax report": "Finances/Taxes"}
    assert after_dis.KEYWORD_RULES == {}

    code_en, out_en, stderr_en = run_cli(
        [
            "rules",
            "enable",
            "q1_tax_report",
            "--settings-file",
            str(settings_path),
            "--json",
        ]
    )
    assert code_en == 0, stderr_en
    en_payload = json.loads(out_en)
    assert en_payload["status"] == "enabled"
    assert en_payload["updated"][0]["kind"] == "learned"

    after_en = AppSettings(filepath=str(settings_path))
    assert after_en.LEARNED_RULES == {"q1 tax report": "Finances/Taxes"}
    assert after_en.DISABLED_LEARNED_RULES == {}

    # Verify both MetadataPass and IncrementalAnalyzer match hyphenated and underscored filenames!
    bypassed = MetadataPass().run(
        str(ws),
        ["q1-tax-report.pdf", "q1_tax_report_v2.txt"],
        after_en,
        None,
    )
    assert set(bypassed) == {"q1-tax-report.pdf", "q1_tax_report_v2.txt"}

    session_ws = AppSession(after_en, base_dir=str(ws))
    try:
        ws_files = get_files_recursively(str(ws), settings=after_en)
        plan = SortPipeline(session_ws).plan(ws_files)
        planned_targets = _flatten_plan_targets(plan)
        assert planned_targets["q1-tax-report.pdf"] == "Finances/Taxes"
        assert planned_targets["q1_tax_report_v2.txt"] == "Finances/Taxes"
    finally:
        session_ws.close()


def test_rules_cli_crud_subcommands_in_text_and_json_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Requirement 12 / Acceptance Criterion 6: rules list, add, remove, enable, disable work in text and --json modes with --folder, --profile, and --settings-file."""
    app_dir = tmp_path / "crud_app"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    env = {"AUTOSORTER_APP_DIR": str(app_dir)}
    folder_a = tmp_path / "FolderA"
    folder_a.mkdir()

    # 1. Add keyword, learned, extension, and policy rules (text + JSON modes)
    c1, out1, err1 = run_cli(
        [
            "rules",
            "add",
            "payslip",
            "Finances/Payroll",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
        ],
        env=env,
    )
    assert c1 == 0, err1
    assert "Added [active] keyword rule 'payslip' -> 'Finances/Payroll'" in out1

    c2, out2, err2 = run_cli(
        [
            "rules",
            "add",
            "quarterly board deck",
            "Leadership/Decks",
            "--kind",
            "learned",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c2 == 0, err2
    assert json.loads(out2)["rule"]["kind"] == "learned"

    c3, out3, err3 = run_cli(
        [
            "rules",
            "add",
            ".sketch,.fig",
            "Design/UI",
            "--kind",
            "extension",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c3 == 0, err3
    assert json.loads(out3)["rule"]["extensions"] == [".sketch", ".fig"]

    c4, out4, err4 = run_cli(
        [
            "rules",
            "add",
            "*.log",
            "Logs/Archive",
            "--kind",
            "policy",
            "--type",
            "glob",
            "--priority",
            "92",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c4 == 0, err4
    assert json.loads(out4)["rule"]["priority"] == 92

    # 2. List rules in JSON and text modes
    c_list_j, out_list_j, err_list_j = run_cli(
        [
            "rules",
            "list",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c_list_j == 0, err_list_j
    listed = json.loads(out_list_j)
    assert listed["count"] == 4
    assert {r["kind"] for r in listed["rules"]} == {
        "keyword",
        "learned",
        "extension",
        "policy",
    }

    c_list_t, out_list_t, _ = run_cli(
        ["rules", "list", "--folder", str(folder_a), "--profile", "ops"],
        env=env,
    )
    assert c_list_t == 0
    assert "payslip -> Finances/Payroll" in out_list_t
    assert "quarterly board deck -> Leadership/Decks" in out_list_t

    # 3. Disable and re-enable keyword & policy rules (text + JSON)
    c_dis, out_dis, _ = run_cli(
        [
            "rules",
            "disable",
            "payslip",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
        ],
        env=env,
    )
    assert c_dis == 0
    assert "Disabled 1 rule(s)" in out_dis

    c_list_dis, out_list_dis, _ = run_cli(
        [
            "rules",
            "list",
            "--status",
            "disabled",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c_list_dis == 0
    assert json.loads(out_list_dis)["count"] == 1
    assert json.loads(out_list_dis)["rules"][0]["pattern"] == "payslip"

    c_en, out_en, _ = run_cli(
        [
            "rules",
            "enable",
            "payslip",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c_en == 0
    assert json.loads(out_en)["status"] == "enabled"

    # 4. Remove rules in JSON and text modes
    c_rem_j, out_rem_j, _ = run_cli(
        [
            "rules",
            "remove",
            "*.log",
            "--kind",
            "policy",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
            "--json",
        ],
        env=env,
    )
    assert c_rem_j == 0
    assert json.loads(out_rem_j)["removed_count"] == 1

    c_rem_t, out_rem_t, _ = run_cli(
        [
            "rules",
            "remove",
            "payslip",
            "--folder",
            str(folder_a),
            "--profile",
            "ops",
        ],
        env=env,
    )
    assert c_rem_t == 0
    assert "Removed 1 rule(s)" in out_rem_t
