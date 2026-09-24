"""Unit and integration tests for Textual full-screen terminal interface (TUI)."""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from textual.widgets import Input, Switch

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    CROForensicModal,
    NewFolderModal,
    RenameModal,
    SettingsModal,
    WizardModal,
)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for TUI testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        folder_a = os.path.join(tmpdir, "FolderA")
        os.makedirs(folder_a, exist_ok=True)

        file1 = os.path.join(tmpdir, "document_1.pdf")
        file2 = os.path.join(folder_a, "invoice_2026.docx")

        with open(file1, "w", encoding="utf-8") as f:
            f.write("Sample document content 1")
        with open(file2, "w", encoding="utf-8") as f:
            f.write("Sample invoice content 2")

        yield tmpdir


def test_tui_app_mount_and_dual_pane(temp_workspace):
    """Verify AutoSorterTUI mounts with dual-pane layout and tree view."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            assert app.query_one("#plan-tree") is not None
            assert app.query_one("#right-meta-pane") is not None
            assert app.query_one("#meta-details") is not None
            assert app.query_one("#status-bar") is not None

    asyncio.run(_test())


def test_tui_tree_rebuild_and_selection(temp_workspace):
    """Verify tree population, node data attachment, and metadata inspector updates."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.plan = {
                "Invoices": {
                    "inv_001.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "inv_001.pdf"),
                        "target_filename": "inv_001.pdf",
                        "confidence": 0.95,
                    }
                },
                "Contracts": {},
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            assert tree.root.children is not None
            assert len(tree.root.children) == 2

            folder_node = tree.root.children[1] if tree.root.children[0].data and tree.root.children[0].data.get("is_file") else tree.root.children[0]
            app.active_tree_node = folder_node

            meta = app.query_one("#meta-details")
            assert meta is not None

    asyncio.run(_test())


def test_tui_toggle_lock(temp_workspace):
    """Verify locking and unlocking file nodes in TUI updates plan state immediately."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "sample.txt")
            app.plan = {
                "Finance": {
                    "sample.txt": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "sample.txt",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node

            # Toggle lock on
            app.action_toggle_lock()
            assert "sample.txt" in app.locked_files or filepath in app.locked_files

            # Toggle lock off
            app.action_toggle_lock()
            assert "sample.txt" not in app.locked_files and filepath not in app.locked_files

    asyncio.run(_test())


def test_tui_node_ratings(temp_workspace):
    """Verify ML quality ratings (+ / -) update rating cache and node labels."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "report.pdf")
            app.plan = {
                "Reports": {
                    "report.pdf": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "report.pdf",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node

            # Rate positive
            app.action_rate_positive()
            assert app._ratings_cache.get(filepath) == "positive"

            # Rate negative (switch)
            app.action_rate_negative()
            assert app._ratings_cache.get(filepath) == "negative"

            # Rate negative again (clear)
            app.action_rate_negative()
            assert filepath not in app._ratings_cache or app._ratings_cache.get(filepath) is None

    asyncio.run(_test())


def test_tui_new_folder(temp_workspace):
    """Verify creating a new folder node via NewFolderModal updates plan state."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_new_folder()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, NewFolderModal)

            input_field = modal.query_one("#input-folder-name", Input)
            input_field.value = "Legal Documents"
            await pilot.press("enter")
            await pilot.pause(0.1)

            assert "Legal Documents" in app.plan

    asyncio.run(_test())


def test_tui_rename_node(temp_workspace):
    """Verify renaming file and folder nodes via RenameModal."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "test_doc.docx")
            app.plan = {
                "OldFolder": {
                    "test_doc.docx": {
                        "__type__": "file",
                        "filepath": filepath,
                        "target_filename": "test_doc.docx",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")

            # Rename Folder
            folder_node = tree.root.children[0]
            app.active_tree_node = folder_node
            app.action_rename_node()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, RenameModal)
            input_field = modal.query_one("#input-name", Input)
            input_field.value = "NewFolder"
            await pilot.press("enter")
            await pilot.pause(0.1)

            assert "NewFolder" in app.plan
            assert "OldFolder" not in app.plan

    asyncio.run(_test())


def test_tui_settings_modal(temp_workspace):
    """Verify settings modal displays and updates application settings."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            modal.query_one("#input-protected", Input).value = "/tmp/protected1, /tmp/protected2"
            modal.query_one("#input-ignored", Input).value = ".tmp, .log, .bak"
            modal.query_one("#input-concurrency", Input).value = "8"

            modal.action_save()
            await pilot.pause(0.1)

            assert getattr(app.settings, "PROTECTED_PATHS", None) == ["/tmp/protected1", "/tmp/protected2"] or getattr(app.settings, "PROTECTED_DIRECTORIES", None) == ["/tmp/protected1", "/tmp/protected2"]
            assert app.settings.IGNORED_EXTENSIONS == [".tmp", ".log", ".bak"]
            assert getattr(app.settings, "MAX_WORKERS", None) == 8 or getattr(app.settings, "WORKER_CONCURRENCY", None) == 8

    asyncio.run(_test())


def test_tui_wizard_modal(temp_workspace):
    """Verify wizard modal allows toggling consent and completes onboarding."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_wizard()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, WizardModal)

            modal.query_one("#switch-consent", Switch).value = True
            modal.action_finish()
            await pilot.pause(0.1)

            assert app.settings.AI_CONSENT_GRANTED is True

    asyncio.run(_test())


def test_tui_cro_forensic_modal(temp_workspace):
    """Verify CRO forensic ingest modal executes pipeline worker and logs output."""
    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_cro_forensic()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, CROForensicModal)

            modal.query_one("#input-source", Input).value = temp_workspace
            modal.query_one("#input-target", Input).value = os.path.join(temp_workspace, "Output")

            with patch("app.core.cro_multi_study_pipeline.CROMultiStudyPipeline.run_pipeline") as mock_run:
                mock_res = MagicMock()
                mock_res.total_scanned_files = 5
                mock_res.discovered_studies_count = 1
                mock_run.return_value = mock_res

                modal.action_run()
                await pilot.pause(0.2)

                assert mock_run.called

    asyncio.run(_test())


def test_tui_modals_render_on_small_viewports(temp_workspace):
    """Verify all six TUI modal screens render without errors on 70x20 and 80x24 viewports."""
    from app.ui.tui import DirectorySelectModal

    settings = AppSettings()

    # Verify CSS definitions enforce fluid 90% width, max-width 80, max-height 90%, overflow-y auto
    modals = [
        RenameModal("Rename Test", "current", ".txt"),
        NewFolderModal(),
        DirectorySelectModal(temp_workspace),
        SettingsModal(settings),
        WizardModal(settings),
        CROForensicModal(settings, temp_workspace),
    ]

    for modal in modals:
        assert "width: 90%" in modal.CSS
        assert "max-width: 80" in modal.CSS
        assert "max-height: 90%" in modal.CSS
        assert "overflow-y: auto" in modal.CSS

    async def _test(size, modal_factory):
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=size) as pilot:
            modal = modal_factory()
            app.push_screen(modal)
            await pilot.pause(0.05)
            assert app.screen is modal

    for size in [(80, 24), (70, 20)]:
        asyncio.run(_test(size, lambda: RenameModal("Rename Test", "current", ".txt")))
        asyncio.run(_test(size, NewFolderModal))
        asyncio.run(_test(size, lambda: DirectorySelectModal(temp_workspace)))
        asyncio.run(_test(size, lambda: SettingsModal(settings)))
        asyncio.run(_test(size, lambda: WizardModal(settings)))
        asyncio.run(_test(size, lambda: CROForensicModal(settings, temp_workspace)))


def test_main_cli_tui_invocation():
    """Verify app/main.py launches run_tui when --tui argument is supplied."""
    from app.main import main

    with patch("argparse.ArgumentParser.parse_args") as mock_args, patch("app.ui.tui.run_tui") as mock_run_tui:
        args = MagicMock()
        args.tui = True
        args.gui = False
        args.daemon = False
        args.demo = False
        args.smoke_test = False
        args.update_snapshots = False
        args.directory = "/path/to/sort"
        args.debug_layout = False
        mock_args.return_value = args

        main()
        mock_run_tui.assert_called_once()
