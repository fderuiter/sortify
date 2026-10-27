"""Unit tests for CommandPaletteModal overlay and Ctrl+K keybinding re-assignment."""


import pytest
from textual.widgets import Input, OptionList

from app.config import AppSettings
from app.ui.modals.command_palette import (
    DEFAULT_COMMAND_CATALOG,
    CommandPaletteModal,
)
from app.ui.modals.help import ShortcutCheatSheetModal
from app.ui.modals.models import ModelManagerModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI


@pytest.mark.anyio
async def test_command_palette_modal_mount_and_widgets(tmp_path):
    """Verify CommandPaletteModal mounts with input field, list, and status label."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)
        modal = app.screen
        inp = modal.query_one("#palette-input", Input)
        lst = modal.query_one("#palette-list", OptionList)

        assert inp.has_focus
        assert lst.option_count == len(DEFAULT_COMMAND_CATALOG)


@pytest.mark.anyio
async def test_command_palette_ctrl_k_binding(tmp_path):
    """Verify Ctrl+K keybinding triggers action_open_command_palette."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    # Check class binding definition
    ctrl_k_bindings = [b for b in AutoSorterTUI.BINDINGS if b.key == "ctrl+k"]
    assert len(ctrl_k_bindings) == 1
    assert ctrl_k_bindings[0].action == "open_command_palette"

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)


@pytest.mark.anyio
async def test_command_palette_substring_search_filtering(tmp_path):
    """Verify typing query strings dynamically filters commands in real time."""
    modal = CommandPaletteModal()
    app = AutoSorterTUI(settings=AppSettings(), base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        lst = modal.query_one("#palette-list", OptionList)
        initial_count = lst.option_count

        await pilot.press("s", "c", "a", "n")
        await pilot.pause()
        filtered_count = lst.option_count

        assert filtered_count < initial_count
        assert filtered_count >= 1

        # Check option label contains 'Scan'
        opt = lst.get_option_at_index(0)
        assert "scan" in str(opt.prompt).lower() or "scan" in opt.id.lower()


@pytest.mark.anyio
async def test_command_palette_execute_action_via_enter(tmp_path):
    """Verify pressing Enter on highlighted palette option triggers corresponding action."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        # Type "settings" to filter down to Open Settings
        await pilot.press("s", "e", "t", "t", "i", "n", "g", "s")
        await pilot.pause()

        # Press Enter to select
        await pilot.press("enter")
        await pilot.pause()

        # CommandPaletteModal should be dismissed and SettingsModal opened
        assert isinstance(app.screen, SettingsModal)


@pytest.mark.anyio
async def test_command_palette_access_model_manager(tmp_path):
    """Verify searching 'model' in command palette launches ModelManagerModal."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        await pilot.press("m", "o", "d", "e", "l")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ModelManagerModal)


@pytest.mark.anyio
async def test_command_palette_escape_dismissal(tmp_path):
    """Verify pressing Escape closes command palette without taking action."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, CommandPaletteModal)


@pytest.mark.anyio
async def test_command_palette_arrow_navigation(tmp_path):
    """Verify Up/Down arrow keys move selection in the option list."""
    modal = CommandPaletteModal()
    app = AutoSorterTUI(settings=AppSettings(), base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        lst = modal.query_one("#palette-list", OptionList)
        assert lst.highlighted == 0

        await pilot.press("down")
        await pilot.pause()
        assert lst.highlighted == 1

        await pilot.press("up")
        await pilot.pause()
        assert lst.highlighted == 0


@pytest.mark.anyio
async def test_shortcut_cheat_sheet_displays_command_palette_label():
    """Verify shortcut cheat sheet displays Ctrl+K for Command Palette Overlay."""
    sheet = ShortcutCheatSheetModal()
    app = AutoSorterTUI(settings=AppSettings())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(sheet)
        await pilot.pause()

        rendered_labels = " ".join([str(lbl.render()) for lbl in sheet.query("Label")])
        assert "Ctrl+K" in rendered_labels
        assert "command palette" in rendered_labels.lower() or "palette" in rendered_labels.lower()
