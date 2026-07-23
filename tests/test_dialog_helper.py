import sys
from unittest.mock import MagicMock, call, patch

import pytest

from app.ui.dialog_helper import ask_directory_async


def mock_thread_start(self):
    """Run the thread target synchronously for testing."""
    self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def run_threads_synchronously():
    with patch("threading.Thread.start", mock_thread_start):
        yield


@pytest.fixture
def mock_filedialog():
    # Save original modules
    orig_tk = sys.modules.get("tkinter")
    orig_fd = sys.modules.get("tkinter.filedialog")

    mock_tk_mod = MagicMock()
    mock_fd_mod = MagicMock()
    mock_tk_mod.filedialog = mock_fd_mod

    sys.modules["tkinter"] = mock_tk_mod
    sys.modules["tkinter.filedialog"] = mock_fd_mod

    yield mock_fd_mod

    # Restore
    if orig_tk is not None:
        sys.modules["tkinter"] = orig_tk
    elif "tkinter" in sys.modules:
        del sys.modules["tkinter"]

    if orig_fd is not None:
        sys.modules["tkinter.filedialog"] = orig_fd
    elif "tkinter.filedialog" in sys.modules:
        del sys.modules["tkinter.filedialog"]


def test_ask_directory_async_success_with_parent():
    parent = MagicMock()
    callback = MagicMock()
    disable_ui = MagicMock()
    enable_ui = MagicMock()

    mock_result = MagicMock()
    mock_result.stdout = "SUCCESS:/mock/selected/path\n"

    with (
        patch(
            "app.ui.dialog_helper.run_background_process", return_value=mock_result
        ) as mock_run,
        patch("sys.platform", "darwin"),
    ):
        ask_directory_async(parent, "Select Folder", callback, disable_ui, enable_ui)

        # UI should be disabled initially
        disable_ui.assert_called_once()

        # Check background process was launched
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "osascript" in cmd
        assert "Select Folder" in cmd[4]

        # Since parent exists and has after, it should schedule using parent.after
        parent.after.assert_called_once()
        after_func = parent.after.call_args[0][1]

        # Call the scheduled completion callback and ensure UI is enabled and callback invoked
        after_func()
        enable_ui.assert_called_once()
        callback.assert_called_once_with("/mock/selected/path")


def test_ask_directory_async_success_no_parent():
    callback = MagicMock()
    disable_ui = MagicMock()
    enable_ui = MagicMock()

    mock_result = MagicMock()
    mock_result.stdout = "SUCCESS:/mock/selected/path\n"

    with (
        patch(
            "app.ui.dialog_helper.run_background_process", return_value=mock_result
        ) as mock_run,
        patch("sys.platform", "win32"),
    ):
        ask_directory_async(None, "Choose Directory", callback, disable_ui, enable_ui)

        disable_ui.assert_called_once()
        mock_run.assert_called_once()

        # Callback and enable_ui should be invoked immediately since there is no parent/scheduler
        enable_ui.assert_called_once()
        callback.assert_called_once_with("/mock/selected/path")


def test_ask_directory_async_fallback_with_parent(mock_filedialog):
    parent = MagicMock()
    callback = MagicMock()
    disable_ui = MagicMock()
    enable_ui = MagicMock()

    mock_filedialog.askdirectory.return_value = "/mock/fallback/path"

    # Force platform command exception to trigger fallback
    with (
        patch(
            "app.ui.dialog_helper.run_background_process",
            side_effect=Exception("Failed"),
        ),
        patch("sys.platform", "darwin"),
    ):
        ask_directory_async(parent, "Fallback Select", callback, disable_ui, enable_ui)

        # It should schedule the fallback callback
        parent.after.assert_called_once()
        fallback_func = parent.after.call_args[0][1]

        # Execute fallback function
        fallback_func()

        # Check topmost and focus were manipulated
        parent.attributes.assert_has_calls(
            [call("-topmost", True), call("-topmost", False)]
        )
        parent.focus_force.assert_called_once()

        mock_filedialog.askdirectory.assert_called_once_with(
            parent=parent, title="Fallback Select"
        )
        enable_ui.assert_called_once()
        callback.assert_called_once_with("/mock/fallback/path")


def test_ask_directory_async_fallback_no_parent(mock_filedialog):
    callback = MagicMock()
    disable_ui = MagicMock()
    enable_ui = MagicMock()

    mock_filedialog.askdirectory.return_value = "/mock/fallback/path"

    # Force platform command exception to trigger fallback
    with (
        patch(
            "app.ui.dialog_helper.run_background_process",
            side_effect=Exception("Failed"),
        ),
        patch("sys.platform", "win32"),
    ):
        ask_directory_async(None, "Fallback Select", callback, disable_ui, enable_ui)

        # Callback and enable_ui should be invoked directly since there is no parent/scheduler
        mock_filedialog.askdirectory.assert_called_once_with(
            parent=None, title="Fallback Select"
        )
        enable_ui.assert_called_once()
        callback.assert_called_once_with("/mock/fallback/path")


def test_ask_directory_async_fallback_exception(mock_filedialog):
    callback = MagicMock()
    disable_ui = MagicMock()
    enable_ui = MagicMock()

    mock_filedialog.askdirectory.side_effect = Exception("Tk Error")

    # Mock askdirectory to throw an exception to verify robustness
    with (
        patch(
            "app.ui.dialog_helper.run_background_process",
            side_effect=Exception("Failed"),
        ),
        patch("sys.platform", "linux"),
    ):
        ask_directory_async(
            None, "Fallback Error Test", callback, disable_ui, enable_ui
        )

        mock_filedialog.askdirectory.assert_called_once_with(
            parent=None, title="Fallback Error Test"
        )
        enable_ui.assert_called_once()
        callback.assert_called_once_with("")
