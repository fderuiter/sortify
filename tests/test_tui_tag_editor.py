"""Integration and UI unit tests for TUI sidecar tag editor modal."""

import asyncio
import json

import pytest
from textual.widgets import Input

from app.config import AppSettings
from app.core.sidecar_tags import load_sidecar_tags
from app.ui.tui import AutoSorterTUI, TagEditorModal


@pytest.mark.slow
@pytest.mark.xdist_group(name="tui")
def test_tag_editor_modal_interactive_flow(tmp_path):
    """Test pressing 't' or invoking action_edit_tags on a file node opens TagEditorModal, persists tags to sidecar, and updates UI."""

    async def _test():
        doc_path = tmp_path / "tax_report_2026.pdf"
        doc_path.touch()

        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        app.plan = {
            "Taxes": {
                "tax_report_2026.pdf": {
                    "__type__": "file",
                    "filepath": str(doc_path),
                    "target_filename": "tax_report_2026.pdf",
                    "confidence": 0.95,
                    "category": "Taxes",
                }
            }
        }

        async with app.run_test(size=(100, 30)) as pilot:
            app.rebuild_tree()
            tree = app.query_one("#plan-tree")
            # Select file node
            folder_node = tree.root.children[0]
            file_node = folder_node.children[0]
            app.active_tree_node = file_node
            app._update_inspector(file_node)

            # Invoke tag edit action
            app.action_edit_tags()
            await pilot.pause(0.1)

            assert isinstance(app.screen, TagEditorModal)
            assert app.screen.filename == "tax_report_2026.pdf"

            # Set tag input value and submit
            inp = app.screen.query_one("#tag-input")
            inp.value = "Reviewed 2026, Tax"
            await pilot.press("enter")
            await pilot.pause(0.1)

            # Verify sidecar file on disk
            sidecar_file = tmp_path / ".sortify_tags.json"
            assert sidecar_file.exists()
            tags = load_sidecar_tags(doc_path)
            assert tags == ["Reviewed 2026", "Tax"]

            # Verify inspector metadata pane text rendered tags
            meta_text = app._update_inspector(file_node)
            assert "Tags:" in meta_text
            assert "Reviewed 2026, Tax" in meta_text

    asyncio.run(_test())


@pytest.mark.slow
@pytest.mark.xdist_group(name="tui")
def test_tag_editor_modal_cancel_flow(tmp_path):
    """Test cancelling TagEditorModal does not alter sidecar storage."""

    async def _test():
        doc_path = tmp_path / "invoice.pdf"
        doc_path.touch()

        # Seed initial sidecar tag
        sidecar_file = tmp_path / ".sortify_tags.json"
        with open(sidecar_file, "w", encoding="utf-8") as f:
            json.dump({"invoice.pdf": ["OriginalTag"]}, f)

        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        app.plan = {
            "Invoices": {
                "invoice.pdf": {
                    "__type__": "file",
                    "filepath": str(doc_path),
                    "target_filename": "invoice.pdf",
                    "category": "Invoices",
                }
            }
        }

        async with app.run_test(size=(100, 30)) as pilot:
            app.rebuild_tree()
            tree = app.query_one("#plan-tree")
            folder_node = tree.root.children[0]
            file_node = folder_node.children[0]
            app.active_tree_node = file_node

            app.action_edit_tags()
            await pilot.pause(0.1)

            assert isinstance(app.screen, TagEditorModal)

            # Press Escape to cancel modal
            await pilot.press("escape")
            await pilot.pause(0.1)

            assert not isinstance(app.screen, TagEditorModal)

            # Verify sidecar tags unchanged
            assert load_sidecar_tags(doc_path) == ["OriginalTag"]

    asyncio.run(_test())


@pytest.mark.slow
@pytest.mark.xdist_group(name="tui")
def test_tag_editor_modal_display():
    """Verify TagEditorModal component attributes and input initialization."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="/dummy/workspace")

        async with app.run_test(size=(100, 30)) as pilot:
            modal = TagEditorModal(
                filename="tax_document.pdf",
                filepath="/dummy/workspace/tax_document.pdf",
                current_tags=["Reviewed 2026", "Tax"],
            )
            app.push_screen(modal)
            for _ in range(5):
                await pilot.pause()

            assert isinstance(app.screen, TagEditorModal)
            assert app.screen.filename == "tax_document.pdf"
            inp = app.screen.query_one("#tag-input", Input)
            assert inp.value == "Reviewed 2026, Tax"

    asyncio.run(_test())
