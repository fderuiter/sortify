"""Unit tests for CommandPaletteModal overlay and Ctrl+K keybinding re-assignment."""

from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Input, OptionList, Tree

from app.config import AppSettings
from app.ui.modals.command_palette import (
    DEFAULT_COMMAND_CATALOG,
    CommandPaletteModal,
)
from app.ui.modals.help import ShortcutCheatSheetModal
from app.ui.modals.models import ModelManagerModal
from app.ui.modals.rules import ExportRulesModal, RuleImportModal, TemplateGalleryModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def mock_find_abandoned_sessions(monkeypatch):
    import app.core.session_catalog

    monkeypatch.setattr(
        app.core.session_catalog, "find_abandoned_sessions", lambda *a, **k: []
    )


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
    """Verify Ctrl+K keybinding triggers action_open_command_palette and ctrl+c is not bound."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    # Check class binding definition
    ctrl_k_bindings = [
        b
        for b in AutoSorterTUI.BINDINGS
        if hasattr(b, "key") and getattr(b, "key", None) == "ctrl+k"
    ]
    assert len(ctrl_k_bindings) == 1
    assert getattr(ctrl_k_bindings[0], "action", None) == "open_command_palette"

    # Verify ctrl+c is not globally bound
    ctrl_c_bindings = [
        b for b in AutoSorterTUI.BINDINGS if getattr(b, "key", None) == "ctrl+c"
    ]
    assert len(ctrl_c_bindings) == 0

    # Verify open_cro_forensic shortcut is cleared in catalog
    cro_cmd = next(c for c in DEFAULT_COMMAND_CATALOG if c.id == "open_cro_forensic")
    assert cro_cmd.shortcut == ""

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+k")
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)


@pytest.mark.anyio
async def test_command_palette_cro_forensic_routing(tmp_path):
    """Verify CRO forensic ingest modal launches from command palette and not ctrl+c."""
    from app.plugins.clinical_compliance.tui_views import CROForensicModal

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()

        # Pressing ctrl+c should not open CROForensicModal
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, CROForensicModal)

        # Open command palette and execute CRO forensic ingestion
        app.action_open_command_palette()
        await pilot.pause()
        assert isinstance(app.screen, CommandPaletteModal)

        await pilot.press("c", "r", "o")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, CROForensicModal)


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
        assert "scan" in str(opt.prompt).lower() or (
            opt.id and "scan" in opt.id.lower()
        )


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
        assert (
            "command palette" in rendered_labels.lower()
            or "palette" in rendered_labels.lower()
        )


@pytest.mark.anyio
async def test_command_palette_catalog_contains_rule_and_daemon_entries():
    """Verify DEFAULT_COMMAND_CATALOG includes template gallery, export rules, import rules, and daemon."""
    cmd_ids = {cmd.id: cmd for cmd in DEFAULT_COMMAND_CATALOG}

    assert "open_template_gallery" in cmd_ids
    gallery_cmd = cmd_ids["open_template_gallery"]
    assert gallery_cmd.category == "Rules"
    assert (
        "template" in gallery_cmd.title.lower()
        or "template" in gallery_cmd.description.lower()
    )
    assert gallery_cmd.action_name == "action_open_template_gallery"

    assert "export_rules" in cmd_ids
    export_cmd = cmd_ids["export_rules"]
    assert export_cmd.category == "Rules"
    assert "export" in export_cmd.title.lower()
    assert export_cmd.action_name == "action_export_rules"

    assert "import_rules" in cmd_ids
    import_cmd = cmd_ids["import_rules"]
    assert import_cmd.category == "Rules"
    assert "import" in import_cmd.title.lower()
    assert import_cmd.action_name == "action_import_rules"

    assert "toggle_daemon" in cmd_ids
    daemon_cmd = cmd_ids["toggle_daemon"]
    assert daemon_cmd.category in ("System", "Workspace")
    assert "daemon" in daemon_cmd.title.lower()
    assert "Ctrl+T" in daemon_cmd.shortcut and "p" in daemon_cmd.shortcut
    assert daemon_cmd.action_name == "action_toggle_daemon"


@pytest.mark.anyio
async def test_command_palette_option_list_shows_daemon_shortcuts(tmp_path):
    """Verify daemon command renders shortcut keys next to title in OptionList."""
    modal = CommandPaletteModal()
    app = AutoSorterTUI(settings=AppSettings(), base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        lst = modal.query_one("#palette-list", OptionList)
        found_daemon_opt = False
        for i in range(lst.option_count):
            opt = lst.get_option_at_index(i)
            if opt.id == "toggle_daemon":
                found_daemon_opt = True
                prompt_str = str(opt.prompt)
                assert "Ctrl+T" in prompt_str
                assert "p" in prompt_str
                break
        assert found_daemon_opt


@pytest.mark.anyio
async def test_command_palette_launch_template_gallery(tmp_path):
    """Verify searching 'template' and pressing Enter directly opens TemplateGalleryModal with settings and restores focus on close."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)
        await pilot.press("t", "e", "m", "p", "l", "a", "t", "e")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, TemplateGalleryModal)
        assert app.screen.settings is app.settings

        # Closing modal returns focus to #plan-tree
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, TemplateGalleryModal)
        tree = app.query_one("#plan-tree", Tree)
        assert tree.has_focus


@pytest.mark.anyio
async def test_command_palette_launch_export_rules(tmp_path):
    """Verify searching 'export' and pressing Enter directly opens ExportRulesModal with settings and restores focus on close."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)
        await pilot.press("e", "x", "p", "o", "r", "t")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ExportRulesModal)
        assert app.screen.settings is app.settings

        # Closing modal returns focus to #plan-tree
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ExportRulesModal)
        tree = app.query_one("#plan-tree", Tree)
        assert tree.has_focus


@pytest.mark.anyio
async def test_command_palette_launch_import_rules(tmp_path):
    """Verify searching 'import' and pressing Enter directly opens RuleImportModal with settings and restores focus on close."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_open_command_palette()
        await pilot.pause()

        assert isinstance(app.screen, CommandPaletteModal)
        await pilot.press("i", "m", "p", "o", "r", "t")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, RuleImportModal)
        assert app.screen.settings is app.settings

        # Closing modal returns focus to #plan-tree
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, RuleImportModal)
        tree = app.query_one("#plan-tree", Tree)
        assert tree.has_focus


@pytest.mark.anyio
async def test_command_palette_toggle_daemon_async_dispatch(tmp_path):
    """Verify searching 'daemon' and pressing Enter triggers async action_toggle_daemon via worker without warning."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    fake_pid_info = {"pid": 99999, "socket_path": "/fake/socket"}
    current_mock_status = {"status": "running", "is_running": True, "is_paused": False}

    async def fake_send_command(cmd, timeout=2.0):
        if cmd == "pause":
            current_mock_status["status"] = "paused"
            current_mock_status["is_paused"] = True
            return {"status": "paused", "message": "Daemon triage pipeline paused"}
        elif cmd == "resume":
            current_mock_status["status"] = "running"
            current_mock_status["is_paused"] = False
            return {"status": "resumed", "message": "Daemon triage pipeline resumed"}
        return dict(current_mock_status)

    mock_send_command = AsyncMock(side_effect=fake_send_command)

    with (
        patch("app.core.ipc.read_pid_file", return_value=fake_pid_info),
        patch("app.core.ipc.is_process_alive", return_value=True),
        patch("app.core.ipc.DaemonIPCClient.send_command", mock_send_command),
    ):
        async with app.run_test() as pilot:
            app._daemon_state = "active"
            app.update_status()
            await pilot.pause()

            app.action_open_command_palette()
            await pilot.pause()

            await pilot.press("d", "a", "e", "m", "o", "n")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            # Command palette dismissed
            assert not isinstance(app.screen, CommandPaletteModal)

            # Wait for worker coroutine to finish execution
            for _ in range(10):
                if app._daemon_state == "paused":
                    break
                await pilot.pause(delay=0.1)

            assert app._daemon_state == "paused"
            mock_send_command.assert_any_await("pause", timeout=2.0)
            assert any(
                "Watchdog daemon paused." in a["message"]
                and a["priority"] == "assertive"
                for a in app.announcements
            )


@pytest.mark.anyio
async def test_tui_direct_rule_actions(tmp_path):
    """Verify direct action methods on AutoSorterTUI open rule modals with settings."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()

        # Template gallery action
        app.action_open_template_gallery()
        await pilot.pause()
        assert isinstance(app.screen, TemplateGalleryModal)
        assert app.screen.settings is app.settings
        await pilot.press("escape")
        await pilot.pause()

        # Export rules action
        app.action_export_rules()
        await pilot.pause()
        assert isinstance(app.screen, ExportRulesModal)
        assert app.screen.settings is app.settings
        await pilot.press("escape")
        await pilot.pause()

        # Import rules action
        app.action_import_rules()
        await pilot.pause()
        assert isinstance(app.screen, RuleImportModal)
        assert app.screen.settings is app.settings
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.anyio
async def test_model_manager_ctrl_g_binding_and_catalog_entry(tmp_path):
    """Verify Ctrl+G opens ModelManagerModal directly and is listed in palette and cheat sheet."""
    cmd = next(c for c in DEFAULT_COMMAND_CATALOG if c.id == "open_model_manager")
    assert cmd.shortcut == "Ctrl+G"

    ctrl_g = next((b for b in AutoSorterTUI.BINDINGS if b.key == "ctrl+g"), None)
    assert ctrl_g is not None
    assert ctrl_g.action == "open_model_manager"
    assert ctrl_g.show is False

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = False
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert isinstance(app.screen, ModelManagerModal)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, ShortcutCheatSheetModal)
        rendered_labels = " ".join(
            str(lbl.render()) for lbl in app.screen.query("Label")
        )
        assert "Ctrl+G" in rendered_labels
        assert "Model Manager" in rendered_labels
