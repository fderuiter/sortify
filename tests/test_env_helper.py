import os
from unittest import mock

from app.config import get_app_dir
from app.core.env_helper import (
    get_cleaned_env,
    get_subprocess_startupinfo,
    run_background_process,
    spawn_background_process,
)


def test_get_cleaned_env_defaults():
    # Setup test environment with PyInstaller variables
    test_env = {
        "PATH": "/usr/bin",
        "_MEIPASS": "/tmp/_MEI12345",
        "PYTHONPATH": "/tmp/frozen_app",
    }

    with mock.patch.dict(os.environ, test_env, clear=True):
        cleaned = get_cleaned_env()

        # Ensure _MEIPASS is removed
        assert "_MEIPASS" not in cleaned
        # Ensure the path contains other variables
        assert cleaned["PATH"] == "/usr/bin"
        # Ensure PYTHONPATH now points to the local cache directory
        expected_cache = str(get_app_dir() / "cache")
        assert cleaned["PYTHONPATH"] == expected_cache


def test_get_cleaned_env_custom_dict():
    input_env = {
        "SOME_VAR": "value",
        "_MEIPASS": "abc",
        "PYTHONPATH": "xyz",
    }

    cleaned = get_cleaned_env(input_env)

    assert "_MEIPASS" not in cleaned
    assert cleaned["SOME_VAR"] == "value"
    assert cleaned["PYTHONPATH"] == str(get_app_dir() / "cache")


def test_get_subprocess_startupinfo_windows():
    mock_startupinfo_class = mock.MagicMock()
    mock_startupinfo_instance = mock.MagicMock()
    mock_startupinfo_instance.dwFlags = 0
    mock_startupinfo_instance.wShowWindow = 0
    mock_startupinfo_class.return_value = mock_startupinfo_instance

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("subprocess.STARTUPINFO", mock_startupinfo_class, create=True),
        mock.patch("subprocess.STARTF_USESHOWWINDOW", 1, create=True),
    ):
        startupinfo = get_subprocess_startupinfo()
        assert startupinfo is not None
        assert startupinfo.dwFlags & 1
        assert startupinfo.wShowWindow == 0


def test_get_subprocess_startupinfo_non_windows():
    with mock.patch("sys.platform", "linux"):
        startupinfo = get_subprocess_startupinfo()
        assert startupinfo is None


@mock.patch("subprocess.Popen")
def test_spawn_background_process(mock_popen):
    cmd = ["python", "-c", "print('hello')"]

    # Test without custom env
    spawn_background_process(cmd)

    # Retrieve arguments passed to Popen
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    assert args[0] == cmd
    assert "env" in kwargs
    assert kwargs["env"]["PYTHONPATH"] == str(get_app_dir() / "cache")
    assert "_MEIPASS" not in kwargs["env"]


@mock.patch("subprocess.run")
def test_run_background_process(mock_run):
    cmd = ["python", "-c", "print('hello')"]

    # Test without custom env
    run_background_process(cmd)

    # Retrieve arguments passed to run
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == cmd
    assert "env" in kwargs
    assert kwargs["env"]["PYTHONPATH"] == str(get_app_dir() / "cache")
    assert "_MEIPASS" not in kwargs["env"]


@mock.patch("subprocess.run")
def test_run_background_process_win32(mock_run):
    cmd = ["powershell", "-Command", "Get-Process"]
    mock_startupinfo_class = mock.MagicMock()
    mock_startupinfo_instance = mock.MagicMock()
    mock_startupinfo_class.return_value = mock_startupinfo_instance

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("subprocess.STARTUPINFO", mock_startupinfo_class, create=True),
        mock.patch("subprocess.STARTF_USESHOWWINDOW", 1, create=True),
    ):
        run_background_process(cmd)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "startupinfo" in kwargs
        assert kwargs["startupinfo"] is not None
