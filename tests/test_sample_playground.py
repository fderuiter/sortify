"""Tests for Interactive Onboarding Sample Playground Modal & Live Rule Simulation Engine."""

import asyncio
import os
import time

from textual.widgets import Button, Checkbox, DataTable, OptionList, Static

from app.config import AppSettings
from app.core.sample_corpus import (
    get_sample_corpus_items,
    simulate_corpus_classification,
)
from app.ui.modals.sample_playground import SamplePlaygroundModal
from app.ui.modals.wizard import WizardModal
from app.ui.tui import AutoSorterTUI


def test_get_sample_corpus_items():
    """Verify get_sample_corpus_items returns in-memory sample files."""
    items = get_sample_corpus_items()
    assert isinstance(items, dict)
    assert len(items) == 4
    assert "demo_finance.txt" in items
    assert "demo_tech.txt" in items
    assert "demo_health.txt" in items
    assert "empty.txt" in items
    assert "finance" in items["demo_finance.txt"].lower()


def test_simulate_corpus_classification_performance_and_accuracy():
    """Verify simulate_corpus_classification executes in <50ms per doc and reflects template pack rules."""
    settings = AppSettings()

    start_time = time.perf_counter()
    preds = simulate_corpus_classification(
        selected_packs=["financial_tax", "medical_health"],
        settings=settings,
    )
    elapsed_ms = (time.perf_counter() - start_time) * 1000

    # Metric 2: Live rule simulation executes in under 50ms per document
    assert elapsed_ms < 50.0

    assert len(preds) == 4
    assert "demo_finance.txt" in preds
    assert "demo_tech.txt" in preds
    assert "demo_health.txt" in preds
    assert "empty.txt" in preds

    # Check predictions structure
    fin_pred = preds["demo_finance.txt"]
    assert "filename" in fin_pred
    assert "category" in fin_pred
    assert "target_path" in fin_pred
    assert "confidence" in fin_pred
    assert "matched_rule" in fin_pred


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
            assert doc_list.option_count == 4

            # Verify doc preview box displays initial content
            preview = modal.query_one("#playground-doc-preview", Static)
            assert "finance" in str(preview.render()).lower()

            # Verify prediction matrix table is populated
            matrix = modal.query_one("#playground-prediction-matrix", DataTable)
            assert matrix.row_count == 4

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
