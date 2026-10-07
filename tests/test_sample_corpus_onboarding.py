"""Tests for Interactive Sample Corpus Wizard & Guided Empty-State Onboarding."""

import asyncio
import os

import pytest
from textual.widgets import Button, Switch, Tree

from app.config import AppSettings
from app.core.sample_corpus import generate_sample_corpus
from app.ui.tui import AutoSorterTUI, WizardModal, inspect_tui_component


def test_generate_sample_corpus_engine(tmp_path):
    """Verify shared sample data generation engine creates categorized files and enforces overwrite guardrail."""
    target_dir = str(tmp_path / "workspace")

    # Initial generation
    files = generate_sample_corpus(target_dir, overwrite=False)
    assert len(files) == 4
    assert "demo_finance.txt" in files
    assert "demo_tech.txt" in files
    assert "demo_health.txt" in files
    assert "empty.txt" in files

    # Verify content
    with open(os.path.join(target_dir, "demo_finance.txt")) as f:
        content = f.read()
        assert "finance" in content.lower()

    # Guardrail 1: Calling without overwrite when files exist raises FileExistsError
    with pytest.raises(FileExistsError):
        generate_sample_corpus(target_dir, overwrite=False)

    # Calling with overwrite=True succeeds
    recreated = generate_sample_corpus(target_dir, overwrite=True)
    assert len(recreated) == 4


def test_wizard_modal_sample_corpus_option(tmp_path):
    """Verify WizardModal presents sample document switch, generates corpus, and triggers scan."""

    async def _test():
        settings = AppSettings()
        workspace = str(tmp_path / "tui_workspace")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=workspace)
        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            # Verify a11y compliance for WizardModal
            a11y_res = modal.audit_a11y_compliance()
            assert a11y_res["compliant"] is True
            assert a11y_res["violations_count"] == 0

            sw_sample = modal.query_one("#switch-sample-corpus", Switch)
            sw_sample.value = True
            await pilot.pause(0.05)

            btn_finish = modal.query_one("#btn-finish", Button)
            btn_finish.press()
            await pilot.pause(0.1)

            # Check sample files generated in workspace
            assert os.path.exists(os.path.join(workspace, "demo_finance.txt"))
            assert os.path.exists(os.path.join(workspace, "demo_tech.txt"))

    asyncio.run(_test())


def test_empty_workspace_guided_state_card_and_shortcut(tmp_path):
    """Verify scanning empty directory renders interactive empty state card and G shortcut generates sample files."""

    async def _test():
        settings = AppSettings()
        empty_workspace = str(tmp_path / "empty_workspace")
        os.makedirs(empty_workspace, exist_ok=True)

        app = AutoSorterTUI(
            settings=settings, base_dir=empty_workspace, skip_wizard=True
        )
        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause(0.05)

            # Verify tree displays empty-state nodes
            tree = app.query_one("#plan-tree", Tree)
            node_labels = [str(child.label) for child in tree.root.children]
            assert any("Empty Workspace" in label for label in node_labels)
            assert any(
                "Press [G] to Load Sample Dataset" in label for label in node_labels
            )

            # Verify a11y compliance of app in empty state
            app_audit = app.audit_a11y_compliance()
            assert app_audit["compliant"] is True
            assert app_audit["violations_count"] == 0
            assert len(inspect_tui_component(app)) == 0

            # Press 'g' shortcut to load sample dataset
            await pilot.press("g")
            await pilot.pause(0.1)

            # Verify sample files created in empty_workspace
            assert os.path.exists(os.path.join(empty_workspace, "demo_finance.txt"))
            assert os.path.exists(os.path.join(empty_workspace, "demo_tech.txt"))

    asyncio.run(_test())


def test_skip_wizard_bypasses_prompts(tmp_path):
    """Verify --skip-wizard or non-interactive mode bypasses onboarding wizard."""

    async def _test():
        settings = AppSettings()
        workspace = str(tmp_path / "skip_workspace")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(
            settings=settings,
            base_dir=workspace,
            skip_wizard=True,
            non_interactive=True,
        )
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            # Ensure screen is not WizardModal
            assert not isinstance(app.screen, WizardModal)

    asyncio.run(_test())
