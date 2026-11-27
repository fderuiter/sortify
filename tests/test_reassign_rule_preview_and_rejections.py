"""Acceptance and regression tests for ReassignModal rule preview, priority-90 learned rules, folder-rename rule remapping, and negative-rating exclusion."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Checkbox, DataTable, Input, Label, Tree

from app.config import AppSettings
from app.core.db_conn import clear_connection_cache
from app.core.metadata import (
    MetadataPass,
    derive_learned_pattern_for_files,
    evaluate_static_file_routing,
    matches_learned_rule_pattern,
    tokenize_filename_for_rule,
)
from app.core.mover import execute_moves
from app.core.policy_engine import compute_active_rule_masking, is_masked_by
from app.core.rule_scopes import resolve_scoped_settings
from app.core.session import AppSession
from app.ui.modals.edit import ReassignModal, RenameModal
from app.ui.modals.rules import ActiveRuleManagerModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture(autouse=True)
def _clean_env_and_sessions(monkeypatch, tmp_path):
    """Isolate AUTOSORTER_APP_DIR and suppress abandoned-session prompts."""
    app_dir = tmp_path / "_autosorter_app"
    app_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )
    yield
    clear_connection_cache(only_current_and_inactive=False)


def _run_cli(
    args: list[str], env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run app/main.py in-process and return (returncode, stdout, stderr)."""
    old_env = os.environ.copy()
    repo_root = str(Path(__file__).parent.parent.resolve())
    os.environ["PYTHONPATH"] = repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")
    if env:
        os.environ.update(env)

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    mock_stdin.encoding = "utf-8"

    code = 0
    with (
        patch("sys.argv", ["smart-autosorter"] + args),
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


def _extract_planned_destinations(plan: dict | object) -> dict[str, str]:
    """Return {source_rel_or_name: folder_path} across a nested sorting plan."""
    raw = plan.plan if hasattr(plan, "plan") and isinstance(plan.plan, dict) else plan
    out: dict[str, str] = {}

    def _walk(node: dict, prefix: str = "") -> None:
        for key, val in node.items():
            if str(key).startswith("__"):
                continue
            v = val.plan if hasattr(val, "plan") and isinstance(val.plan, dict) else val
            if not isinstance(v, dict):
                continue
            if v.get("__type__") == "file":
                src = str(v.get("source") or key).replace("\\", "/")
                out[src] = prefix.strip("/")
            else:
                next_prefix = f"{prefix}/{key}" if prefix else str(key)
                _walk(v, next_prefix)

    if isinstance(raw, dict):
        _walk(raw)
    return out


# ---------------------------------------------------------------------------
# Req 1 & AC 1: Token generalization + ReassignModal shared token & live count
# ---------------------------------------------------------------------------


def test_filename_token_generalization_strips_dates_counters_and_stopwords():
    """Req 1: Strip dates, counters, version/quarter tags, hashes, and generic stopwords."""
    toks, stops = tokenize_filename_for_rule("invoice_2026_acme.pdf")
    assert toks == ["invoice", "acme"]
    assert stops == []

    pat_single, stops_single, generic_single = derive_learned_pattern_for_files(
        ["invoice_2026_acme.pdf"]
    )
    assert pat_single == "invoice acme"
    assert stops_single == []
    assert generic_single is False
    assert matches_learned_rule_pattern("invoice acme", "invoice_2026_acme.pdf")
    assert matches_learned_rule_pattern("invoice acme", "acme-2027-invoice-v2.pdf")

    pat_multi, _, generic_multi = derive_learned_pattern_for_files(
        ["invoice_2026_acme.pdf", "invoice_2026_beta.pdf"]
    )
    assert pat_multi == "invoice"
    assert generic_multi is False

    pat_scan, stops_scan, generic_scan = derive_learned_pattern_for_files(
        ["scan_001.pdf"]
    )
    assert pat_scan == ""
    assert stops_scan == ["scan"]
    assert generic_scan is True

    pat_misc, stops_misc, generic_misc = derive_learned_pattern_for_files(
        ["Screenshot_2026-04-12_final_v3_a1b2c3d4e5.png"]
    )
    assert pat_misc == ""
    assert "screenshot" in stops_misc
    assert "final" in stops_misc
    assert generic_misc is True


@pytest.mark.anyio
async def test_ac1_reassign_multiple_invoices_proposes_shared_token_and_live_count(
    tmp_path,
):
    """AC 1: Reassigning invoice_2026_acme.pdf and invoice_2026_beta.pdf proposes 'invoice' with live match count."""
    ws = tmp_path / "ac1_ws"
    ws.mkdir()
    f_acme = ws / "invoice_2026_acme.pdf"
    f_beta = ws / "invoice_2026_beta.pdf"
    f_gamma = ws / "invoice_2025_gamma.pdf"
    f_other = ws / "payroll_summary.pdf"
    for fp in (f_acme, f_beta, f_gamma, f_other):
        fp.write_text("stub pdf text", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.KEYWORD_RULES = {"invoice": "Financial/Invoices"}
    settings.LEARNED_RULES = {}

    app = AutoSorterTUI(settings=settings, base_dir=str(ws))
    app.plan = {
        "Financial": {
            "Invoices": {
                "invoice_2026_acme.pdf": {
                    "__type__": "file",
                    "filepath": str(f_acme),
                    "source": "invoice_2026_acme.pdf",
                    "target_filename": "invoice_2026_acme.pdf",
                    "is_checked": True,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "Financial/Invoices",
                },
                "invoice_2026_beta.pdf": {
                    "__type__": "file",
                    "filepath": str(f_beta),
                    "source": "invoice_2026_beta.pdf",
                    "target_filename": "invoice_2026_beta.pdf",
                    "is_checked": True,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "Financial/Invoices",
                },
                "invoice_2025_gamma.pdf": {
                    "__type__": "file",
                    "filepath": str(f_gamma),
                    "source": "invoice_2025_gamma.pdf",
                    "target_filename": "invoice_2025_gamma.pdf",
                    "is_checked": False,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "Financial/Invoices",
                },
            }
        },
        "HR": {
            "payroll_summary.pdf": {
                "__type__": "file",
                "filepath": str(f_other),
                "source": "payroll_summary.pdf",
                "target_filename": "payroll_summary.pdf",
                "is_checked": False,
                "is_excluded": False,
                "is_locked": False,
                "folder": "HR",
            }
        },
    }

    announcements: list[str] = []
    orig_announce = app.announce

    def _record_announce(
        message: str, priority: str = "polite", help_url: str | None = None
    ) -> str:
        announcements.append(str(message))
        return orig_announce(message, priority=priority, help_url=help_url)

    app.announce = _record_announce

    async with app.run_test(size=(120, 38)) as pilot:
        await pilot.pause()
        app.render_tree()
        await pilot.pause()

        app.action_reassign_selected()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ReassignModal)

        chk = modal.query_one("#chk-learn-rule", Checkbox)
        pat_input = modal.query_one("#input-learned-pattern", Input)
        matches_lbl = modal.query_one("#lbl-rule-matches", Label)

        assert chk.value is True
        assert pat_input.value == "invoice"
        match_text = str(matches_lbl.render())
        assert "Matches 3 file(s) in current plan" in match_text
        assert "Overrides 'invoice' keyword rule" in match_text

        # Editing the pattern updates the live match count dynamically
        pat_input.value = "invoice acme"
        await pilot.pause()
        assert "Matches 1 file(s) in current plan" in str(matches_lbl.render())

        # Restore shared token 'invoice' and confirm reassignment
        pat_input.value = "invoice"
        await pilot.pause()
        folder_input = modal.query_one("#input-target-folder", Input)
        folder_input.value = "Clients/Billing"
        modal.action_confirm()
        await pilot.pause()

        scoped = app.get_active_settings()
        assert scoped.LEARNED_RULES.get("invoice") == "Clients/Billing"
        assert any("saved filename rule 'invoice'" in msg for msg in announcements)


# ---------------------------------------------------------------------------
# Req 3 & AC 2: Generic stem defaults rule off + external path disables rule
# + compact 80x24 and narrow <80 viewport responsiveness
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ac2_reassign_scan_001_defaults_rule_off_and_announces_only_move(
    tmp_path,
):
    """AC 2: Reassigning scan_001.pdf leaves rule learning unchecked and announces only the move."""
    ws = tmp_path / "ac2_ws"
    ws.mkdir()
    f_scan = ws / "scan_001.pdf"
    f_scan.write_text("Scanned receipt image", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.LEARNED_RULES = {}

    app = AutoSorterTUI(settings=settings, base_dir=str(ws))
    app.plan = {
        "Uncategorized": {
            "scan_001.pdf": {
                "__type__": "file",
                "filepath": str(f_scan),
                "source": "scan_001.pdf",
                "target_filename": "scan_001.pdf",
                "is_checked": True,
                "is_excluded": False,
                "is_locked": False,
                "folder": "Uncategorized",
            }
        }
    }

    announcements: list[str] = []
    orig_announce = app.announce

    def _record_announce(
        message: str, priority: str = "polite", help_url: str | None = None
    ) -> str:
        announcements.append(str(message))
        return orig_announce(message, priority=priority, help_url=help_url)

    app.announce = _record_announce

    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        app.render_tree()
        await pilot.pause()

        app.action_reassign_selected()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ReassignModal)

        chk = modal.query_one("#chk-learn-rule", Checkbox)
        pat_input = modal.query_one("#input-learned-pattern", Input)
        tokens_lbl = modal.query_one("#lbl-rule-tokens", Label)
        matches_lbl = modal.query_one("#lbl-rule-matches", Label)

        assert chk.value is False
        assert pat_input.value == ""
        assert "generic" in str(tokens_lbl.render()).lower()
        assert "file move only" in str(matches_lbl.render()).lower()

        folder_input = modal.query_one("#input-target-folder", Input)
        folder_input.value = "Receipts"
        modal.action_confirm()
        await pilot.pause()

        scoped = app.get_active_settings()
        assert scoped.LEARNED_RULES == {}
        assert announcements[-1] == "Reassigned 1 file(s) to folder 'Receipts'."
        assert not any("saved filename rule" in msg for msg in announcements)


@pytest.mark.anyio
async def test_reassign_external_path_disables_rule_and_compact_80x24_fits(tmp_path):
    """Req 2 & Req 3: External path disables rule toggle, and ReassignModal fits 80x24 and narrow terminals."""
    ws = tmp_path / "ext_ws"
    ws.mkdir()
    ext_dir = tmp_path / "external_vault"
    ext_dir.mkdir()
    f_inv = ws / "invoice_2026_acme.pdf"
    f_inv.write_text("Acme invoice", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    app = AutoSorterTUI(settings=settings, base_dir=str(ws))
    app.plan = {
        "Uncategorized": {
            "invoice_2026_acme.pdf": {
                "__type__": "file",
                "filepath": str(f_inv),
                "source": "invoice_2026_acme.pdf",
                "target_filename": "invoice_2026_acme.pdf",
                "is_checked": True,
                "is_excluded": False,
                "is_locked": False,
                "folder": "Uncategorized",
            }
        }
    }

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.render_tree()
        await pilot.pause()

        app.action_reassign_selected()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ReassignModal)
        assert modal.has_class("compact")

        btn_confirm = modal.query_one("#btn-confirm")
        assert btn_confirm.region.right <= 80
        assert btn_confirm.region.bottom <= 24

        # Resize below 80 columns to verify narrow responsive class as well
        await pilot.resize_terminal(76, 24)
        await pilot.pause()
        assert modal.has_class("narrow")
        assert modal.has_class("compact")

        chk = modal.query_one("#chk-learn-rule", Checkbox)
        assert chk.value is True
        assert chk.disabled is False

        folder_input = modal.query_one("#input-target-folder", Input)
        folder_input.value = str(ext_dir)
        await pilot.pause()

        assert chk.value is False
        assert chk.disabled is True
        modal.action_confirm()
        await pilot.pause()

        assert app.get_active_settings().LEARNED_RULES == {}


# ---------------------------------------------------------------------------
# Req 4, Req 6, Req 8 & AC 3: Priority-90 LEARNED_RULES ahead of KEYWORD_RULES
# and priority < 90 move policies, filename-only matching, [LEARNED] badge,
# and compliance/JEV quarantine override preservation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ac3_learned_rules_priority_90_filename_only_and_tree_badge(tmp_path):
    """AC 3, Req 4, Req 6, Req 8: LEARNED_RULES beats KEYWORD_RULES and <90 policies on filenames only and renders [LEARNED]."""
    ws = tmp_path / "ac3_ws"
    ws.mkdir()

    f_match = ws / "invoice_2026_acme.txt"
    f_match.write_text("Standard consulting fee schedule.", encoding="utf-8")

    f_body_only = ws / "scan_0099.txt"
    f_body_only.write_text(
        "Body mentions invoice acme consulting contract.", encoding="utf-8"
    )

    f_compliance = ws / "invoice_2026_acme_secret.txt"
    f_compliance.write_text("Top secret customer key material.", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.AUTO_QUARANTINE_RATINGS = []
    settings.LEARNED_RULES = {"invoice acme": "Clients/Acme"}
    settings.KEYWORD_RULES = {"invoice": "Financial/Invoices"}
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "invoice_*.txt",
            "target_path": "Policies/Sub90_Invoices",
            "priority": 85,
            "halting": False,
            "enabled": True,
        },
    ]

    # 1) Static routing & MetadataPass: learned rule wins over priority 85 policy & keyword rule
    static_target, static_action, static_halt = evaluate_static_file_routing(
        str(f_match), settings
    )
    assert static_target == "Clients/Acme"
    assert static_action is False
    assert static_halt is False

    session = AppSession(settings, base_dir=str(ws))
    try:
        bypassed = MetadataPass.run(
            str(ws),
            [f_match.name, f_body_only.name],
            settings,
            session.db,
        )
        assert f_match.name in bypassed
        assert f_body_only.name not in bypassed

        # Now add a compliance quarantine policy (even at priority 50) and verify it overrides LEARNED_RULES
        settings.POLICIES.append(
            {
                "type": "pattern",
                "expression": "*secret*",
                "action": "quarantine",
                "target_path": "_Compliance_Quarantine",
                "priority": 50,
                "halting": False,
                "enabled": True,
            }
        )
        comp_target, comp_action, _ = evaluate_static_file_routing(
            str(f_compliance), settings
        )
        assert comp_target == "_Compliance_Quarantine"
        assert comp_action is True

        # Populate extracted text for f_body_only so analyzer sees body text
        session.db.upsert_document(
            str(ws),
            f_body_only.name,
            "hash_body_only",
            f_body_only.read_text(encoding="utf-8"),
        )
        session.db.upsert_document(
            str(ws),
            f_compliance.name,
            "hash_comp",
            f_compliance.read_text(encoding="utf-8"),
        )

        analyzer = session.analyzer
        sorting_plan = analyzer.generate_sorting_plan(
            str(ws), runtime_settings=settings
        )
        dest_map = _extract_planned_destinations(sorting_plan)

        # Filename match routes to Clients/Acme via LEARNED_RULES (ahead of priority-85 policy & priority-80 keyword)
        assert dest_map.get(f_match.name) == "Clients/Acme"
        # Body-only match MUST NOT match LEARNED_RULES; routes via KEYWORD_RULES ("invoice") instead
        assert dest_map.get(f_body_only.name) == "Financial/Invoices"
        # Compliance quarantine policy wins over LEARNED_RULES
        assert dest_map.get(f_compliance.name) == "_Compliance_Quarantine"

        acme_leaf = sorting_plan.plan["Clients"]["Acme"][f_match.name]
        assert acme_leaf["routed_by"] == "learned"
        assert acme_leaf["match"] == "invoice acme"

        # JevClassifierEngine CRITICAL quarantine also wins ahead of LEARNED_RULES
        settings.AUTO_QUARANTINE_RATINGS = ["CRITICAL"]
        jev_plan = analyzer.generate_sorting_plan(
            str(ws),
            runtime_settings=settings,
            jev_results={
                f_match.name: {
                    "category": "Finance",
                    "confidence": 0.95,
                    "sensitivity_rating": "CRITICAL",
                    "is_classified": True,
                }
            },
        )
        jev_dest = _extract_planned_destinations(jev_plan)
        assert jev_dest.get(f_match.name) == "_Compliance_Quarantine"

        # Verify TreeViewMixin renders [LEARNED] badge for routed_by="learned"
        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        app.plan = sorting_plan.plan
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.pause()
            app.render_tree()
            await pilot.pause()
            tree = app.query_one("#plan-tree", Tree)

            def _collect_labels(node) -> list[str]:
                labels = [str(node.label)]
                for child in node.children:
                    child.expand()
                    labels.extend(_collect_labels(child))
                return labels

            all_labels = "\n".join(_collect_labels(tree.root))
            assert "[LEARNED]" in all_labels
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Req 5: F2 folder rename remaps LEARNED_RULES and DISABLED_LEARNED_RULES
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_req5_f2_folder_rename_remaps_learned_rules_destinations(tmp_path):
    """Req 5: Renaming a folder with F2 remaps matching LEARNED_RULES and nested subfolder targets."""
    ws = tmp_path / "f2_ws"
    ws.mkdir()
    f_doc = ws / "invoice_2026_acme.pdf"
    f_doc.write_text("Acme invoice", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.LEARNED_RULES = {
        "invoice acme": "Clients/Acme",
        "statement acme": "Clients/Acme/2026",
        "other": "Vendors/Beta",
    }
    settings.DISABLED_LEARNED_RULES = {
        "draft acme": "Clients/Acme",
    }
    scoped_view = resolve_scoped_settings(settings, str(ws), fallback_to_global=False)
    scoped_view.LEARNED_RULES = {
        "invoice acme": "Clients/Acme",
        "statement acme": "Clients/Acme/2026",
    }

    app = AutoSorterTUI(settings=settings, base_dir=str(ws))
    app.plan = {
        "Clients": {
            "Acme": {
                "invoice_2026_acme.pdf": {
                    "__type__": "file",
                    "filepath": str(f_doc),
                    "source": "invoice_2026_acme.pdf",
                    "target_filename": "invoice_2026_acme.pdf",
                    "is_checked": True,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "Clients/Acme",
                }
            }
        }
    }

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.render_tree()
        await pilot.pause()
        tree = app.query_one("#plan-tree", Tree)
        tree.focus()
        clients_node = tree.root.children[0]
        clients_node.expand()
        await pilot.pause()
        acme_node = clients_node.children[0]
        tree.select_node(acme_node)
        await pilot.pause()

        await pilot.press("f2")
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, RenameModal)
        name_input = modal.query_one("#input-name", Input)
        name_input.value = "Acme_Holdings"
        modal.action_confirm()
        await pilot.pause()

        assert settings.LEARNED_RULES["invoice acme"] == "Clients/Acme_Holdings"
        assert settings.LEARNED_RULES["statement acme"] == "Clients/Acme_Holdings/2026"
        assert settings.LEARNED_RULES["other"] == "Vendors/Beta"
        assert settings.DISABLED_LEARNED_RULES["draft acme"] == "Clients/Acme_Holdings"

        updated_scope = resolve_scoped_settings(
            settings, str(ws), fallback_to_global=False
        )
        assert updated_scope.LEARNED_RULES["invoice acme"] == "Clients/Acme_Holdings"
        assert (
            updated_scope.LEARNED_RULES["statement acme"]
            == "Clients/Acme_Holdings/2026"
        )


# ---------------------------------------------------------------------------
# Req 7 & AC 4: Priority 90 display and masked_by across CLI and TUI Rule Manager
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ac4_active_rule_manager_and_cli_rules_list_show_priority_90_and_masked_by(
    tmp_path,
):
    """AC 4: ActiveRuleManagerModal and `rules list` both display priority 90 for learned rules and flag shadowed rules."""
    settings_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_path))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "tax",
            "target_path": "Taxes/HighPriority",
            "priority": 95,
            "halting": False,
            "enabled": True,
        },
        {
            "type": "pattern",
            "expression": "invoice_q1",
            "target_path": "Finance/Q1",
            "priority": 85,
            "halting": False,
            "enabled": True,
        },
    ]
    settings.LEARNED_RULES = {
        "invoice": "Clients/Invoices",
        "tax_return": "Taxes/Learned",
    }
    settings.KEYWORD_RULES = {
        "invoice_2026": "Legacy/Invoices",
        "tax_2026": "Legacy/Taxes",
    }
    settings._save()
    settings.flush()

    # Verify core masking helper directly
    assert is_masked_by(
        {"type": "learned", "expression": "invoice", "priority": 90},
        {"type": "pattern", "expression": "invoice_q1", "priority": 85},
    )
    mask_map = compute_active_rule_masking(settings)
    assert mask_map.get(("learned", "tax_return")) == "tax"
    assert mask_map.get(("policy", 1)) == "invoice"
    assert mask_map.get(("keyword", "tax_2026")) == "tax"

    # 1) Verify CLI `smart-autosorter rules list --json` and plain text
    cli_env = {
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
    }
    rc_json, out_json, err_json = _run_cli(
        ["rules", "list", "--settings-file", str(settings_path), "--json"],
        env=cli_env,
    )
    assert rc_json == 0, err_json
    payload = json.loads(out_json)
    rows_by_kind_pat = {(r["kind"], r["pattern"]): r for r in payload["rules"]}

    assert rows_by_kind_pat[("learned", "invoice")]["priority"] == 90
    assert rows_by_kind_pat[("learned", "invoice")].get("masked_by") is None

    assert rows_by_kind_pat[("learned", "tax_return")]["priority"] == 90
    assert rows_by_kind_pat[("learned", "tax_return")]["masked_by"] == "tax"

    assert rows_by_kind_pat[("policy", "invoice_q1")]["priority"] == 85
    assert rows_by_kind_pat[("policy", "invoice_q1")]["masked_by"] == "invoice"

    assert rows_by_kind_pat[("keyword", "invoice_2026")]["priority"] == 80
    assert rows_by_kind_pat[("keyword", "invoice_2026")].get("masked_by") is None
    assert rows_by_kind_pat[("keyword", "tax_2026")]["priority"] == 80
    assert rows_by_kind_pat[("keyword", "tax_2026")]["masked_by"] == "tax"

    rc_txt, out_txt, err_txt = _run_cli(
        ["rules", "list", "--settings-file", str(settings_path)],
        env=cli_env,
    )
    assert rc_txt == 0, err_txt
    assert "learned" in out_txt
    assert "[masked by 'tax']" in out_txt
    assert "[masked by 'invoice']" in out_txt

    # 2) Verify TUI ActiveRuleManagerModal displays priority 90 and masked_by
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        rule_modal = ActiveRuleManagerModal(settings)
        app.push_screen(rule_modal)
        await pilot.pause()

        table = rule_modal.query_one("#rules-table", DataTable)
        assert table.row_count == 6

        ui_by_kind_pat = {(r["kind"], r["pattern"]): r for r in rule_modal._rules_data}
        assert ui_by_kind_pat[("learned", "invoice")]["priority"] == 90
        assert ui_by_kind_pat[("learned", "tax_return")]["priority"] == 90
        assert ui_by_kind_pat[("learned", "tax_return")]["masked_by"] == "tax"
        assert ui_by_kind_pat[("policy", "invoice_q1")]["masked_by"] == "invoice"
        assert ui_by_kind_pat[("keyword", "invoice_2026")]["priority"] == 80
        assert ui_by_kind_pat[("keyword", "tax_2026")]["masked_by"] == "tax"


# ---------------------------------------------------------------------------
# Req 9 & AC 5: Negative rating [-] unchecks & excludes file, skips Ctrl+E
# execution, and preserves !WrongFolder in Database
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ac5_negative_rating_unchecks_excludes_skips_execution_and_preserves_bang_marker(
    tmp_path,
):
    """AC 5: Pressing '-' sets is_checked=False & is_excluded=True, skips execution, and retains !WrongFolder in DB."""
    ws = tmp_path / "ac5_ws"
    ws.mkdir()
    rejected_file = ws / "misclassified_note.txt"
    rejected_file.write_text("Random personal note.", encoding="utf-8")
    approved_file = ws / "valid_invoice.txt"
    approved_file.write_text("Valid Q2 vendor invoice.", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.AUTO_QUARANTINE_RATINGS = []

    session = AppSession(settings, base_dir=str(ws))
    try:
        session.db.upsert_document(
            str(ws), rejected_file.name, "hash_rej", "Random personal note."
        )
        session.db.upsert_document(
            str(ws), approved_file.name, "hash_ok", "Valid Q2 vendor invoice."
        )

        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        app.app_session = session
        app.plan = {
            "WrongFolder": {
                "misclassified_note.txt": {
                    "__type__": "file",
                    "filepath": str(rejected_file),
                    "source": "misclassified_note.txt",
                    "target_filename": "misclassified_note.txt",
                    "is_checked": True,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "WrongFolder",
                }
            },
            "Invoices": {
                "valid_invoice.txt": {
                    "__type__": "file",
                    "filepath": str(approved_file),
                    "source": "valid_invoice.txt",
                    "target_filename": "valid_invoice.txt",
                    "is_checked": True,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "Invoices",
                }
            },
        }

        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app.render_tree()
            await pilot.pause()
            tree = app.query_one("#plan-tree", Tree)
            tree.focus()
            wrong_folder_node = next(
                ch for ch in tree.root.children if "WrongFolder" in str(ch.label)
            )
            wrong_folder_node.expand()
            await pilot.pause()
            rejected_leaf = wrong_folder_node.children[0]
            tree.select_node(rejected_leaf)
            await pilot.pause()

            await pilot.press("minus")
            await pilot.pause()

            leaf_info = app.plan["WrongFolder"]["misclassified_note.txt"]
            assert leaf_info["rating"] == "negative"
            assert leaf_info["is_checked"] is False
            assert leaf_info["is_excluded"] is True

            # Verify tree renders [EXCLUDED] and [-]
            rebuilt_folder = next(
                ch for ch in tree.root.children if "WrongFolder" in str(ch.label)
            )
            rebuilt_folder.expand()
            await pilot.pause()
            rendered_label = str(rebuilt_folder.children[0].label)
            assert "[EXCLUDED]" in rendered_label
            assert "[-]" in rendered_label

            # Verify _get_executable_plan (used by Ctrl+E) skips the negatively rated file
            exec_plan = app._get_executable_plan()
            assert "WrongFolder" not in exec_plan
            assert "Invoices" in exec_plan
            assert "valid_invoice.txt" in exec_plan["Invoices"]

        # Execute moves on the executable plan (simulating Ctrl+E move execution)
        move_summary = execute_moves(
            str(ws),
            exec_plan,
            session.db,
            session.history_manager,
            settings=settings,
        )
        assert rejected_file.exists(), "Rejected file must remain untouched on disk!"
        assert not (ws / "WrongFolder" / "misclassified_note.txt").exists()
        assert (ws / "Invoices" / "valid_invoice.txt").exists()
        assert isinstance(move_summary, dict)

        # Verify !WrongFolder is retained in documents.user_verified_target_path
        docs_map = {
            str(d[0]).replace("\\", "/"): d[3]
            for d in session.db.get_all_documents(str(ws))
            if len(d) > 3
        }
        assert docs_map.get("misclassified_note.txt") == "!WrongFolder"

        # Verify Database.update_document_path and batch updates preserve !WrongFolder
        session.db.update_document_path(
            str(ws),
            "misclassified_note.txt",
            "misclassified_note.txt",
            clear_verified_target=True,
        )
        session.db.execute_batch_updates(
            [
                {
                    "type": "document_path",
                    "args": (
                        str(ws),
                        "misclassified_note.txt",
                        "SomeOtherFolder/misclassified_note.txt",
                    ),
                },
                {
                    "type": "document_path",
                    "args": (
                        str(ws),
                        "SomeOtherFolder/misclassified_note.txt",
                        "misclassified_note.txt",
                    ),
                    "clear_verified_target": True,
                },
            ]
        )
        docs_after = {
            str(d[0]).replace("\\", "/"): d[3]
            for d in session.db.get_all_documents(str(ws))
            if len(d) > 3
        }
        assert docs_after.get("misclassified_note.txt") == "!WrongFolder"
    finally:
        session.close()


def test_db_preserves_bang_marker_directly(tmp_path):
    """Unit-verify Database.update_document_path and execute_batch_updates keep !Folder intact."""
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    session = AppSession(settings, base_dir=str(ws))
    try:
        db = session.db
        db.upsert_document(str(ws), "doc.txt", "h1", "sample body")
        db.set_user_verified_target_path(str(ws), "doc.txt", "!BadFolder")

        db.update_document_path(
            str(ws),
            "doc.txt",
            "NewFolder/renamed_doc.txt",
            clear_verified_target=False,
        )
        docs = {str(d[0]): d[3] for d in db.get_all_documents(str(ws))}
        assert docs.get("NewFolder/renamed_doc.txt") == "!BadFolder"

        db.execute_batch_updates(
            [
                {
                    "type": "document_path",
                    "args": (
                        str(ws),
                        "NewFolder/renamed_doc.txt",
                        "OtherFolder/moved_doc.txt",
                    ),
                },
                {
                    "type": "document_path",
                    "args": (str(ws), "OtherFolder/moved_doc.txt", "final_doc.txt"),
                    "clear_verified_target": True,
                },
            ]
        )
        docs2 = {str(d[0]): d[3] for d in db.get_all_documents(str(ws))}
        assert docs2.get("final_doc.txt") == "!BadFolder"
    finally:
        session.close()
