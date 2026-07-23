import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.integration import is_admin, register_context_menu
from app.ui.app import AutoSorterApp, run_app
from app.ui.settings import show_settings


@pytest.fixture(autouse=True)
def mock_winreg_and_ctypes():
    """Mock winreg and ctypes modules for platform-independent testing."""
    mock_winreg = MagicMock()
    mock_winreg.HKEY_CLASSES_ROOT = "HKEY_CLASSES_ROOT"
    mock_winreg.REG_SZ = 1

    mock_ctypes = MagicMock()
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = False
    mock_ctypes.windll.shell32.ShellExecuteW.return_value = 42

    with (
        patch.dict(sys.modules, {"winreg": mock_winreg, "ctypes": mock_ctypes}),
        patch("app.core.verifier.check_ai_status", return_value=(True, None)),
    ):
        yield mock_winreg, mock_ctypes


def test_non_windows_platform_guardrail():
    """Verify that running register_context_menu on non-Windows platforms correctly raises OSError."""
    with patch("sys.platform", "linux"):
        with pytest.raises(OSError) as excinfo:
            register_context_menu(enable=True)
        assert "Context menu integration is only available on Windows." in str(
            excinfo.value
        )


def test_is_admin_on_non_windows():
    """Verify is_admin returns False on non-Windows systems."""
    with patch("sys.platform", "linux"):
        assert is_admin() is False


def test_is_admin_on_windows_true(mock_winreg_and_ctypes):
    """Verify is_admin returns True on Windows when ctypes says so."""
    _, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = True

    with patch("sys.platform", "win32"):
        assert is_admin() is True


def test_is_admin_on_windows_false(mock_winreg_and_ctypes):
    """Verify is_admin returns False on Windows when ctypes says so."""
    _, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = False

    with patch("sys.platform", "win32"):
        assert is_admin() is False


def test_windows_admin_escalation(mock_winreg_and_ctypes):
    """Verify that calling register_context_menu attempts standard OS privilege escalation if not admin."""
    _, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = False

    with patch("sys.platform", "win32"):
        # Calling register_context_menu should trigger ShellExecuteW
        res = register_context_menu(enable=True)
        assert res is True
        mock_ctypes.windll.shell32.ShellExecuteW.assert_called_once()

        args = mock_ctypes.windll.shell32.ShellExecuteW.call_args[0]
        assert args[1] == "runas"
        assert sys.executable in args[2]
        assert "enable" in args[3]


def test_windows_admin_escalation_failure(mock_winreg_and_ctypes):
    """Verify that register_context_menu raises RuntimeError if ShellExecuteW fails (returns <= 32)."""
    _, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = False
    mock_ctypes.windll.shell32.ShellExecuteW.return_value = (
        5  # Failure return code <= 32
    )

    with patch("sys.platform", "win32"):
        with pytest.raises(RuntimeError) as excinfo:
            register_context_menu(enable=True)
        assert "Failed to elevate privileges" in str(excinfo.value)


def test_windows_admin_registry_operations_enable_packaged(mock_winreg_and_ctypes):
    """Verify registry keys creation under both Directory and Directory\\Background when enable is True (packaged)."""
    mock_winreg, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = True

    # We mock CreateKey to return fake key handles
    mock_key_dir = MagicMock()
    mock_key_bg = MagicMock()
    mock_key_dir_cmd = MagicMock()
    mock_key_bg_cmd = MagicMock()

    # To trace properly, we'll let CreateKey return structured handles based on path
    def mock_create_key(root, path):
        if root == mock_key_bg or (isinstance(path, str) and "Background" in path):
            if "command" in path:
                return mock_key_bg_cmd
            return mock_key_bg
        else:
            if "command" in path:
                return mock_key_dir_cmd
            return mock_key_dir

    mock_winreg.CreateKey.side_effect = mock_create_key

    with (
        patch("sys.platform", "win32"),
        patch("app.core.path_utils.is_packaged", return_value=True),
    ):
        register_context_menu(enable=True)

        # Verify winreg.CreateKey was called for directory and background and commands
        mock_winreg.CreateKey.assert_any_call(
            "HKEY_CLASSES_ROOT", r"Directory\shell\SmartAutoSorter"
        )
        mock_winreg.CreateKey.assert_any_call(mock_key_dir, "command")
        mock_winreg.CreateKey.assert_any_call(
            "HKEY_CLASSES_ROOT", r"Directory\Background\shell\SmartAutoSorter"
        )
        mock_winreg.CreateKey.assert_any_call(mock_key_bg, "command")

        # Verify SetValue was called on handles to set prog_name and command
        mock_winreg.SetValue.assert_any_call(
            mock_key_dir, "", 1, "Open in Smart Auto-Sorter"
        )
        mock_winreg.SetValue.assert_any_call(
            mock_key_bg, "", 1, "Open in Smart Auto-Sorter"
        )

        # Check command formats for packaged app
        expected_dir_cmd = f'"{sys.executable}" "%1"'
        expected_bg_cmd = f'"{sys.executable}" "%V"'
        mock_winreg.SetValue.assert_any_call(mock_key_dir_cmd, "", 1, expected_dir_cmd)
        mock_winreg.SetValue.assert_any_call(mock_key_bg_cmd, "", 1, expected_bg_cmd)


def test_windows_admin_registry_operations_enable_unpackaged(mock_winreg_and_ctypes):
    """Verify registry keys creation under both paths when enable is True (unpackaged script)."""
    mock_winreg, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = True

    with (
        patch("sys.platform", "win32"),
        patch("app.core.path_utils.is_packaged", return_value=False),
    ):
        register_context_menu(enable=True)

        # Verify SetValue on command handles includes main.py
        set_value_args = [c[0] for c in mock_winreg.SetValue.call_args_list]
        commands_set = [
            args[3] for args in set_value_args if "%1" in args[3] or "%V" in args[3]
        ]

        assert len(commands_set) == 2
        assert any("main.py" in cmd and "%1" in cmd for cmd in commands_set)
        assert any("main.py" in cmd and "%V" in cmd for cmd in commands_set)


def test_windows_admin_registry_operations_disable(mock_winreg_and_ctypes):
    """Verify registry keys deletion under both Directory and Directory\\Background when enable is False."""
    mock_winreg, mock_ctypes = mock_winreg_and_ctypes
    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = True

    with patch("sys.platform", "win32"):
        register_context_menu(enable=False)

        # Verify DeleteKey was called for both keys and their commands
        mock_winreg.DeleteKey.assert_any_call(
            "HKEY_CLASSES_ROOT", r"Directory\shell\SmartAutoSorter\command"
        )
        mock_winreg.DeleteKey.assert_any_call(
            "HKEY_CLASSES_ROOT", r"Directory\shell\SmartAutoSorter"
        )
        mock_winreg.DeleteKey.assert_any_call(
            "HKEY_CLASSES_ROOT", r"Directory\Background\shell\SmartAutoSorter\command"
        )
        mock_winreg.DeleteKey.assert_any_call(
            "HKEY_CLASSES_ROOT", r"Directory\Background\shell\SmartAutoSorter"
        )


def test_settings_toggle_on_explorer_integration_non_windows():
    """Verify the UI toggle rejects context integration on non-Windows with a warning."""
    parent_app = MagicMock()
    settings = AppSettings()
    settings.EXPLORER_INTEGRATION = False

    with patch("app.ui.settings.ui") as mock_ui, patch("sys.platform", "linux"):
        show_settings(parent_app, settings)

        # Find the switch call for context menu
        switch_on_change = None
        for call_args in mock_ui.switch.call_args_list:
            args, kwargs = call_args
            if "Explorer" in args[0] or "Context Menu" in args[0]:
                switch_on_change = kwargs.get("on_change")
                break

        assert switch_on_change is not None

        # Simulate toggling to True
        mock_sender = MagicMock()
        mock_sender.value = True
        mock_event = MagicMock()
        mock_event.value = True
        mock_event.sender = mock_sender

        switch_on_change(mock_event)

        # Verify warning notification
        mock_ui.notify.assert_called_once_with(
            "Context menu integration is only available on Windows.", type="warning"
        )
        assert mock_sender.value is False
        assert settings.EXPLORER_INTEGRATION is False


def test_settings_toggle_on_explorer_integration_windows_success(
    mock_winreg_and_ctypes,
):
    """Verify the UI toggle registers correctly on Windows when enabled."""
    parent_app = MagicMock()
    settings = AppSettings()
    settings.EXPLORER_INTEGRATION = False

    with (
        patch("app.ui.settings.ui") as mock_ui,
        patch("sys.platform", "win32"),
        patch("app.core.integration.is_admin", return_value=True),
    ):
        show_settings(parent_app, settings)

        # Find the switch call
        switch_on_change = None
        for call_args in mock_ui.switch.call_args_list:
            args, kwargs = call_args
            if "Explorer" in args[0] or "Context Menu" in args[0]:
                switch_on_change = kwargs.get("on_change")
                break

        assert switch_on_change is not None

        # Simulate toggling to True
        mock_sender = MagicMock()
        mock_sender.value = True
        mock_event = MagicMock()
        mock_event.value = True
        mock_event.sender = mock_sender

        switch_on_change(mock_event)

        mock_ui.notify.assert_called_once_with(
            "Explorer integration updated successfully.", type="positive"
        )
        assert settings.EXPLORER_INTEGRATION is True


def test_settings_toggle_on_explorer_integration_windows_failure(
    mock_winreg_and_ctypes,
):
    """Verify the UI toggle safely reverts and notifies on failure."""
    parent_app = MagicMock()
    settings = AppSettings()
    settings.EXPLORER_INTEGRATION = False

    with (
        patch("app.ui.settings.ui") as mock_ui,
        patch("sys.platform", "win32"),
        patch(
            "app.core.integration.register_context_menu",
            side_effect=RuntimeError("Elevation refused"),
        ),
    ):
        show_settings(parent_app, settings)

        # Find the switch call
        switch_on_change = None
        for call_args in mock_ui.switch.call_args_list:
            args, kwargs = call_args
            if "Explorer" in args[0] or "Context Menu" in args[0]:
                switch_on_change = kwargs.get("on_change")
                break

        assert switch_on_change is not None

        # Simulate toggling to True
        mock_sender = MagicMock()
        mock_sender.value = True
        mock_event = MagicMock()
        mock_event.value = True
        mock_event.sender = mock_sender

        switch_on_change(mock_event)

        # Verify negative notification and revert
        mock_ui.notify.assert_called_once_with(
            "Failed to update Explorer integration: Elevation refused", type="negative"
        )
        assert mock_sender.value is False
        assert settings.EXPLORER_INTEGRATION is False


def test_run_app_directory_preload():
    """Verify that run_app resolves directory to absolute path and sets it on the app instance."""
    with (
        patch("app.ui.app.ui"),
        patch("app.ui.app.AutoSorterApp") as mock_app_class,
        patch("os.path.exists", return_value=True),
    ):
        settings = AppSettings()
        run_app(settings, "some_dir")

        # Verify instantiation
        mock_app_class.assert_called_once_with(settings)
        # Verify base_dir set on the created instance
        inst = mock_app_class.return_value
        assert inst.base_dir == os.path.abspath("some_dir")


def test_autosorterapp_build_ui_schedules_analysis():
    """Verify that if base_dir is set on the app, build_ui schedules start_analysis via a timer."""
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.base_dir = "/mock/dir"

    with (
        patch("app.ui.app.ui") as mock_ui,
        patch("app.ui.app.AutoSorterApp.check_setup_wizard"),
        patch("app.ui.app.AutoSorterApp.check_abandoned_sessions"),
    ):
        app.build_ui()

        # Verify that ui.timer was called to schedule start_analysis
        timer_calls = mock_ui.timer.call_args_list
        found = False
        for args, kwargs in timer_calls:
            if len(args) > 1 and args[1] == app.start_analysis:
                found = True
                assert kwargs.get("once") is True
                break
        assert found, "ui.timer was not called with app.start_analysis"


def test_main_cli_directory_argument():
    """Verify that launching main() with a directory argument executes run_app with that directory."""
    from app.main import main

    mock_args = MagicMock()
    mock_args.demo = False
    mock_args.directory = "/some/test/directory"

    with (
        patch("app.main.argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("app.ui.app.run_app") as mock_run_app,
        patch("app.main.AppSettings") as mock_settings_class,
    ):
        main()

        mock_run_app.assert_called_once_with(
            mock_settings_class.return_value, "/some/test/directory"
        )
