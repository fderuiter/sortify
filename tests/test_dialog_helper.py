import asyncio
from unittest import mock

import pytest

from app.ui.dialog_helper import ask_directory_async


@pytest.mark.anyio
async def test_ask_directory_async_macos():
    # Test macOS logic
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.stdout = "SUCCESS:/mock/mac/path"
    mock_run.return_value = mock_result

    callback = mock.MagicMock()

    with (
        mock.patch("sys.platform", "darwin"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "osascript" in args[0]
        callback.assert_called_once_with("/mock/mac/path")


@pytest.mark.anyio
async def test_ask_directory_async_windows_success():
    # Test Windows logic with successful PowerShell picker
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.stdout = "SUCCESS:C:\\mock\\win\\path"
    mock_run.return_value = mock_result

    callback = mock.MagicMock()

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "powershell" in args[0]
        callback.assert_called_once_with("C:\\mock\\win\\path")


@pytest.mark.anyio
async def test_ask_directory_async_linux_zenity():
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "/mock/linux/path"
    mock_run.return_value = mock_result

    callback = mock.MagicMock()

    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("shutil.which", return_value="/usr/bin/zenity"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_run.assert_called_once()
        callback.assert_called_once_with("/mock/linux/path")


@pytest.mark.anyio
async def test_ask_directory_async_fallback_os_error(tmp_path):
    """Test fallback modal invocation when native process execution raises OSError."""
    mock_run = mock.MagicMock(side_effect=OSError("Native picker command failed"))
    callback = mock.MagicMock()
    enable_ui = mock.MagicMock()
    disable_ui = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_dialog = mock.MagicMock()
    mock_ui.dialog.return_value = mock_dialog

    mock_input = mock.MagicMock()
    mock_input.classes.return_value = mock_input
    mock_input.props.return_value = mock_input
    mock_input.value = str(tmp_path)
    mock_ui.input.return_value = mock_input

    mock_card = mock.MagicMock()
    mock_card.classes.return_value = mock_card
    mock_ui.card.return_value = mock_card

    with (
        mock.patch("sys.platform", "darwin"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
        mock.patch("app.ui.dialog_helper.ui", mock_ui),
    ):
        ask_directory_async(
            None,
            "Select Directory Title",
            callback,
            disable_ui_callback=disable_ui,
            enable_ui_callback=enable_ui,
        )
        await asyncio.sleep(0.1)

        disable_ui.assert_called_once()
        mock_ui.dialog.assert_called_once()
        mock_ui.input.assert_called_once()
        _, input_kwargs = mock_ui.input.call_args
        assert input_kwargs.get("label") == "Enter Directory Path"
        mock_input.props.assert_called()
        props_arg = mock_input.props.call_args[0][0]
        assert 'aria-label="Enter Directory Path"' in props_arg


@pytest.mark.anyio
async def test_ask_directory_async_fallback_non_zero_status():
    """Test fallback modal invocation when native picker returns non-zero status."""
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.returncode = 2  # Non-zero error exit code
    mock_result.stdout = "Error occurred"
    mock_run.return_value = mock_result

    callback = mock.MagicMock()
    enable_ui = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_dialog = mock.MagicMock()
    mock_ui.dialog.return_value = mock_dialog

    mock_input = mock.MagicMock()
    mock_input.classes.return_value = mock_input
    mock_input.props.return_value = mock_input
    mock_ui.input.return_value = mock_input

    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("shutil.which", return_value="/usr/bin/zenity"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
        mock.patch("app.ui.dialog_helper.ui", mock_ui),
    ):
        ask_directory_async(
            None,
            "Select Folder",
            callback,
            enable_ui_callback=enable_ui,
        )
        await asyncio.sleep(0.1)

        mock_ui.dialog.assert_called_once()
        mock_ui.input.assert_called_once()


def test_render_fallback_dialog_submit_valid(tmp_path):
    """Test submitting a valid directory path through the fallback dialog."""
    callback = mock.MagicMock()
    enable_ui = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_dialog = mock.MagicMock()
    mock_ui.dialog.return_value = mock_dialog

    mock_input = mock.MagicMock()
    mock_input.classes.return_value = mock_input
    mock_input.props.return_value = mock_input
    mock_input.value = str(tmp_path)
    mock_ui.input.return_value = mock_input

    confirm_callback = None

    def mock_button(text, on_click=None):
        nonlocal confirm_callback
        btn = mock.MagicMock()
        btn.classes.return_value = btn
        btn.props.return_value = btn
        if text == "Confirm":
            confirm_callback = on_click
        return btn

    mock_ui.button.side_effect = mock_button

    from app.ui.dialog_helper import _render_fallback_dialog

    with mock.patch("app.ui.dialog_helper.ui", mock_ui):
        _render_fallback_dialog("Test Title", callback, enable_ui)
        assert confirm_callback is not None
        confirm_callback()

        callback.assert_called_once_with(str(tmp_path))
        enable_ui.assert_called_once()


def test_render_fallback_dialog_validation_empty_and_invalid(tmp_path):
    """Test inline validation for empty and non-existent directory inputs."""
    callback = mock.MagicMock()
    enable_ui = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_dialog = mock.MagicMock()
    mock_ui.dialog.return_value = mock_dialog

    mock_input = mock.MagicMock()
    mock_input.classes.return_value = mock_input
    mock_input.props.return_value = mock_input
    mock_ui.input.return_value = mock_input

    labels = []

    def mock_label(text=""):
        lbl = mock.MagicMock()
        lbl.classes.return_value = lbl
        lbl.props.return_value = lbl
        labels.append((text, lbl))
        return lbl

    mock_ui.label.side_effect = mock_label

    confirm_callback = None

    def mock_button(text, on_click=None):
        nonlocal confirm_callback
        btn = mock.MagicMock()
        btn.classes.return_value = btn
        btn.props.return_value = btn
        if text == "Confirm":
            confirm_callback = on_click
        return btn

    mock_ui.button.side_effect = mock_button

    from app.ui.dialog_helper import _render_fallback_dialog

    with mock.patch("app.ui.dialog_helper.ui", mock_ui):
        _render_fallback_dialog("Test Title", callback, enable_ui)

        error_lbl = labels[2][1]  # 3rd label is error_label (title=0, alert=1, error=2)

        # 1. Test empty path
        mock_input.value = ""
        confirm_callback()
        error_lbl.set_text.assert_called_with("Directory path cannot be empty.")
        callback.assert_not_called()

        # 2. Test non-existent path
        mock_input.value = str(tmp_path / "non_existent_subdir")
        confirm_callback()
        error_lbl.set_text.assert_called_with("The specified path does not exist.")
        callback.assert_not_called()


def test_render_fallback_dialog_cancel():
    """Test canceling the fallback dialog."""
    callback = mock.MagicMock()
    enable_ui = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_dialog = mock.MagicMock()
    mock_ui.dialog.return_value = mock_dialog

    cancel_callback = None

    def mock_button(text, on_click=None):
        nonlocal cancel_callback
        btn = mock.MagicMock()
        btn.classes.return_value = btn
        btn.props.return_value = btn
        if text == "Cancel":
            cancel_callback = on_click
        return btn

    mock_ui.button.side_effect = mock_button

    from app.ui.dialog_helper import _render_fallback_dialog

    with mock.patch("app.ui.dialog_helper.ui", mock_ui):
        _render_fallback_dialog("Test Title", callback, enable_ui)
        assert cancel_callback is not None
        cancel_callback()

        callback.assert_called_once_with("")
        enable_ui.assert_called_once()


@pytest.mark.anyio
async def test_ask_directory_async_windows_cancel_multiline():
    """Test Windows PowerShell dialog returning multiline output containing CANCEL."""
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.stdout = "GAC Assembly Load Warning\nCANCEL:\n"
    mock_run.return_value = mock_result

    callback = mock.MagicMock()

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_run.assert_called_once()
        callback.assert_called_once_with("")


@pytest.mark.anyio
async def test_ask_directory_async_fallback_render_exception():
    """Test fallback dialog gracefully handling UI render exception by calling callbacks."""
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "Error"
    mock_run.return_value = mock_result

    enable_ui = mock.MagicMock()

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
        mock.patch(
            "app.ui.dialog_helper.ui.dialog",
            side_effect=RuntimeError("No page context"),
        ),
    ):
        fut = ask_directory_async(
            None,
            "Select Folder",
            enable_ui_callback=enable_ui,
        )
        res = await asyncio.wait_for(fut, timeout=1.0)
        assert res == ""
        enable_ui.assert_called_once()


@pytest.mark.anyio
async def test_ask_directory_async_fallback_nicegui_unavailable():
    """Test fallback when NiceGUI is unavailable (_NICEGUI_AVAILABLE=False and dummy ui)."""
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "Error"
    mock_run.return_value = mock_result

    enable_ui = mock.MagicMock()

    from app.ui.dialog_helper import _dummy_ui

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
        mock.patch("app.ui.dialog_helper._NICEGUI_AVAILABLE", False),
        mock.patch("app.ui.dialog_helper.ui", _dummy_ui),
    ):
        fut = ask_directory_async(
            None,
            "Select Folder",
            enable_ui_callback=enable_ui,
        )
        res = await asyncio.wait_for(fut, timeout=1.0)
        assert res == ""
        enable_ui.assert_called_once()


@pytest.mark.anyio
async def test_ask_directory_async_windows_timeout():
    """Test Windows PowerShell dialog execution timing out cleanly."""
    import subprocess

    mock_run = mock.MagicMock(
        side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=15)
    )
    enable_ui = mock.MagicMock()

    from app.ui.dialog_helper import _dummy_ui

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
        mock.patch("app.ui.dialog_helper._NICEGUI_AVAILABLE", False),
        mock.patch("app.ui.dialog_helper.ui", _dummy_ui),
    ):
        fut = ask_directory_async(
            None,
            "Select Folder",
            enable_ui_callback=enable_ui,
        )
        res = await asyncio.wait_for(fut, timeout=1.0)
        assert res == ""
        enable_ui.assert_called_once()
        mock_run.assert_called_once()





