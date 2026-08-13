import os
import subprocess
from unittest import mock

import pytest

from app.config import get_app_dir
from app.core.env_helper import (
    MACOS_SANDBOX_PROFILE,
    RestrictedPopen,
    check_linux_sandbox_support,
    check_macos_sandbox_support,
    check_windows_sandbox_support,
    get_cleaned_env,
    get_subprocess_startupinfo,
    run_background_process,
    spawn_background_process,
)


def test_get_cleaned_env_defaults():
    # Setup test environment with PyInstaller variables while preserving system PATH
    real_path = os.environ.get("PATH", "/usr/bin")
    test_env = {
        "PATH": real_path,
        "_MEIPASS": "/tmp/_MEI12345",
        "PYTHONPATH": "/tmp/frozen_app",
    }

    with mock.patch.dict(os.environ, test_env, clear=False):
        cleaned = get_cleaned_env()

        # Ensure _MEIPASS is removed
        assert "_MEIPASS" not in cleaned
        # Ensure the path contains other variables
        assert cleaned["PATH"] == real_path
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

    # Test without custom env, passing sandbox=False to preserve original test assertions
    spawn_background_process(cmd, sandbox=False)

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

    # Test without custom env, passing sandbox=False to preserve original test assertions
    run_background_process(cmd, sandbox=False)

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
        run_background_process(cmd, sandbox=False)

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert "startupinfo" in kwargs
        assert kwargs["startupinfo"] is not None


# --- NEW SANDBOXING UNIT TESTS ---


def test_check_linux_sandbox_support_success():
    with mock.patch("sys.platform", "linux"), mock.patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        assert check_linux_sandbox_support() is True
        mock_run.assert_called_once_with(
            ["unshare", "-n", "-r", "true"], capture_output=True, text=True, timeout=2
        )


def test_check_linux_sandbox_support_failure():
    with mock.patch("sys.platform", "linux"), mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = Exception("Not supported")
        assert check_linux_sandbox_support() is False


def test_check_macos_sandbox_support_success():
    with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        assert check_macos_sandbox_support() is True
        mock_run.assert_called_once_with(
            ["sandbox-exec", "-p", MACOS_SANDBOX_PROFILE, "true"],
            capture_output=True,
            text=True,
            timeout=2,
        )


def test_check_macos_sandbox_support_failure():
    with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run") as mock_run:
        mock_run.side_effect = Exception("Not supported")
        assert check_macos_sandbox_support() is False


def test_check_windows_sandbox_support_success():
    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("ctypes.windll", create=True) as mock_windll,
    ):
        # Mocking existence of required Win32 APIs
        mock_windll.advapi32.CreateRestrictedToken = mock.MagicMock()
        mock_windll.advapi32.ConvertStringSidToSidW = mock.MagicMock()
        mock_windll.advapi32.DuplicateTokenEx = mock.MagicMock()
        mock_windll.advapi32.CreateProcessAsUserW = mock.MagicMock()

        assert check_windows_sandbox_support() is True


def test_check_windows_sandbox_support_failure():
    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("ctypes.windll", create=True) as mock_windll,
    ):
        # Cause an exception by making advapi32 lack one of the methods
        mock_windll.advapi32 = mock.MagicMock(spec=[])
        assert check_windows_sandbox_support() is False


def test_spawn_background_process_sandboxed_linux():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", True),
        mock.patch("subprocess.Popen") as mock_popen,
    ):
        spawn_background_process(cmd, sandbox=True)
        mock_popen.assert_called_once()
        called_args, called_kwargs = mock_popen.call_args
        expected_cmd = [
            "unshare",
            "-n",
            "-r",
            "sh",
            "-c",
            'ip link set lo up && exec "$@"',
            "--",
        ] + cmd
        assert called_args[0] == expected_cmd


def test_spawn_background_process_sandboxed_macos():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    with (
        mock.patch("sys.platform", "darwin"),
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", True),
        mock.patch("subprocess.Popen") as mock_popen,
    ):
        spawn_background_process(cmd, sandbox=True)
        mock_popen.assert_called_once()
        called_args, called_kwargs = mock_popen.call_args
        assert called_args[0][0] == "sandbox-exec"
        assert called_args[0][1] == "-p"
        assert "deny network-outbound" in called_args[0][2]
        assert called_args[0][3:] == cmd


def test_spawn_background_process_sandboxed_windows():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    # Mock RestrictedPopen and Windows platform
    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", True),
        mock.patch("app.core.env_helper.RestrictedPopen") as mock_restricted_popen,
    ):
        spawn_background_process(cmd, sandbox=True)
        mock_restricted_popen.assert_called_once_with(
            cmd, env=mock.ANY, startupinfo=mock.ANY
        )


def test_spawn_background_process_fail_closed():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    with (
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", False),
    ):
        with pytest.raises(PermissionError) as exc_info:
            spawn_background_process(cmd, sandbox=True)
        assert "Subprocess sandboxing is enabled but not supported" in str(
            exc_info.value
        )


def test_run_background_process_sandboxed_linux():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    with (
        mock.patch("sys.platform", "linux"),
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", True),
        mock.patch("subprocess.run") as mock_run,
    ):
        run_background_process(cmd, sandbox=True)
        mock_run.assert_called_once()
        called_args, called_kwargs = mock_run.call_args
        expected_cmd = [
            "unshare",
            "-n",
            "-r",
            "sh",
            "-c",
            'ip link set lo up && exec "$@"',
            "--",
        ] + cmd
        assert called_args[0] == expected_cmd


def test_run_background_process_sandboxed_macos():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    with (
        mock.patch("sys.platform", "darwin"),
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", True),
        mock.patch("subprocess.run") as mock_run,
    ):
        run_background_process(cmd, sandbox=True)
        mock_run.assert_called_once()
        called_args, called_kwargs = mock_run.call_args
        assert called_args[0][0] == "sandbox-exec"
        assert called_args[0][1] == "-p"
        assert "deny network-outbound" in called_args[0][2]
        assert called_args[0][3:] == cmd


def test_run_background_process_sandboxed_windows():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    is_restricted_popen = False

    def mock_run_side_effect(*args, **kwargs):
        nonlocal is_restricted_popen
        if subprocess.Popen is RestrictedPopen:
            is_restricted_popen = True
        return mock.MagicMock()

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", True),
        mock.patch("subprocess.run", side_effect=mock_run_side_effect) as mock_run,
    ):
        run_background_process(cmd, sandbox=True)
        mock_run.assert_called_once()
        assert is_restricted_popen is True


def test_run_background_process_fail_closed():
    cmd = ["ffmpeg", "-i", "input.mp3"]
    with (
        mock.patch("app.core.env_helper.SANDBOX_SUPPORTED", False),
    ):
        with pytest.raises(PermissionError) as exc_info:
            run_background_process(cmd, sandbox=True)
        assert "Subprocess sandboxing is enabled but not supported" in str(
            exc_info.value
        )


def test_restricted_popen_execute_child_logic():
    import ctypes
    import importlib

    # We will temporarily mock sys.platform and other Win32-specific components
    mock_ctypes = mock.MagicMock()
    mock_ctypes.windll = mock.MagicMock()
    mock_ctypes.windll.advapi32 = mock.MagicMock()
    mock_ctypes.windll.kernel32 = mock.MagicMock()

    # Configure mock return values to allow token duplication and process creation
    mock_ctypes.windll.advapi32.OpenProcessToken.return_value = True
    mock_ctypes.windll.advapi32.ConvertStringSidToSidW.return_value = True
    mock_ctypes.windll.advapi32.CreateRestrictedToken.return_value = True
    mock_ctypes.windll.advapi32.DuplicateTokenEx.return_value = True
    mock_ctypes.windll.advapi32.CreateProcessAsUserW.return_value = True
    mock_ctypes.windll.kernel32.GetCurrentProcess.return_value = 1234
    mock_ctypes.windll.kernel32.CloseHandle.return_value = True
    mock_ctypes.windll.kernel32.LocalFree.return_value = True

    # Mock wintypes using actual ctypes types so Structure compilation succeeds
    mock_wintypes = mock.MagicMock()
    mock_wintypes.LPVOID = ctypes.c_void_p
    mock_wintypes.DWORD = ctypes.c_ulong
    mock_wintypes.HANDLE = ctypes.c_void_p
    mock_wintypes.WORD = ctypes.c_ushort
    mock_wintypes.LPWSTR = ctypes.c_wchar_p
    mock_wintypes.LPCWSTR = ctypes.c_wchar_p
    mock_wintypes.BOOL = ctypes.c_long

    # Mock _winapi
    mock_winapi = mock.MagicMock()
    mock_msvcrt = mock.MagicMock()

    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("subprocess._mswindows", True),
        mock.patch("ctypes.windll", mock_ctypes.windll, create=True),
        mock.patch("ctypes.WinError", Exception, create=True),
        mock.patch("os.open", return_value=999),
        mock.patch("os.set_inheritable"),
        mock.patch("os.close"),
        mock.patch.dict(
            "sys.modules", {
                "ctypes.wintypes": mock_wintypes,
                "_winapi": mock_winapi,
                "msvcrt": mock_msvcrt
            }
        ),
    ):
        # Reload env_helper to define RestrictedPopen Windows class
        from app.core import env_helper

        importlib.reload(env_helper)

        # Instantiate a mock Popen and call _execute_child
        class MockPopen(env_helper.RestrictedPopen):
            def __init__(self):
                self._child_created = False
                self._handle = None
                self.pid = None
                self.returncode = None
                self._closed_child_pipe_fds = False

            def __del__(self):
                pass

        proc = MockPopen()

        p2cread_mock = mock.MagicMock()
        p2cwrite_mock = mock.MagicMock()
        c2pread_mock = mock.MagicMock()
        c2pwrite_mock = mock.MagicMock()
        errread_mock = mock.MagicMock()
        errwrite_mock = mock.MagicMock()

        # Call _execute_child
        proc._execute_child(
            args=["dummy.exe"],
            executable="dummy.exe",
            preexec_fn=None,
            close_fds=True,
            pass_fds=(),
            cwd=None,
            env={},
            startupinfo=None,
            creationflags=0,
            shell=False,
            p2cread=p2cread_mock,
            p2cwrite=p2cwrite_mock,
            c2pread=c2pread_mock,
            c2pwrite=c2pwrite_mock,
            errread=errread_mock,
            errwrite=errwrite_mock,
        )

        # Verify that only the child pipe ends are closed
        p2cread_mock.Close.assert_called_once()
        c2pwrite_mock.Close.assert_called_once()
        errwrite_mock.Close.assert_called_once()

        # Verify that parent ends are NOT closed
        p2cwrite_mock.Close.assert_not_called()
        c2pread_mock.Close.assert_not_called()
        errread_mock.Close.assert_not_called()

        # Verify that self.pid was set
        assert proc.pid is not None

    # Clean up by reloading env_helper with standard platform
    from app.core import env_helper

    importlib.reload(env_helper)


def test_check_windows_sandbox_support_live_probe_pipe_failure():
    mock_ctypes = mock.MagicMock()
    mock_ctypes.windll = mock.MagicMock()
    mock_ctypes.windll.advapi32 = mock.MagicMock()
    
    class FakeFunction:
        pass
        
    mock_ctypes.windll.advapi32.CreateProcessAsUserW = FakeFunction()
    
    with (
        mock.patch("sys.platform", "win32"),
        mock.patch("ctypes.windll", mock_ctypes.windll, create=True),
        mock.patch("app.core.env_helper.RestrictedPopen", side_effect=Exception("Pipe creation or duplication failure")),
    ):
        from app.core.env_helper import check_windows_sandbox_support
        assert check_windows_sandbox_support() is False

