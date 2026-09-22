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
