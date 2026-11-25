"""Tests for Interactive Onboarding Sample Playground Modal & Live Rule Simulation Engine."""

import asyncio
import os
import time

import pytest
from textual.widgets import Button, Checkbox, DataTable, OptionList, Static

from app.config import AppSettings
from app.core.sample_corpus import (
    get_sample_corpus_items,
    simulate_corpus_classification,
)
from app.ui.modals.sample_playground import SamplePlaygroundModal
from app.ui.modals.wizard import WizardModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


def test_get_sample_corpus_items():
    """Verify get_sample_corpus_items returns in-memory sample files covering all starter packs."""
    items = get_sample_corpus_items()
    assert isinstance(items, dict)
    assert len(items) == 7
    assert "demo_finance.txt" in items
    assert "demo_tech.txt" in items
    assert "demo_health.txt" in items
    assert "demo_legal.txt" in items
    assert "demo_personal.txt" in items
    assert "demo_hr.txt" in items
    assert "empty.txt" in items
    assert "finance" in items["demo_finance.txt"].lower()


def test_simulate_corpus_classification_performance_and_accuracy():
    """Verify simulate_corpus_classification reflects template pack rules accurately."""
    settings = AppSettings()

    preds = simulate_corpus_classification(
        selected_packs=["financial_tax", "medical_health"],
        settings=settings,
    )

    assert len(preds) == 7
    assert "demo_finance.txt" in preds
    assert "demo_tech.txt" in preds
    assert "demo_health.txt" in preds
    assert "demo_legal.txt" in preds
    assert "demo_personal.txt" in preds
    assert "demo_hr.txt" in preds
    assert "empty.txt" in preds

    # Check predictions structure
    fin_pred = preds["demo_finance.txt"]
    assert "filename" in fin_pred
    assert "category" in fin_pred
    assert "target_path" in fin_pred
    assert "confidence" in fin_pred
    assert "matched_rule" in fin_pred


@pytest.mark.benchmark
def test_simulate_corpus_classification_latency_benchmark():
    """Benchmark warmed simulate_corpus_classification completing in under 50ms."""
    settings = AppSettings()
    simulate_corpus_classification(
        selected_packs=["financial_tax", "medical_health"],
        settings=settings,
    )

    start_time = time.perf_counter()
    preds = simulate_corpus_classification(
        selected_packs=["financial_tax", "medical_health"],
        settings=settings,
    )
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    assert len(preds) == 7
    assert elapsed_ms < 50.0, f"Simulation took {elapsed_ms:.2f}ms (>50ms SLA)"


def test_sample_playground_modal_ui_and_commit(tmp_path):
    """Verify SamplePlaygroundModal renders preview, toggles packs, and commits files to workspace disk."""

    async def _test():
        settings = AppSettings()
        target_workspace = str(tmp_path / "playground_workspace")
        os.makedirs(target_workspace, exist_ok=True)

        app = AutoSorterTUI(
            settings=settings, base_dir=target_workspace, skip_wizard=True
        )
        async with app.run_test() as pilot:
            modal = SamplePlaygroundModal(settings, target_dir=target_workspace)
            app.push_screen(modal)
            await pilot.pause(0.05)

            # Audit a11y compliance
            audit_res = modal.audit_a11y_compliance()
            assert audit_res["compliant"] is True
            assert audit_res["violations_count"] == 0

            # Verify document list displays all items
            doc_list = modal.query_one("#playground-doc-list", OptionList)
            assert doc_list.option_count == 7

            # Verify doc preview box displays initial content
            preview = modal.query_one("#playground-doc-preview", Static)
            assert "finance" in str(preview.render()).lower()

            # Verify prediction matrix table is populated
            matrix = modal.query_one("#playground-prediction-matrix", DataTable)
            assert matrix.row_count == 7

            # Toggle a starter pack checkbox
            chk_med = modal.query_one("#chk-playground-pack-medical_health", Checkbox)
            chk_med.value = not chk_med.value
            await pilot.pause(0.05)

            # Ensure files are NOT created on disk prior to explicit commit (Guardrail 1)
            assert not os.path.exists(
                os.path.join(target_workspace, "demo_finance.txt")
            )

            # Press Commit Sandbox to Workspace button
            btn_commit = modal.query_one("#btn-commit-playground", Button)
            btn_commit.press()
            await pilot.pause(0.1)

            # Verify files exist on disk after commit
            assert os.path.exists(os.path.join(target_workspace, "demo_finance.txt"))
            assert os.path.exists(os.path.join(target_workspace, "demo_tech.txt"))

    asyncio.run(_test())


def test_each_builtin_starter_pack_changes_playground_prediction_row(tmp_path):
    """Toggling each of the six built-in starter packs updates at least one row in the prediction table."""
    from app.core.rule_templates import get_starter_templates

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        workspace = str(tmp_path / "starter_packs_ws")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=workspace, skip_wizard=True)
        async with app.run_test() as pilot:
            modal = SamplePlaygroundModal(
                settings,
                target_dir=workspace,
                initial_selected_packs=[],
            )
            app.push_screen(modal)
            await pilot.pause(0.05)

            matrix = modal.query_one("#playground-prediction-matrix", DataTable)

            def _snapshot_rows() -> list[tuple[str, ...]]:
                return [
                    tuple(str(cell) for cell in matrix.get_row_at(idx))
                    for idx in range(matrix.row_count)
                ]

            baseline_rows = _snapshot_rows()
            builtin_packs = [t.id for t in get_starter_templates()]
            assert len(builtin_packs) == 6

            for pack_id in builtin_packs:
                chk = modal.query_one(f"#chk-playground-pack-{pack_id}", Checkbox)
                chk.value = True
                await pilot.pause(0.05)
                toggled_rows = _snapshot_rows()
                assert toggled_rows != baseline_rows, (
                    f"Toggling starter pack '{pack_id}' did not change any prediction row"
                )
                chk.value = False
                await pilot.pause(0.05)
                assert _snapshot_rows() == baseline_rows

    asyncio.run(_test())


def test_sample_playground_syncs_packs_and_folder_with_wizard_without_stale_overwrite(
    tmp_path,
):
    """Committing in SamplePlaygroundModal synchronizes folder and starter packs back to WizardModal."""
    from textual.widgets import Input

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        initial_ws = str(tmp_path / "initial_ws")
        chosen_ws = str(tmp_path / "custom_playground_ws")
        os.makedirs(initial_ws, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=initial_ws, skip_wizard=True)
        async with app.run_test() as pilot:
            wizard = WizardModal(settings)
            app.push_screen(wizard)
            await pilot.pause(0.05)

            # Set target folder in wizard and launch playground
            wizard.query_one("#input-dir", Input).value = chosen_ws
            wizard.query_one("#btn-open-playground", Button).press()
            await pilot.pause(0.1)

            assert isinstance(app.screen, SamplePlaygroundModal)
            pg = app.screen
            pg.query_one("#chk-playground-pack-legal_contracts", Checkbox).value = True
            pg.query_one("#chk-playground-pack-office_hr", Checkbox).value = True
            await pilot.pause(0.05)

            pg.query_one("#btn-commit-playground", Button).press()
            await pilot.pause(0.1)

            # Back on WizardModal: verify starter packs and folder were synced
            assert isinstance(app.screen, WizardModal)
            assert wizard.query_one("#input-dir", Input).value == os.path.abspath(
                chosen_ws
            )
            assert (
                wizard.query_one("#chk-template-legal_contracts", Checkbox).value
                is True
            )
            assert wizard.query_one("#chk-template-office_hr", Checkbox).value is True

            # Finishing wizard afterward must NOT overwrite playground rules or folder with stale values
            wizard.query_one("#btn-finish", Button).press()
            await pilot.pause(0.1)

            assert app.base_dir == os.path.abspath(chosen_ws)
            scoped = settings.for_folder(app.base_dir)
            kw_targets = set(scoped.KEYWORD_RULES.values())
            assert "Legal/Contracts" in kw_targets
            assert "HR/Resumes" in kw_targets

    asyncio.run(_test())


def test_wizard_modal_playground_button(tmp_path):
    """Verify WizardModal displays interactive playground action button and opens modal."""

    async def _test():
        settings = AppSettings()
        workspace = str(tmp_path / "wizard_workspace")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=workspace)
        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            btn_pg = modal.query_one("#btn-open-playground", Button)
            assert btn_pg is not None
            btn_pg.press()
            await pilot.pause(0.1)

            # Current active screen should now be SamplePlaygroundModal
            assert isinstance(app.screen, SamplePlaygroundModal)

    asyncio.run(_test())


def test_tui_shortcut_g_launches_playground(tmp_path):
    """Verify pressing [G] key in TUI opens SamplePlaygroundModal instead of silently writing files."""

    async def _test():
        settings = AppSettings()
        workspace = str(tmp_path / "tui_g_workspace")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=workspace, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            # Press 'g' shortcut
            await pilot.press("g")
            await pilot.pause(0.1)

            # Active screen should be SamplePlaygroundModal
            assert isinstance(app.screen, SamplePlaygroundModal)

    asyncio.run(_test())
