"""Unit and integration tests for target destination folder path override modal in TUI preview and move execution."""

import asyncio
import os

from textual.widgets import Input

from app.config import AppSettings
from app.core.mover import execute_moves
from app.core.verifier import VerificationEngine
from app.ui.tui import AutoSorterTUI, DestinationOverrideModal


def test_destination_override_modal_unit(tmp_path):
    """Verify DestinationOverrideModal rendering, input confirmation, and cancellation."""
    temp_workspace = str(tmp_path)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        modal = DestinationOverrideModal(
            title="Override Destination Test",
            file_key="invoice.pdf",
            current_path="Invoices",
        )
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause(0.1)

            active_modal = app.screen
            assert isinstance(active_modal, DestinationOverrideModal)

            inp = active_modal.query_one("#input-destination-path", Input)
            assert inp.value == "Invoices"

            inp.value = "Finance/Invoices/2024"
            await pilot.press("enter")
            await pilot.pause(0.1)

    asyncio.run(_test())


def test_tui_override_destination_action(tmp_path):
    """Verify action_override_destination updates target path and plan structure."""
    temp_workspace = str(tmp_path)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "report.pdf")
            with open(filepath, "w") as f:
                f.write("test content")

            app.plan = {
                "Reports": {
                    "report.pdf": {
                        "__type__": "file",
                        "filepath": filepath,
                        "relative_source": "report.pdf",
                        "target_filename": "report.pdf",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            folder_node = tree.root.children[0]
            file_node = folder_node.children[0]
            app.active_tree_node = file_node

            # Trigger action_override_destination via action / shortcut
            app.action_override_destination()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, DestinationOverrideModal)
            input_field = modal.query_one("#input-destination-path", Input)
            assert input_field.value == "Reports"

            # Set custom target folder path override
            custom_path = "Custom/Target/Folder"
            input_field.value = custom_path
            await pilot.press("enter")
            await pilot.pause(0.1)

            # Check plan updated
            assert custom_path in app.plan
            assert "report.pdf" in app.plan[custom_path]
            node_info = app.plan[custom_path]["report.pdf"]
            assert node_info.get("destination_override") == custom_path
            assert node_info.get("target_folder") == custom_path
            assert node_info.get("is_locked") is True
            assert node_info.get("is_overridden") is True

            # Verify VerificationEngine.get_moves reflects custom_path
            moves = VerificationEngine.get_moves(temp_workspace, app.plan)
            assert len(moves) == 1
            key, src, dst = moves[0]
            expected_dst = os.path.normpath(
                os.path.join(temp_workspace, custom_path, "report.pdf")
            )
            assert os.path.normpath(dst) == expected_dst

            # Verify mover.execute_moves moves physical file to custom destination folder
            summary = execute_moves(temp_workspace, app.plan)
            assert summary.get("cancelled") is False
            assert os.path.exists(expected_dst)

    asyncio.run(_test())


def test_tui_override_destination_cancel(tmp_path):
    """Verify canceling DestinationOverrideModal leaves plan unchanged."""
    temp_workspace = str(tmp_path)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            filepath = os.path.join(temp_workspace, "data.csv")
            app.plan = {
                "Data": {
                    "data.csv": {
                        "__type__": "file",
                        "filepath": filepath,
                        "relative_source": "data.csv",
                        "target_filename": "data.csv",
                    }
                }
            }
            app.rebuild_tree()

            tree = app.query_one("#plan-tree")
            folder_node = tree.root.children[0]
            file_node = folder_node.children[0]
            app.active_tree_node = file_node

            app.action_override_destination()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, DestinationOverrideModal)
            await pilot.press("escape")
            await pilot.pause(0.1)

            # Plan should be unchanged
            assert "Data" in app.plan
            assert "data.csv" in app.plan["Data"]
            assert "destination_override" not in app.plan["Data"]["data.csv"]

    asyncio.run(_test())
