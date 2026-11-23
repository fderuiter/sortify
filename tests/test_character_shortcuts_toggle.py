"""Tests for character shortcut toggle and tree focus scoping (WCAG 2.1.4)."""

import asyncio
import os
import tempfile

import pytest
from pydantic import ValidationError
from textual.widgets import Input, Switch, Tree

from app.config import AppSettings, Settings
from app.ui.modals.help import ShortcutCheatSheetModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI
from app.ui.widgets.vim_tree import VimTree


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for TUI testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        folder_a = os.path.join(tmpdir, "FolderA")
        os.makedirs(folder_a, exist_ok=True)
        file1 = os.path.join(tmpdir, "document_1.pdf")
        file2 = os.path.join(folder_a, "invoice_2026.docx")
        with open(file1, "w") as f:
            f.write("content 1")
        with open(file2, "w") as f:
            f.write("content 2")
        yield tmpdir


def test_settings_model_validates_enable_character_key_shortcuts():
    """Criterion 1: The Settings model validates ENABLE_CHARACTER_KEY_SHORTCUTS as a boolean with default True."""
    # Default is True
    settings = Settings()
    assert settings.ENABLE_CHARACTER_KEY_SHORTCUTS is True

    # Explicit False
    s_false = Settings(ENABLE_CHARACTER_KEY_SHORTCUTS=False)
    assert s_false.ENABLE_CHARACTER_KEY_SHORTCUTS is False

    # Explicit True
    s_true = Settings(ENABLE_CHARACTER_KEY_SHORTCUTS=True)
    assert s_true.ENABLE_CHARACTER_KEY_SHORTCUTS is True

    # Pydantic bool validation accepts valid bool-coercible values
    s_coerced = Settings(ENABLE_CHARACTER_KEY_SHORTCUTS="0")
    assert s_coerced.ENABLE_CHARACTER_KEY_SHORTCUTS is False

    # Invalid type raises ValidationError
    with pytest.raises(ValidationError):
        Settings(ENABLE_CHARACTER_KEY_SHORTCUTS="invalid_not_a_bool")


def test_settings_modal_exposes_toggle_switch_and_saves(temp_workspace):
    """Criterion 2: The settings dialog exposes a toggle switch that updates ENABLE_CHARACTER_KEY_SHORTCUTS."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    assert settings.ENABLE_CHARACTER_KEY_SHORTCUTS is True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(120, 60)) as pilot:
            results = []
            modal = SettingsModal(settings)
            app.push_screen(modal, results.append)
            await pilot.pause(0.1)

            # Query switch control via either selector
            switch = modal.query_one("#switch-character-key-shortcuts", Switch)
            assert switch is not None
            assert switch.value is True

            # Also verify alias query works
            switch_alias = modal.query_one(
                "#switch-enable-character-key-shortcuts", Switch
            )
            assert switch_alias is switch

            # Toggle switch off
            switch.value = False
            await pilot.pause(0.05)

            # Check announcement on switch change (Requirement 5)
            assert any(
                "Character key shortcuts disabled." in str(a)
                for a in modal.announcements
            )

            # Save settings dialog
            modal.action_save()
            await pilot.pause(0.1)

            assert len(results) == 1
            assert results[0]["ENABLE_CHARACTER_KEY_SHORTCUTS"] is False

    asyncio.run(_test())


def test_key_bindings_do_not_fire_when_focus_outside_tree(temp_workspace):
    """Criterion 3: The key bindings do not fire character actions when focus is outside the tree widget."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings._settings_model.ENABLE_CHARACTER_KEY_SHORTCUTS = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        # Create a sample plan with checked files
        app.plan = {
            "Documents": {
                "sample.txt": {
                    "__type__": "file",
                    "filepath": os.path.join(temp_workspace, "sample.txt"),
                    "target_filename": "sample.txt",
                    "is_checked": True,
                    "is_excluded": False,
                }
            }
        }
        with open(os.path.join(temp_workspace, "sample.txt"), "w") as f:
            f.write("content")

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node

            # Move focus away from tree to the search input
            search_input = app.query_one("#tree-search-input", Input)
            search_input.focus()
            await pilot.pause(0.05)

            assert app._tree_has_focus() is False
            assert tree.has_focus is False

            # Space key in search input types space instead of toggling selection
            await pilot.press("space")
            await pilot.pause(0.05)
            assert file_node.data["info"]["is_checked"] is True
            assert search_input.value == " "

            # 'x' key in search input types 'x' instead of excluding checked files
            await pilot.press("x")
            await pilot.pause(0.05)
            assert file_node.data["info"]["is_excluded"] is False
            assert search_input.value == " x"

            # '?' key in search input types '?' instead of opening cheat sheet
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)
            assert search_input.value == " x?"

    asyncio.run(_test())


def test_tree_widget_ignores_character_shortcuts_when_disabled(temp_workspace):
    """Criterion 4: VimTree ignores character shortcuts when ENABLE_CHARACTER_KEY_SHORTCUTS is False."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings._settings_model.ENABLE_CHARACTER_KEY_SHORTCUTS = False

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        app.plan = {
            "Documents": {
                "file_a.txt": {
                    "__type__": "file",
                    "filepath": os.path.join(temp_workspace, "file_a.txt"),
                    "target_filename": "file_a.txt",
                    "is_checked": True,
                    "is_excluded": False,
                },
                "file_b.txt": {
                    "__type__": "file",
                    "filepath": os.path.join(temp_workspace, "file_b.txt"),
                    "target_filename": "file_b.txt",
                    "is_checked": True,
                    "is_excluded": False,
                },
            }
        }
        with open(os.path.join(temp_workspace, "file_a.txt"), "w") as f:
            f.write("a")
        with open(os.path.join(temp_workspace, "file_b.txt"), "w") as f:
            f.write("b")

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", VimTree)
            tree.focus()
            await pilot.pause(0.05)

            assert app._tree_has_focus() is True
            doc_folder = tree.root.children[0]
            doc_folder.expand()
            await pilot.pause(0.05)
            file_node = doc_folder.children[0]
            app.active_tree_node = file_node

            # Pressing space while shortcuts disabled does NOT toggle selection
            await pilot.press("space")
            await pilot.pause(0.05)
            assert file_node.data["info"]["is_checked"] is True

            # Pressing 'x' does NOT exclude
            await pilot.press("x")
            await pilot.pause(0.05)
            assert file_node.data["info"]["is_excluded"] is False

            # Pressing 'a' does NOT deselect all
            await pilot.press("a")
            await pilot.pause(0.05)
            assert file_node.data["info"]["is_checked"] is True

            # Pressing '?' does NOT open cheat sheet
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

            # Vim navigation keys j, k, h, l STILL WORK when shortcuts are disabled
            initial_cursor = tree.cursor_node
            await pilot.press("j")
            await pilot.pause(0.05)
            # Cursor moved down
            assert tree.cursor_node != initial_cursor

    asyncio.run(_test())


def test_ctrl_alt_k_toggles_shortcuts_dynamically_and_announces(temp_workspace):
    """Criterion 5: Pressing ctrl+alt+k toggles the shortcut state dynamically and announces the new state."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings.ENABLE_CHARACTER_KEY_SHORTCUTS = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)
            assert app.settings.ENABLE_CHARACTER_KEY_SHORTCUTS is True

            # Press ctrl+alt+k to disable
            await pilot.press("ctrl+alt+k")
            await pilot.pause(0.05)

            assert app.settings.ENABLE_CHARACTER_KEY_SHORTCUTS is False
            assert "Character key shortcuts disabled." in app.get_last_announcement()

            # Press ctrl+alt+k again to re-enable
            await pilot.press("ctrl+alt+k")
            await pilot.pause(0.05)

            assert app.settings.ENABLE_CHARACTER_KEY_SHORTCUTS is True
            assert "Character key shortcuts enabled." in app.get_last_announcement()

    asyncio.run(_test())


def test_help_dialog_documents_toggle_control(temp_workspace):
    """Criterion 6: The help dialog documents the toggle control (Ctrl+Alt+K)."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            app.action_open_cheat_sheet()
            await pilot.pause(0.05)

            assert isinstance(app.screen, ShortcutCheatSheetModal)
            labels = [str(lbl.render()) for lbl in app.screen.query(".shortcut-row")]
            all_text = " ".join(labels)
            assert "Ctrl+Alt+K" in all_text

    asyncio.run(_test())


def test_f1_always_opens_cheat_sheet_regardless_of_shortcuts_state(temp_workspace):
    """Requirement 6: F1 continues to open cheat sheet even when character shortcuts are disabled."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings.ENABLE_CHARACTER_KEY_SHORTCUTS = False

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            # Even with character shortcuts disabled and tree focused:
            await pilot.press("f1")
            await pilot.pause(0.05)

            assert isinstance(app.screen, ShortcutCheatSheetModal)

            # Dismiss
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

            # Focus search input (outside tree)
            search_input = app.query_one("#tree-search-input", Input)
            search_input.focus()
            await pilot.pause(0.05)
            assert app._tree_has_focus() is False

            # F1 from outside tree still opens cheat sheet
            await pilot.press("f1")
            await pilot.pause(0.05)
            assert isinstance(app.screen, ShortcutCheatSheetModal)

    asyncio.run(_test())


def test_all_modal_subclasses_define_question_mark_and_f1_bindings():
    """Verify every ModalScreen subclass in app.ui and app.plugins binds question_mark and f1 with show=False."""
    import importlib
    import pkgutil

    from textual.binding import Binding
    from textual.screen import ModalScreen

    import app.plugins.clinical_compliance.tui_views  # noqa: F401
    import app.ui.modals as modals_pkg

    for mod_info in pkgutil.iter_modules(modals_pkg.__path__):
        importlib.import_module(f"app.ui.modals.{mod_info.name}")

    def _collect_subclasses(cls: type) -> set[type]:
        found: set[type] = set()
        for sub in cls.__subclasses__():
            found.add(sub)
            found.update(_collect_subclasses(sub))
        return found

    app_modals = [
        cls
        for cls in _collect_subclasses(ModalScreen)
        if cls.__module__.startswith(("app.ui.", "app.plugins."))
    ]
    assert len(app_modals) >= 33

    for modal_cls in app_modals:
        bindings = {
            b.key: b
            for b in getattr(modal_cls, "BINDINGS", [])
            if isinstance(b, Binding)
        }
        assert "question_mark" in bindings, (
            f"{modal_cls.__name__} missing question_mark binding"
        )
        assert "f1" in bindings, f"{modal_cls.__name__} missing f1 binding"
        assert bindings["question_mark"].show is False
        assert bindings["f1"].show is False


def test_f1_and_question_mark_across_non_text_controls_and_modals(temp_workspace):
    """Verify ? opens cheat sheet on non-text controls (main screen and modals) while Input/Select block ? and allow F1."""
    from textual.widgets import Button, Select

    from app.ui.modals.command_palette import CommandPaletteModal
    from app.ui.modals.reports import ExportReportModal

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings.ENABLE_CHARACTER_KEY_SHORTCUTS = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause(0.05)

            # 1. Non-tree Button on main screen opens cheat sheet via ?
            main_buttons = [b for b in app.screen.query(Button) if b.display]
            assert main_buttons
            main_buttons[0].focus()
            await pilot.pause(0.05)
            assert app._tree_has_focus() is False
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert isinstance(app.screen, ShortcutCheatSheetModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ShortcutCheatSheetModal)

            # 2. Inside CommandPaletteModal: Input focused blocks ? (types '?') and allows F1
            palette = CommandPaletteModal()
            app.push_screen(palette)
            await pilot.pause(0.05)
            palette_inp = palette.query_one("#palette-input", Input)
            palette_inp.focus()
            await pilot.pause(0.05)

            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert app.screen is palette
            assert palette_inp.value == "?"

            await pilot.press("f1")
            await pilot.pause(0.05)
            assert isinstance(app.screen, ShortcutCheatSheetModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app.screen is palette

            await pilot.press("escape")
            await pilot.pause(0.05)

            # 3. Inside ExportReportModal: Select focused (closed & expanded) blocks ? and allows F1; Button focused allows ?
            export_modal = ExportReportModal()
            app.push_screen(export_modal)
            await pilot.pause(0.05)

            sel = export_modal.query_one("#select-export-format", Select)
            sel.focus()
            await pilot.pause(0.05)
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert app.screen is export_modal

            # Expand Select dropdown so SelectOverlay holds focus
            await pilot.press("enter")
            await pilot.pause(0.05)
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert app.screen is export_modal

            # F1 still opens cheat sheet while Select is expanded
            await pilot.press("f1")
            await pilot.pause(0.05)
            assert isinstance(app.screen, ShortcutCheatSheetModal)
            await pilot.press("escape")
            await pilot.pause(0.05)

            # Focus Cancel button on modal -> ? opens cheat sheet, Escape returns to modal
            btn_cancel = export_modal.query_one("#btn-cancel", Button)
            btn_cancel.focus()
            await pilot.pause(0.05)
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            assert isinstance(app.screen, ShortcutCheatSheetModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert app.screen is export_modal

    asyncio.run(_test())
