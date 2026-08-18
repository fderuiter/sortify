import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.integration import is_admin, register_context_menu
from app.ui.app import AutoSorterApp, run_app
from app.ui.settings import show_settings


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
        patch("app.ui.app.os.path.exists", return_value=True),
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


def test_settings_protected_paths_ui():
    """Verify that protected directories list manager behaves correctly in the UI."""
    parent_app = MagicMock()
    settings = AppSettings()
    settings.PROTECTED_PATHS = ["/var/protected_one"]

    with patch("app.ui.settings.ui") as mock_ui:
        show_settings(parent_app, settings)

        # Let's find the add_protected_path function inside settings view
        # We can locate it by searching the mock_ui.button calls for "Add" or similar
        add_btn_callback = None
        for call_args in mock_ui.button.call_args_list:
            args, kwargs = call_args
            if len(args) > 0 and args[0] == "Add" and "on_click" in kwargs:
                add_btn_callback = kwargs["on_click"]
                break

        assert add_btn_callback is not None

        # Let's simulate entering a valid path
        mock_input = mock_ui.input.return_value.props.return_value
        mock_input.value = "/var/protected_two"

        # Click the add button
        add_btn_callback()

        # The path should be added
        assert "/var/protected_two" in settings.PROTECTED_PATHS
        mock_ui.notify.assert_any_call(
            "Protected path added: /var/protected_two", type="positive"
        )

        # Let's try adding an invalid path (e.g. relative path)
        mock_input.value = "relative/path"
        mock_ui.notify.reset_mock()
        add_btn_callback()

        # It should show error notification, and settings shouldn't contain relative/path
        assert "relative/path" not in settings.PROTECTED_PATHS
        assert mock_ui.notify.call_count >= 1
        last_call_args, last_call_kwargs = mock_ui.notify.call_args
        assert last_call_kwargs.get("type") == "negative"


def test_advanced_settings_sliders():
    """Verify that advanced setting sliders exist and propagate change events with validation."""
    parent_app = MagicMock()
    settings = AppSettings()

    # Set known values
    settings.MAX_WORKERS = 4
    settings.VISUAL_TIMEOUT = 30
    settings.MODEL_THREADS = 2
    settings.IMAGE_MAX_DIMENSION = 1000
    settings.IMAGE_SKIP_THRESHOLD = 3000
    settings.DEBOUNCE_DELAY = 0.6
    settings.MAX_DEBOUNCE_DELAY = 5.0
    settings.COHERENCE_THRESHOLD = 0.5

    with patch("app.ui.settings.ui") as mock_ui:
        show_settings(parent_app, settings)

        # We expect exactly 8 sliders
        assert mock_ui.slider.call_count == 8

        # Locate sliders based on min/max bounds
        sliders_by_bounds = {}
        for call_args in mock_ui.slider.call_args_list:
            args, kwargs = call_args
            bounds = (kwargs.get("min"), kwargs.get("max"))
            sliders_by_bounds[bounds] = kwargs

        # 1. MAX_WORKERS: 1 to 64
        assert (1, 64) in sliders_by_bounds
        worker_kwargs = sliders_by_bounds[(1, 64)]
        assert worker_kwargs.get("value") == 4
        on_worker_change = worker_kwargs.get("on_change")
        assert on_worker_change is not None

        # 2. MODEL_THREADS: 1 to 32
        assert (1, 32) in sliders_by_bounds
        threads_kwargs = sliders_by_bounds[(1, 32)]
        assert threads_kwargs.get("value") == 2
        on_threads_change = threads_kwargs.get("on_change")
        assert on_threads_change is not None

        # 3. VISUAL_TIMEOUT: 1 to 300
        assert (1, 300) in sliders_by_bounds
        timeout_kwargs = sliders_by_bounds[(1, 300)]
        assert timeout_kwargs.get("value") == 30
        on_timeout_change = timeout_kwargs.get("on_change")
        assert on_timeout_change is not None

        # 4. IMAGE_MAX_DIMENSION: 1 to 5000
        assert (1, 5000) in sliders_by_bounds
        dim_kwargs = sliders_by_bounds[(1, 5000)]
        assert dim_kwargs.get("value") == 1000
        on_dim_change = dim_kwargs.get("on_change")
        assert on_dim_change is not None

        # 5. IMAGE_SKIP_THRESHOLD: 1 to 10000
        assert (1, 10000) in sliders_by_bounds
        skip_kwargs = sliders_by_bounds[(1, 10000)]
        assert skip_kwargs.get("value") == 3000
        on_skip_change = skip_kwargs.get("on_change")
        assert on_skip_change is not None

        # 6. DEBOUNCE_DELAY: 0.1 to 10.0
        assert (0.1, 10.0) in sliders_by_bounds
        debounce_kwargs = sliders_by_bounds[(0.1, 10.0)]
        assert debounce_kwargs.get("value") == 0.6
        on_debounce_change = debounce_kwargs.get("on_change")
        assert on_debounce_change is not None

        # 7. MAX_DEBOUNCE_DELAY: 0.5 to 30.0
        assert (0.5, 30.0) in sliders_by_bounds
        max_debounce_kwargs = sliders_by_bounds[(0.5, 30.0)]
        assert max_debounce_kwargs.get("value") == 5.0
        on_max_debounce_change = max_debounce_kwargs.get("on_change")
        assert on_max_debounce_change is not None

        # 8. COHERENCE_THRESHOLD: 0.0 to 1.0
        assert (0.0, 1.0) in sliders_by_bounds
        coherence_kwargs = sliders_by_bounds[(0.0, 1.0)]
        assert coherence_kwargs.get("value") == 0.5
        on_coherence_change = coherence_kwargs.get("on_change")
        assert on_coherence_change is not None

        # Test valid changes
        # Max Workers change
        mock_event = MagicMock()
        mock_event.value = 16
        on_worker_change(mock_event)
        assert settings.MAX_WORKERS == 16

        # ML Threads change
        mock_event.value = 8
        on_threads_change(mock_event)
        assert settings.MODEL_THREADS == 8

        # Timeout change
        mock_event.value = 45
        on_timeout_change(mock_event)
        assert settings.VISUAL_TIMEOUT == 45

        # Image Dimension change
        mock_event.value = 1200
        on_dim_change(mock_event)
        assert settings.IMAGE_MAX_DIMENSION == 1200

        # Image Skip Threshold change
        mock_event.value = 4000
        on_skip_change(mock_event)
        assert settings.IMAGE_SKIP_THRESHOLD == 4000

        # Debounce Delay change
        mock_event.value = 1.5
        on_debounce_change(mock_event)
        assert settings.DEBOUNCE_DELAY == 1.5

        # Max Debounce Delay change
        mock_event.value = 10.0
        on_max_debounce_change(mock_event)
        assert settings.MAX_DEBOUNCE_DELAY == 10.0

        # Coherence Threshold change
        mock_event.value = 0.75
        on_coherence_change(mock_event)
        assert settings.COHERENCE_THRESHOLD == 0.75

        # Test invalid values trigger revert-and-notify
        # Invalid Max Workers (e.g. 100 which is > 64)
        mock_sender = MagicMock()
        mock_event.sender = mock_sender
        mock_event.value = 100
        mock_ui.notify.reset_mock()
        on_worker_change(mock_event)
        # Should be reverted to 16
        assert settings.MAX_WORKERS == 16
        assert mock_sender.value == 16
        assert mock_ui.notify.call_count >= 1
        last_call_args, last_call_kwargs = mock_ui.notify.call_args
        assert last_call_kwargs.get("type") == "negative"
        assert (
            "workers" in last_call_args[0].lower()
            or "validation" in last_call_args[0].lower()
        )

        # Invalid ML Threads (e.g. 50 which is > 32)
        mock_sender = MagicMock()
        mock_event.sender = mock_sender
        mock_event.value = 50
        mock_ui.notify.reset_mock()
        on_threads_change(mock_event)
        # Should be reverted to 8
        assert settings.MODEL_THREADS == 8
        assert mock_sender.value == 8
        assert mock_ui.notify.call_count >= 1
        last_call_args, last_call_kwargs = mock_ui.notify.call_args
        assert last_call_kwargs.get("type") == "negative"
        assert (
            "threads" in last_call_args[0].lower()
            or "validation" in last_call_args[0].lower()
        )

        # Invalid Debounce Delay (e.g. -1.0 which is <= 0.0)
        mock_sender = MagicMock()
        mock_event.sender = mock_sender
        mock_event.value = -1.0
        mock_ui.notify.reset_mock()
        on_debounce_change(mock_event)
        # Should be reverted to 1.5
        assert settings.DEBOUNCE_DELAY == 1.5
        assert mock_sender.value == 1.5
        assert mock_ui.notify.call_count >= 1
        last_call_args, last_call_kwargs = mock_ui.notify.call_args
        assert last_call_kwargs.get("type") == "negative"
        assert (
            "debounce delay" in last_call_args[0].lower()
            or "validation" in last_call_args[0].lower()
        )

        # Invalid Max Debounce Delay (e.g. -1.0 which is <= 0.0)
        mock_sender = MagicMock()
        mock_event.sender = mock_sender
        mock_event.value = -1.0
        mock_ui.notify.reset_mock()
        on_max_debounce_change(mock_event)
        # Should be reverted to 10.0
        assert settings.MAX_DEBOUNCE_DELAY == 10.0
        assert mock_sender.value == 10.0
        assert mock_ui.notify.call_count >= 1
        last_call_args, last_call_kwargs = mock_ui.notify.call_args
        assert last_call_kwargs.get("type") == "negative"
        assert (
            "max debounce delay" in last_call_args[0].lower()
            or "validation" in last_call_args[0].lower()
        )


def test_debounce_sliders_auto_adjustment():
    """Test real-time slider auto-adjustment when slider values cross bounds."""
    parent_app = MagicMock()
    settings = AppSettings()
    settings.DEBOUNCE_DELAY = 1.0
    settings.MAX_DEBOUNCE_DELAY = 5.0

    with patch("app.ui.settings.ui") as mock_ui:
        show_settings(parent_app, settings)

        sliders = {}
        for call_args in mock_ui.slider.call_args_list:
            _, kwargs = call_args
            bounds = (kwargs.get("min"), kwargs.get("max"))
            sliders[bounds] = kwargs

        on_debounce_change = sliders[(0.1, 10.0)]["on_change"]
        on_max_debounce_change = sliders[(0.5, 30.0)]["on_change"]

        # 1. Slide MIN debounce delay above MAX debounce delay (e.g., 8.0 when MAX is 5.0)
        mock_event = MagicMock()
        mock_event.value = 8.0
        mock_ui.notify.reset_mock()

        on_debounce_change(mock_event)

        assert settings.DEBOUNCE_DELAY == 8.0
        assert settings.MAX_DEBOUNCE_DELAY == 8.0
        for call in mock_ui.notify.call_args_list:
            assert call.kwargs.get("type") != "negative"

        # 2. Slide MAX debounce delay below MIN debounce delay (e.g., 2.0 when MIN is 8.0)
        mock_event.value = 2.0
        mock_ui.notify.reset_mock()

        on_max_debounce_change(mock_event)

        assert settings.DEBOUNCE_DELAY == 2.0
        assert settings.MAX_DEBOUNCE_DELAY == 2.0
        for call in mock_ui.notify.call_args_list:
            assert call.kwargs.get("type") != "negative"

        # 3. Non-overlapping update
        mock_event.value = 1.0
        on_debounce_change(mock_event)
        assert settings.DEBOUNCE_DELAY == 1.0
        assert settings.MAX_DEBOUNCE_DELAY == 2.0

