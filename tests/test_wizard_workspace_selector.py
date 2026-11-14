"""Unit and integration tests for WizardModal workspace selector, preset buttons, and safe corpus delegation."""

import asyncio
import os

import pytest
from textual.widgets import Button, Checkbox, Input, Static

from app.config import AppSettings
from app.ui.a11y import inspect_tui_component
from app.ui.modals.wizard import WizardModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


def test_wizard_modal_workspace_selector_components_and_a11y(tmp_path):
    """Verify WizardModal renders workspace directory input, quick preset buttons, and passes a11y audit."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings, current_dir=str(tmp_path))
            app.push_screen(modal)
            await pilot.pause(0.05)

            inp = modal.query_one("#input-dir", Input)
            assert inp is not None
            assert inp.value == str(tmp_path)
            assert inp.tooltip is not None

            err_lbl = modal.query_one("#lbl-error", Static)
            assert err_lbl is not None
            assert str(err_lbl.render()) == ""

            btn_demo = modal.query_one("#preset-demo", Button)
            btn_dl = modal.query_one("#preset-downloads", Button)
            btn_docs = modal.query_one("#preset-documents", Button)
            assert btn_demo is not None and btn_demo.tooltip is not None
            assert btn_dl is not None and btn_dl.tooltip is not None
            assert btn_docs is not None and btn_docs.tooltip is not None

            audit_res = modal.audit_a11y_compliance()
            assert audit_res["compliant"] is True, (
                f"A11y violations: {audit_res['violations']}"
            )
            assert len(inspect_tui_component(modal)) == 0

    asyncio.run(_test())


def test_wizard_modal_preset_buttons_and_announcements(tmp_path):
    """Verify selecting quick preset buttons populates input field and announces changes."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            # Focus input and verify announcement
            inp = modal.query_one("#input-dir", Input)
            inp.focus()
            await pilot.pause(0.05)
            assert any(
                "workspace directory input focused" in a["message"].lower()
                for a in modal.announcements
            )

            # Press Demo Workspace preset
            btn_demo = modal.query_one("#preset-demo", Button)
            btn_demo.press()
            await pilot.pause(0.05)

            demo_path = os.path.abspath("sandbox/demo_workspace")
            assert inp.value == demo_path
            assert os.path.isdir(demo_path)
            assert any(
                "selected demo workspace preset" in a["message"].lower()
                for a in modal.announcements
            )

            # Press Downloads preset
            btn_dl = modal.query_one("#preset-downloads", Button)
            btn_dl.press()
            await pilot.pause(0.05)
            assert inp.value == os.path.expanduser("~/Downloads")
            assert any(
                "selected downloads preset" in a["message"].lower()
                for a in modal.announcements
            )

            # Press Documents preset
            btn_docs = modal.query_one("#preset-documents", Button)
            btn_docs.press()
            await pilot.pause(0.05)
            assert inp.value == os.path.expanduser("~/Documents")
            assert any(
                "selected documents preset" in a["message"].lower()
                for a in modal.announcements
            )

    asyncio.run(_test())


def test_wizard_modal_empty_input_defaults_to_cwd_and_scans(tmp_path):
    """Verify empty input defaults safely to current working directory, updates header, and triggers scan."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir="", skip_wizard=True)

        scan_called = []
        app.action_scan_directory = lambda: scan_called.append(True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings, current_dir="")
            app.push_screen(modal)
            await pilot.pause(0.05)

            inp = modal.query_one("#input-dir", Input)
            inp.value = ""
            await pilot.pause(0.05)

            btn_finish = modal.query_one("#btn-finish", Button)
            btn_finish.press()
            await pilot.pause(0.1)

            expected_cwd = os.path.abspath(os.getcwd())
            assert app.base_dir == expected_cwd
            assert app.source_dirs == [expected_cwd]
            assert len(scan_called) == 1
            assert not isinstance(app.screen, WizardModal)

            # Verify active workspace state text
            ws_state = app.query_one("#onboarding-workspace-state", Static)
            assert expected_cwd in str(ws_state.render())

    asyncio.run(_test())


def test_wizard_modal_custom_directory_selection_sets_base_dir_and_scans(tmp_path):
    """Verify valid custom directory selection updates base_dir, workspace header, and launches initial scan."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        target_folder = tmp_path / "my_custom_workspace"
        target_folder.mkdir(parents=True, exist_ok=True)

        app = AutoSorterTUI(settings=settings, base_dir="", skip_wizard=True)

        scan_called = []
        app.action_scan_directory = lambda: scan_called.append(True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            inp = modal.query_one("#input-dir", Input)
            inp.value = str(target_folder)
            await pilot.pause(0.05)

            btn_finish = modal.query_one("#btn-finish", Button)
            btn_finish.press()
            await pilot.pause(0.1)

            assert app.base_dir == str(target_folder)
            assert app.source_dirs == [str(target_folder)]
            assert len(scan_called) == 1
            assert not isinstance(app.screen, WizardModal)

            ws_state = app.query_one("#onboarding-workspace-state", Static)
            assert str(target_folder) in str(ws_state.render())

    asyncio.run(_test())


def test_wizard_modal_invalid_directory_displays_error_and_keeps_open(tmp_path):
    """Verify invalid directory paths show inline error banner and keep the wizard open."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir="", skip_wizard=True)

        scan_called = []
        app.action_scan_directory = lambda: scan_called.append(True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            inp = modal.query_one("#input-dir", Input)
            inp.value = "/path/that/does/not/exist/at/all"
            await pilot.pause(0.05)

            btn_finish = modal.query_one("#btn-finish", Button)
            btn_finish.press()
            await pilot.pause(0.1)

            # Error shown and modal still active
            assert isinstance(app.screen, WizardModal)
            assert len(scan_called) == 0

            err_lbl = modal.query_one("#lbl-error", Static)
            err_text = str(err_lbl.render())
            assert "Target directory does not exist" in err_text

            # Changing input text clears the error
            inp.value = "/another/path"
            await pilot.pause(0.05)
            assert str(err_lbl.render()) == ""

    asyncio.run(_test())


def test_wizard_modal_sample_corpus_safe_delegation_non_empty_folder(tmp_path):
    """Verify sample document generation in non-empty folder routes to sortify-sample-dataset without overwriting existing files."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        workspace = tmp_path / "existing_user_files"
        workspace.mkdir(parents=True, exist_ok=True)

        user_doc = workspace / "critical_data.txt"
        user_doc.write_text("IMPORTANT ORIGINAL CONTENT")

        app = AutoSorterTUI(
            settings=settings, base_dir=str(workspace), skip_wizard=True
        )

        scan_called = []
        app.action_scan_directory = lambda: scan_called.append(True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings, current_dir=str(workspace))
            app.push_screen(modal)
            await pilot.pause(0.05)

            chk_sample = modal.query_one("#chk-sample-corpus", Checkbox)
            chk_sample.value = True
            await pilot.pause(0.05)

            btn_finish = modal.query_one("#btn-finish", Button)
            btn_finish.press()
            await pilot.pause(0.1)

            # Ensure user original file was NOT overwritten
            assert user_doc.read_text() == "IMPORTANT ORIGINAL CONTENT"

            # Ensure sample files were delegated to sortify-sample-dataset subfolder
            sample_subfolder = workspace / "sortify-sample-dataset"
            assert sample_subfolder.exists()
            assert (sample_subfolder / "demo_finance.txt").exists()
            assert (sample_subfolder / "demo_tech.txt").exists()

            # Base dir and scan target updated to sample subfolder
            assert app.base_dir == str(sample_subfolder)
            assert app.source_dirs == [str(sample_subfolder)]
            assert len(scan_called) == 1

    asyncio.run(_test())
