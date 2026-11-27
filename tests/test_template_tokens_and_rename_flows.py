"""End-to-end unit and TUI integration tests for template token separation and rename modal flows."""

from __future__ import annotations

import asyncio
import datetime
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from textual.widgets import Button, Input, Label, Static, Switch, Tree

from app.config import AppSettings, Settings
from app.core import extractor as extractor_mod
from app.core.file_renamer import FileRenamerEngine
from app.core.mover import execute_moves
from app.core.pattern_formatter import SUPPORTED_TOKENS, PatternTokenFormatter
from app.core.pipeline import SortPipeline
from app.core.session import AppSession
from app.core.verifier import VerificationEngine
from app.ui.markup_utils import format_file_inspector_lines
from app.ui.modals.edit import RenameModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def _isolate_app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate per-test app directory and session catalog."""
    app_dir = tmp_path / "_app_dir"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setattr("app.core.session_catalog.find_abandoned_sessions", lambda: [])


def _set_mtime(path: Path, year: int, month: int, day: int) -> None:
    ts = datetime.datetime(year, month, day, 12, 0, 0).timestamp()
    os.utime(path, (ts, ts))


def test_pattern_formatter_separates_stem_original_and_keywords() -> None:
    """Requirement 2: {stem} and {original} stay bound to source stem while {keywords} holds extracted terms."""
    assert "keywords" in SUPPORTED_TOKENS

    formatted = PatternTokenFormatter.format_pattern(
        "{date}_{keywords}_{stem}{extension}",
        metadata={
            "date": "2025-11-26",
            "keywords": "quarterly_budget",
            "stem": "Q1_Tax_Return_Final",
            "original": "Q1_Tax_Return_Final",
            "extension": ".txt",
        },
    )
    assert formatted == "2025-11-26_quarterly_budget_Q1_Tax_Return_Final.txt"

    # Empty {keywords} collapses redundant separator cleanly while preserving literal stem
    formatted_empty_kw = PatternTokenFormatter.format_pattern(
        "{date}_{keywords}_{original}{extension}",
        metadata={
            "date": "2025-11-26",
            "keywords": "",
            "original": "Q1_Tax_Return_Final",
            "extension": ".txt",
        },
    )
    assert formatted_empty_kw == "2025-11-26_Q1_Tax_Return_Final.txt"


def test_standalone_rename_pattern_formats_all_5_files_without_lazy_text_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1, Req 1 & Req 4: CONTEXTUAL_RENAMING=False + RENAME_PATTERN='{date}_{stem}{extension}' renames all 5 files using source stems and zero lazy reads."""
    ws = tmp_path / "inbox_standalone"
    ws.mkdir()

    files_spec = [
        (
            "scan_001_invoice.txt",
            "Vendor invoice payment schedule alpha",
            (2024, 3, 10),
        ),
        ("scan_002_invoice.txt", "Vendor invoice payment schedule beta", (2024, 3, 11)),
        (
            "Q1_Tax_Return_Final.txt",
            "Corporate tax return filing documentation",
            (2024, 3, 15),
        ),
        (
            "notes_alpha.txt",
            "Project alpha architecture roadmap milestones",
            (2024, 4, 1),
        ),
        (
            "scan_01.txt",
            "Project alpha budget allocation spreadsheet notes",
            (2024, 4, 2),
        ),
    ]
    for name, body, ymd in files_spec:
        fp = ws / name
        fp.write_text(body, encoding="utf-8")
        _set_mtime(fp, *ymd)

    settings = AppSettings(filepath=str(tmp_path / "standalone_settings.json"))
    settings.CONTEXTUAL_RENAMING = False
    settings.CLINICAL_SMART_RENAMING = False
    settings.AI_CONSENT_GRANTED = False
    settings.RENAME_PATTERN = "{date}_{stem}{extension}"
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "tax": "Tax",
        "alpha": "Project-Alpha",
        "scan_01": "Project-Alpha",
    }

    lazy_reads: list[str] = []
    orig_preview_fn = extractor_mod._read_preview_text_offline

    def _spy_lazy_read(file_path: str, *args: Any, **kwargs: Any) -> tuple[str, str]:
        lazy_reads.append(file_path)
        return orig_preview_fn(file_path, *args, **kwargs)

    monkeypatch.setattr(extractor_mod, "_read_preview_text_offline", _spy_lazy_read)

    session = AppSession(settings, str(ws))
    try:
        pipeline = SortPipeline(session)
        plan_obj = pipeline.plan()
        raw_plan = plan_obj.plan if hasattr(plan_obj, "plan") else dict(plan_obj)

        collected: dict[str, dict[str, Any]] = {}

        def _collect(node: dict[str, Any]) -> None:
            for k, v in node.items():
                if isinstance(v, dict):
                    if v.get("__type__") == "file":
                        src_name = os.path.basename(str(v.get("source_path") or k))
                        collected[src_name] = v
                    else:
                        _collect(v)

        _collect(raw_plan)
        assert len(collected) == 5
        assert lazy_reads == []

        expected = {
            "scan_001_invoice.txt": "2024-03-10_scan_001_invoice.txt",
            "scan_002_invoice.txt": "2024-03-11_scan_002_invoice.txt",
            "Q1_Tax_Return_Final.txt": "2024-03-15_Q1_Tax_Return_Final.txt",
            "notes_alpha.txt": "2024-04-01_notes_alpha.txt",
            "scan_01.txt": "2024-04-02_scan_01.txt",
        }
        for src_name, exp_target in expected.items():
            node_info = collected[src_name]
            assert node_info.get("target_filename") == exp_target
            assert node_info.get("rename_source") == "Template"
    finally:
        session.close()


def test_contextual_renaming_preserves_descriptive_stems_formats_bypassed_and_prevents_collisions(
    tmp_path: Path,
) -> None:
    """AC 2, Req 3, 4, 5 & Guardrails: CONTEXTUAL_RENAMING=True + '{date}_{original}' preserves Q1_Tax_Return_Final, lazy-formats bypassed scan_001_invoice.txt, scopes {seq} per folder, and yields zero collisions."""
    ws = tmp_path / "inbox_contextual"
    ws.mkdir()

    # Pre-existing file on disk in destination folder to verify disk collision disambiguation too
    invoices_dir = ws / "Invoices"
    invoices_dir.mkdir()
    preexisting_disk = invoices_dir / "2024-03-20_acme_vendor_remittance.txt"
    preexisting_disk.write_text("existing archived invoice", encoding="utf-8")

    # 1. Descriptive stem that MUST remain intact
    tax_file = ws / "Q1_Tax_Return_Final.txt"
    tax_file.write_text(
        "Quarterly corporate revenue ledger reconciliation details.",
        encoding="utf-8",
    )
    _set_mtime(tax_file, 2024, 3, 15)

    # 2 & 3. Two bypassed keyword-rule matches with identical body text in same folder ("Invoices")
    inv1 = ws / "scan_001_invoice.txt"
    inv2 = ws / "scan_002_invoice.txt"
    shared_invoice_body = (
        "Invoice Date: 2024-03-20. Acme vendor remittance acme vendor remittance."
    )
    inv1.write_text(shared_invoice_body, encoding="utf-8")
    inv2.write_text(shared_invoice_body, encoding="utf-8")
    _set_mtime(inv1, 2024, 3, 20)
    _set_mtime(inv2, 2024, 3, 20)

    settings = AppSettings(filepath=str(tmp_path / "contextual_settings.json"))
    settings.CONTEXTUAL_RENAMING = True
    settings.AI_CONSENT_GRANTED = False
    settings.AI_ASSISTED_NAMING = (
        True  # Must be ignored because AI_CONSENT_GRANTED is False
    )
    settings.RENAME_PATTERN = "{date}_{original}"
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "tax_return": "Tax",
    }

    session = AppSession(settings, str(ws))
    try:
        # Spy on embedding_manager to verify zero ONNX calls when consent is False
        mock_emb = MagicMock()
        mock_emb.is_mock = False
        mock_emb.is_degraded = False
        mock_emb.is_reconstruction_active.return_value = False
        session.analyzer.embedding_manager = mock_emb

        pipeline = SortPipeline(session)
        plan_obj = pipeline.plan()
        raw_plan = plan_obj.plan if hasattr(plan_obj, "plan") else dict(plan_obj)

        mock_emb.generate_embedding.assert_not_called()

        tax_node = raw_plan["Tax"]["Q1_Tax_Return_Final.txt"]
        assert tax_node["target_filename"] == "2024-03-15_Q1_Tax_Return_Final.txt"
        assert tax_node["rename_source"] == "Template"

        inv_node_1 = raw_plan["Invoices"]["scan_001_invoice.txt"]
        inv_node_2 = raw_plan["Invoices"]["scan_002_invoice.txt"]
        tf_1 = inv_node_1["target_filename"]
        tf_2 = inv_node_2["target_filename"]

        assert tf_1 != "scan_001_invoice.txt"
        assert tf_2 != "scan_002_invoice.txt"
        assert tf_1 != tf_2
        # Pre-existing disk file 2024-03-20_acme_vendor_remittance.txt must also be avoided!
        assert tf_1 != preexisting_disk.name
        assert tf_2 != preexisting_disk.name
        assert inv_node_1["rename_source"] == "Contextual Keywords"
        assert inv_node_2["rename_source"] == "Contextual Keywords"

        # Verify zero same-folder collisions in VerificationEngine (verifier.py:L755-L837)
        collisions = VerificationEngine.get_collision_groups(str(ws), raw_plan)
        assert collisions == []

        # Confirm renames and verify MoveLedger execution + undo rollback semantics
        for folder_dict in raw_plan.values():
            if isinstance(folder_dict, dict):
                for file_info in folder_dict.values():
                    if isinstance(file_info, dict) and file_info.get("target_filename"):
                        file_info["confirmed"] = True

        hm = session.history_manager
        execute_moves(str(ws), raw_plan, session.db, hm)
        assert (ws / "Tax" / "2024-03-15_Q1_Tax_Return_Final.txt").is_file()
        assert (ws / "Invoices" / tf_1).is_file()
        assert (ws / "Invoices" / tf_2).is_file()
        assert preexisting_disk.is_file()

        # Rollback restores original filenames cleanly
        sid = hm.get_sessions()[0]["session_id"]
        hm.rollback(sid)
        assert tax_file.is_file()
        assert inv1.is_file()
        assert inv2.is_file()
    finally:
        session.close()


def test_per_folder_seq_counters_and_keywords_token_in_file_renamer(
    tmp_path: Path,
) -> None:
    """Req 2 & Req 5: {seq} resets to 01 per destination folder and {keywords} populates without clobbering {stem}."""
    f_a1 = tmp_path / "alpha_first.txt"
    f_a2 = tmp_path / "alpha_second.txt"
    f_b1 = tmp_path / "beta_first.txt"
    for p, txt in [
        (f_a1, "budget forecast variance analysis"),
        (f_a2, "quarterly payroll audit summary"),
        (f_b1, "contract renewal addendum clause"),
    ]:
        p.write_text(txt, encoding="utf-8")
        _set_mtime(p, 2025, 11, 26)

    settings = Settings(
        CONTEXTUAL_RENAMING=False,
        RENAME_PATTERN="{seq}_{keywords}_{stem}{extension}",
    )
    engine = FileRenamerEngine(runtime_settings=settings)
    plan: dict[str, Any] = {
        "Finance": {
            "alpha_first.txt": {
                "__type__": "file",
                "source_path": "alpha_first.txt",
                "relative_source": "../alpha_first.txt",
            },
            "alpha_second.txt": {
                "__type__": "file",
                "source_path": "alpha_second.txt",
                "relative_source": "../alpha_second.txt",
            },
        },
        "Legal": {
            "beta_first.txt": {
                "__type__": "file",
                "source_path": "beta_first.txt",
                "relative_source": "../beta_first.txt",
            },
        },
    }
    engine.process_sorting_plan(plan, documents_map={}, base_dir=str(tmp_path))

    tf_a1 = plan["Finance"]["alpha_first.txt"]["target_filename"]
    tf_a2 = plan["Finance"]["alpha_second.txt"]["target_filename"]
    tf_b1 = plan["Legal"]["beta_first.txt"]["target_filename"]

    assert tf_a1.startswith("01_") and tf_a1.endswith("_alpha_first.txt")
    assert tf_a2.startswith("02_") and tf_a2.endswith("_alpha_second.txt")
    # Legal resets {seq} counter to 01!
    assert tf_b1.startswith("01_") and tf_b1.endswith("_beta_first.txt")


def test_inspector_rename_source_provenance_all_sources() -> None:
    """Requirement 7: format_file_inspector_lines renders Rename Source across all 5 provenance types."""
    for source_label in (
        "Template",
        "Contextual Keywords",
        "AI Keyphrases",
        "Policy Rule",
        "Manual",
    ):
        lines = format_file_inspector_lines(
            key="scan_01.txt",
            path_display="/ws/scan_01.txt",
            folder="Project-Alpha",
            locked="No",
            rating="unrated",
            info={
                "source_path": "scan_01.txt",
                "target_filename": "2025-11-26_project_alpha_budget.txt",
                "rename_source": source_label,
            },
        )
        joined = "\n".join(lines)
        assert (
            "[bold]Target Filename:[/bold] 2025-11-26_project_alpha_budget.txt"
            in joined
        )
        assert f"[bold]Rename Source:[/bold] {source_label}" in joined


def test_settings_modal_ai_assisted_naming_toggle_and_consent_gate(
    tmp_path: Path,
) -> None:
    """Requirement 6: SettingsModal exposes #switch-ai-assisted-naming gated on AI_CONSENT_GRANTED."""

    async def _run() -> None:
        settings = AppSettings(filepath=str(tmp_path / "ui_settings.json"))
        settings.AI_CONSENT_GRANTED = False
        settings.AI_ASSISTED_NAMING = False
        settings.SORTING_STRATEGY = "default"

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()

            # 1. With AI_CONSENT_GRANTED=False, switch is disabled and False
            app.action_open_settings()
            await pilot.pause()
            assert isinstance(app.screen, SettingsModal)
            sw_assisted = app.screen.query_one("#switch-ai-assisted-naming", Switch)
            assert sw_assisted.disabled is True
            assert sw_assisted.value is False
            hint_lbl = app.screen.query_one("#lbl-rename-template-hint", Label)
            assert "{stem}" in str(hint_lbl.render())
            assert "{keywords}" in str(hint_lbl.render())

            # Grant consent -> enables AI Assisted Naming under default strategy
            sw_consent = app.screen.query_one("#switch-ai-consent", Switch)
            sw_consent.value = True
            await pilot.pause()
            assert sw_assisted.disabled is False

            sw_assisted.value = True
            await pilot.pause()
            app.screen.query_one("#btn-save", Button).press()
            await pilot.pause()

            assert settings.AI_CONSENT_GRANTED is True
            assert settings.SORTING_STRATEGY == "default"
            assert settings.AI_ASSISTED_NAMING is True

    asyncio.run(_run())


def test_single_file_f2_prefills_target_filename_and_reverts_via_button_or_input(
    tmp_path: Path,
) -> None:
    """AC 3 & Req 8: Pressing F2 on scan_01.txt -> project_alpha_budget.txt pre-fills project_alpha_budget, and Reset to Original or typing scan_01 restores scan_01.txt."""

    async def _run() -> None:
        ws = tmp_path / "f2_revert_ws"
        ws.mkdir()
        f1 = ws / "scan_01.txt"
        f2 = ws / "scan_02.txt"
        f1.write_text("budget", encoding="utf-8")
        f2.write_text("schedule", encoding="utf-8")
        _set_mtime(f1, 2025, 11, 26)
        _set_mtime(f2, 2025, 11, 26)

        settings = AppSettings(filepath=str(tmp_path / "f2_settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        app.plan = {
            "Project-Alpha": {
                "scan_01.txt": {
                    "__type__": "file",
                    "source_path": "scan_01.txt",
                    "relative_source": "../scan_01.txt",
                    "target_filename": "project_alpha_budget.txt",
                    "rename_source": "Contextual Keywords",
                    "is_checked": False,
                },
                "scan_02.txt": {
                    "__type__": "file",
                    "source_path": "scan_02.txt",
                    "relative_source": "../scan_02.txt",
                    "target_filename": "project_alpha_schedule.txt",
                    "rename_source": "Contextual Keywords",
                    "is_checked": False,
                },
            }
        }

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.rebuild_tree()
            await pilot.pause()

            tree = app.query_one("#plan-tree", Tree)
            folder_node = tree.root.children[0]
            folder_node.expand()
            await pilot.pause()

            # Select scan_01.txt -> project_alpha_budget.txt and check #meta-details provenance
            node_1 = folder_node.children[0]
            tree.select_node(node_1)
            await pilot.pause()
            meta_txt = str(app.query_one("#meta-details", Static).render())
            assert "Contextual Keywords" in meta_txt

            # 1. Open F2 -> pre-fills project_alpha_budget, {stem} still resolves to scan_01, Reset to Original restores scan_01.txt
            app.action_rename_node()
            await pilot.pause()
            assert isinstance(app.screen, RenameModal)
            inp = app.screen.query_one("#input-name", Input)
            assert inp.value == "project_alpha_budget"
            assert (
                app.screen._evaluate_preview("{date}_{stem}")
                == "2025-11-26_scan_01.txt"
            )

            app.screen.query_one("#btn-reset-original", Button).press()
            await pilot.pause()

            info_1 = app.plan["Project-Alpha"]["scan_01.txt"]
            assert info_1.get("target_filename") is None
            assert info_1.get("rename_source") is None

            # 2. Select scan_02.txt -> project_alpha_schedule.txt, open F2, type 'scan_02', confirm -> restores scan_02.txt
            folder_node = tree.root.children[0]
            folder_node.expand()
            await pilot.pause()
            node_2 = folder_node.children[1]
            tree.select_node(node_2)
            await pilot.pause()

            app.action_rename_node()
            await pilot.pause()
            assert isinstance(app.screen, RenameModal)
            inp_2 = app.screen.query_one("#input-name", Input)
            assert inp_2.value == "project_alpha_schedule"
            inp_2.value = "scan_02"
            await pilot.pause()
            app.screen.query_one("#btn-confirm", Button).press()
            await pilot.pause()

            info_2 = app.plan["Project-Alpha"]["scan_02.txt"]
            assert info_2.get("target_filename") is None
            assert info_2.get("rename_source") is None

    asyncio.run(_run())


def test_batch_f2_repeated_edits_do_not_compound_prefix_and_check_per_folder_collisions(
    tmp_path: Path,
) -> None:
    """AC 4, AC 5, Req 9 & Req 10: Repeated batch F2 keeps {original} immutable; multi-folder collisions suppressed across folders and warned in-plan or on-disk."""

    async def _run() -> None:
        ws = tmp_path / "batch_f2_ws"
        ws.mkdir()
        f_alpha = ws / "notes_alpha.txt"
        f_beta = ws / "notes_beta.txt"
        f_alpha.write_text("alpha notes", encoding="utf-8")
        f_beta.write_text("beta notes", encoding="utf-8")
        _set_mtime(f_alpha, 2025, 11, 26)
        _set_mtime(f_beta, 2025, 11, 26)

        # Create an untracked file on disk inside Project-Beta to test os.path.lexists check
        beta_dir = ws / "Project-Beta"
        beta_dir.mkdir()
        (beta_dir / "disk_taken.txt").write_text("on disk", encoding="utf-8")

        settings = AppSettings(filepath=str(tmp_path / "batch_settings.json"))
        settings.RENAME_PATTERN = ""
        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        app.plan = {
            "Project-Alpha": {
                "notes_alpha.txt": {
                    "__type__": "file",
                    "source_path": "notes_alpha.txt",
                    "relative_source": "../notes_alpha.txt",
                    "is_checked": True,
                }
            },
            "Project-Beta": {
                "notes_beta.txt": {
                    "__type__": "file",
                    "source_path": "notes_beta.txt",
                    "relative_source": "../notes_beta.txt",
                    "is_checked": True,
                },
                "plan_taken.txt": {
                    "__type__": "file",
                    "source_path": "plan_taken.txt",
                    "relative_source": "../plan_taken.txt",
                    "is_checked": False,
                },
            },
        }

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.rebuild_tree()
            await pilot.pause()

            # 1st batch F2 with {category}_{original}
            app.action_rename_node()
            await pilot.pause()
            assert isinstance(app.screen, RenameModal)
            inp = app.screen.query_one("#input-name", Input)
            assert inp.value == "{category}_{original}"

            # Test AC 5a: Identical basename across SEPARATE folders ('summary') suppresses false duplicate warning!
            inp.value = "summary"
            await pilot.pause()
            warn_lbl = app.screen.query_one("#warning-collision", Label)
            assert str(warn_lbl.render()).strip() == ""

            # Test AC 5b: Colliding with sibling in 2nd checked folder ('plan_taken' in Project-Beta) warns!
            inp.value = "plan_taken"
            await pilot.pause()
            assert "Collision detected" in str(warn_lbl.render())

            # Test AC 5c: Colliding with file on disk in Project-Beta ('disk_taken') via os.path.lexists warns!
            inp.value = "disk_taken"
            await pilot.pause()
            assert "exists on disk" in str(warn_lbl.render())

            # Apply {category}_{original} (1st time)
            inp.value = "{category}_{original}"
            await pilot.pause()
            assert str(warn_lbl.render()).strip() == ""
            app.screen.query_one("#btn-confirm", Button).press()
            await pilot.pause()

            assert "Project-Alpha_notes_alpha.txt" in app.plan["Project-Alpha"]
            assert "Project-Beta_notes_beta.txt" in app.plan["Project-Beta"]
            assert (
                app.plan["Project-Alpha"]["Project-Alpha_notes_alpha.txt"][
                    "rename_source"
                ]
                == "Manual"
            )

            # 2nd batch F2 with {category}_{original} -> MUST NOT double prefix!
            app.action_rename_node()
            await pilot.pause()
            assert isinstance(app.screen, RenameModal)
            inp_second = app.screen.query_one("#input-name", Input)
            assert inp_second.value == "{category}_{original}"
            app.screen.query_one("#btn-confirm", Button).press()
            await pilot.pause()

            assert "Project-Alpha_notes_alpha.txt" in app.plan["Project-Alpha"]
            assert (
                "Project-Alpha_Project-Alpha_notes_alpha.txt"
                not in app.plan["Project-Alpha"]
            )
            assert "Project-Beta_notes_beta.txt" in app.plan["Project-Beta"]
            assert (
                "Project-Beta_Project-Beta_notes_beta.txt"
                not in app.plan["Project-Beta"]
            )

    asyncio.run(_run())
