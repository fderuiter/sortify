"""Tests for Onboarding CTA Card in empty right-hand meta pane."""

import asyncio
import os

import pytest
from textual.widgets import Button, Static, Tree

from app.config import AppSettings
from app.ui.modals.edit import DirectorySelectModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


def test_onboarding_cta_card_initial_visibility(tmp_path):
    """Verify onboarding CTA card renders in #right-meta-pane on startup with no plan."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        empty_workspace = str(tmp_path / "empty_workspace")
        os.makedirs(empty_workspace, exist_ok=True)

        app = AutoSorterTUI(
            settings=settings, base_dir=empty_workspace, skip_wizard=True
        )
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            # Requirement 1: #onboarding-cta-card container inside #right-meta-pane
            right_pane = app.query_one("#right-meta-pane")
            cta_card = right_pane.query_one("#onboarding-cta-card")
            assert cta_card is not None
            assert cta_card.display is True

            # Requirement 2: Interactive action buttons
            btn_select = cta_card.query_one("#btn-cta-select-dir", Button)
            btn_scan = cta_card.query_one("#btn-cta-scan-dir", Button)
            btn_sample = cta_card.query_one("#btn-cta-sample-data", Button)

            assert btn_select is not None
            assert btn_scan is not None
            assert btn_sample is not None

            # Requirement 3: Current workspace state displayed inside card
            ws_state = cta_card.query_one("#onboarding-workspace-state", Static)
            assert ws_state is not None
            assert empty_workspace in str(ws_state.render())

            # Accessibility audit
            audit_res = app.audit_a11y_compliance()
            assert audit_res["compliant"] is True

    asyncio.run(_test())


def test_onboarding_cta_card_select_dir_button(tmp_path):
    """Verify clicking #btn-cta-select-dir opens DirectorySelectModal."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        empty_workspace = str(tmp_path / "empty_workspace")
        os.makedirs(empty_workspace, exist_ok=True)

        app = AutoSorterTUI(
            settings=settings, base_dir=empty_workspace, skip_wizard=True
        )
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            btn_select = app.query_one("#btn-cta-select-dir", Button)
            btn_select.press()
            await pilot.pause(0.05)

            # Check screen pushed is DirectorySelectModal
            assert isinstance(app.screen, DirectorySelectModal)

    asyncio.run(_test())


def test_onboarding_cta_card_scan_dir_button(tmp_path):
    """Verify clicking #btn-cta-scan-dir initiates directory scan."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        workspace = str(tmp_path / "scan_workspace")
        os.makedirs(workspace, exist_ok=True)
        # Create a file so scan populates a plan
        with open(os.path.join(workspace, "test_doc.txt"), "w", encoding="utf-8") as f:
            f.write("Some test document text")

        app = AutoSorterTUI(settings=settings, base_dir=workspace, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            btn_scan = app.query_one("#btn-cta-scan-dir", Button)
            btn_scan.press()
            await pilot.pause(0.05)
            for _ in range(50):
                if not app._running_pipeline_worker():
                    break
                await pilot.pause(0.1)

            await pilot.pause(0.05)

            # After scan finishes, card should be hidden as plan data exists
            cta_card = app.query_one("#onboarding-cta-card")
            assert cta_card.display is False

            inspector = app.query_one("#inspector-container")
            assert inspector.display is True

    asyncio.run(_test())


def test_onboarding_cta_card_sample_data_button(tmp_path):
    """Verify clicking #btn-cta-sample-data generates sample corpus files."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        workspace = str(tmp_path / "sample_workspace")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=workspace, skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            btn_sample = app.query_one("#btn-cta-sample-data", Button)
            btn_sample.press()
            await pilot.pause(0.05)
            for _ in range(50):
                if not app._running_pipeline_worker():
                    break
                await pilot.pause(0.1)

            await pilot.pause(0.05)

            # Sample dataset generated
            sample_dir = os.path.join(workspace, "sortify-sample-dataset")
            if not os.path.exists(sample_dir):
                sample_dir = workspace
            assert os.path.exists(os.path.join(sample_dir, "demo_finance.txt"))

    asyncio.run(_test())


def test_onboarding_cta_card_transition_on_node_selection(tmp_path):
    """Verify selecting a tree node hides card and reveals Node Metadata Inspector."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        workspace = str(tmp_path / "transition_workspace")
        os.makedirs(workspace, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir=workspace, skip_wizard=True)
        async with app.run_test() as pilot:
            # Set up plan
            app.plan = {
                "Invoices": {
                    "inv_001.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(workspace, "inv_001.pdf"),
                        "target_filename": "inv_001.pdf",
                    }
                }
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            cta_card = app.query_one("#onboarding-cta-card")
            inspector = app.query_one("#inspector-container")

            # With plan loaded, CTA card should be hidden and inspector container visible
            assert cta_card.display is False
            assert inspector.display is True

            # Select the node in the tree
            tree = app.query_one("#plan-tree", Tree)
            file_node = None
            for child in tree.root.children:
                for subchild in child.children:
                    file_node = subchild
                    break

            if file_node:
                tree.select_node(file_node)
                await pilot.pause(0.05)
                assert cta_card.display is False
                assert inspector.display is True

    asyncio.run(_test())
