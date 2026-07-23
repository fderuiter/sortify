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

        # Check that background process ran
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "osascript" in args[0]

        # Check that callback was triggered with the correct path
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
    mock_result.stdout = "/mock/linux/zenity"
    mock_run.return_value = mock_result

    mock_which = mock.MagicMock(
        side_effect=lambda cmd: "/usr/bin/zenity" if cmd == "zenity" else None
    )

    callback = mock.MagicMock()

    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("shutil.which", mock_which),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "zenity" in args[0]
        assert "--directory" in args[0]

        callback.assert_called_once_with("/mock/linux/zenity")


@pytest.mark.anyio
async def test_ask_directory_async_linux_kdialog():
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "/mock/linux/kdialog"
    mock_run.return_value = mock_result

    mock_which = mock.MagicMock(
        side_effect=lambda cmd: "/usr/bin/kdialog" if cmd == "kdialog" else None
    )

    callback = mock.MagicMock()

    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("shutil.which", mock_which),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "kdialog" in args[0]

        callback.assert_called_once_with("/mock/linux/kdialog")


@pytest.mark.anyio
async def test_ask_directory_async_linux_none_fallback():
    # If no native CLI tools are available on Linux, verify we trigger NiceGUI manual path entry
    mock_which = mock.MagicMock(return_value=None)
    callback = mock.MagicMock()

    mock_ui = mock.MagicMock()
    mock_ui.dialog.return_value.__enter__.return_value = mock.MagicMock()

    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("shutil.which", mock_which),
        mock.patch("nicegui.ui", mock_ui),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        # Verify that NiceGUI ui components (like notifying or dialog) were invoked
        mock_ui.notify.assert_called_with(
            "No native CLI directory picker (Zenity or KDialog) was found. Please install Zenity or KDialog.",
            type="warning",
        )
        assert mock_ui.dialog.called


@pytest.mark.anyio
async def test_ask_directory_async_windows_restricted_fallback():
    # If Windows powershell selection fails (restricted execution), verify manual dialog trigger
    mock_run = mock.MagicMock()
    mock_result = mock.MagicMock()
    mock_result.stdout = ""  # empty output on restriction/error
    mock_run.return_value = mock_result

    callback = mock.MagicMock()
    mock_ui = mock.MagicMock()
    mock_ui.dialog.return_value.__enter__.return_value = mock.MagicMock()

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.ui.dialog_helper.run_background_process", mock_run),
        mock.patch("nicegui.ui", mock_ui),
    ):
        ask_directory_async(None, "Select Folder", callback, None, None)
        await asyncio.sleep(0.1)

        mock_ui.notify.assert_called_with(
            "Could not open native folder picker (PowerShell may be restricted). Please enter path manually.",
            type="warning",
        )
        assert mock_ui.dialog.called
