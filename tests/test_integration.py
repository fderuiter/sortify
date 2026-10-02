import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.integration import is_admin, register_context_menu

pytestmark = [pytest.mark.slow, pytest.mark.integration]


@pytest.fixture()
def mock_winreg_and_ctypes():
    """Mock winreg and ctypes modules for platform-independent testing."""
    mock_winreg = MagicMock()
    mock_winreg.HKEY_CLASSES_ROOT = "HKEY_CLASSES_ROOT"
    mock_winreg.REG_SZ = 1

    mock_shell32 = MagicMock()
    mock_shell32.IsUserAnAdmin = MagicMock(return_value=False)
    mock_shell32.ShellExecuteW = MagicMock(return_value=42)

    mock_windll = MagicMock()
    mock_windll.shell32 = mock_shell32

    # Create a clean mock for ctypes module in integration namespace
    mock_ctypes_module = MagicMock()
    mock_ctypes_module.windll = mock_windll

    class MockCtypesWrapper:
        pass

    mock_ctypes = MockCtypesWrapper()
    mock_ctypes.windll = mock_windll

    with (
        patch("app.core.integration.winreg", mock_winreg, create=True),
        patch("app.core.integration.ctypes", mock_ctypes_module),
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


def test_main_cli_directory_argument():
    """Verify that launching main() with a directory argument executes run_tui with that directory."""
    from app.main import main

    mock_args = MagicMock()
    mock_args.tui = True
    mock_args.daemon = False
    mock_args.demo = False
    mock_args.directory = "/some/test/directory"

    with (
        patch("app.main.argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("app.ui.tui.run_tui") as mock_run_tui,
        patch("app.main.AppSettings") as mock_settings_class,
    ):
        main()

        mock_run_tui.assert_called_once_with(
            mock_settings_class.return_value,
            "/some/test/directory",
            skip_wizard=False,
            non_interactive=False,
        )
