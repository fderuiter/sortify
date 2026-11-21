"""Unit tests for Base64-encoded communication channel and native directory selection dialog."""

import base64
from unittest.mock import MagicMock, patch

import pytest

from app.core.native_dialog import (
    RULE_IMPORT_EXTENSIONS,
    NativeDialogError,
    decode_base64_to_path,
    encode_path_to_base64,
    select_native_directory,
    select_native_directory_powershell,
    select_native_file,
    select_native_file_powershell,
)


def test_encode_decode_base64_path():
    """Test Base64 encoding and decoding for international non-ASCII folder paths."""
    test_paths = [
        "C:\\Users\\Test\\Données\\Projets",
        "C:\\Workspace\\日本語_Folder\\München",
        "/home/user/documentos_español/café_folder",
        "D:\\Archive\\Résumé_2026_📁",
    ]

    for orig_path in test_paths:
        encoded = encode_path_to_base64(orig_path)
        # Ensure encoded string contains only ASCII characters
        encoded.encode("ascii")

        decoded = decode_base64_to_path(encoded)
        assert decoded == orig_path


def test_select_native_directory_powershell_success():
    """Test PowerShell folder browser execution returning Base64-encoded UTF-8 path."""
    target_path = "C:\\Users\\User\\Données\\日本語_Projects"
    b64_path = base64.b64encode(target_path.encode("utf-8")).decode("ascii")

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = f"{b64_path}\n"
    mock_res.stderr = ""

    with patch(
        "app.core.native_dialog.run_background_process", return_value=mock_res
    ) as mock_run:
        result = select_native_directory_powershell(
            initial_dir="C:\\Users\\User\\Downloads"
        )
        assert result == target_path

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "powershell"
        assert "-ExecutionPolicy" in cmd
        assert "Bypass" in cmd

        # Verify environment variable sanitization
        env = kwargs.get("env", {})
        assert "_MEIPASS" not in env


def test_select_native_directory_powershell_cancelled():
    """Test PowerShell dialog cancellation by user (empty output)."""
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "\n"
    mock_res.stderr = ""

    with patch("app.core.native_dialog.run_background_process", return_value=mock_res):
        result = select_native_directory_powershell()
        assert result is None


def test_select_native_directory_powershell_failure_raises_native_dialog_error():
    """Test PowerShell failure raises NativeDialogError for manual fallback trigger."""
    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stdout = ""
    mock_res.stderr = "PowerShell Execution Policy Restricted"

    with patch("app.core.native_dialog.run_background_process", return_value=mock_res):
        with pytest.raises(NativeDialogError) as exc_info:
            select_native_directory_powershell()
        assert "PowerShell folder dialog failed" in str(exc_info.value)


def test_select_native_directory_platform_dispatch():
    """Test platform dispatch for select_native_directory."""
    with patch("sys.platform", "win32"):
        with patch(
            "app.core.native_dialog.select_native_directory_powershell",
            return_value="C:\\DecodedPath",
        ) as mock_ps:
            res = select_native_directory()
            assert res == "C:\\DecodedPath"
            mock_ps.assert_called_once()


def test_downstream_path_validation_with_decoded_utf8_path(tmp_path):
    """Test that decoded UTF-8 path operates safely in file system operations."""
    test_dir_name = "Données_日本語_Tests"
    test_dir = tmp_path / test_dir_name
    test_dir.mkdir()

    orig_path = str(test_dir)
    encoded = encode_path_to_base64(orig_path)
    decoded = decode_base64_to_path(encoded)

    assert decoded == orig_path
    assert test_dir.exists()
    assert (test_dir / "test.txt").write_text("ok", encoding="utf-8") == 2


def test_select_native_file_powershell_success_cancel_and_error():
    """Verify PowerShell OpenFileDialog returns UTF-8 paths, handles cancel, and raises on error."""
    target_file = "C:\\Users\\Alice\\Données\\DropIt_Rules.ini"
    b64_file = base64.b64encode(target_file.encode("utf-8")).decode("ascii")

    mock_ok = MagicMock(returncode=0, stdout=f"{b64_file}\n", stderr="")
    with patch(
        "app.core.native_dialog.run_background_process", return_value=mock_ok
    ) as mock_run:
        res = select_native_file_powershell(
            initial_dir="C:\\Users\\Alice\\Downloads",
            title="Select Rule File",
            extensions=RULE_IMPORT_EXTENSIONS,
        )
        assert res == target_file
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "powershell"
        assert "OpenFileDialog" in cmd[-1]
        import re

        m_filter = re.search(
            r"\$filterBytes = \[System\.Convert\]::FromBase64String\('([^']+)'\)",
            cmd[-1],
        )
        assert m_filter is not None
        decoded_filter = decode_base64_to_path(m_filter.group(1))
        assert "*.ini" in decoded_filter
        assert "*.hazelrules" in decoded_filter

    mock_cancel = MagicMock(returncode=0, stdout="\n", stderr="")
    with patch(
        "app.core.native_dialog.run_background_process", return_value=mock_cancel
    ):
        assert select_native_file_powershell() is None

    mock_fail = MagicMock(returncode=1, stdout="", stderr="STA thread unavailable")
    with patch("app.core.native_dialog.run_background_process", return_value=mock_fail):
        with pytest.raises(NativeDialogError, match="PowerShell file dialog failed"):
            select_native_file_powershell()


def test_select_native_file_platform_dispatch():
    """Verify cross-platform native file picker dispatch across Windows, macOS, Linux, and unsupported OS."""
    with patch("sys.platform", "win32"):
        with patch(
            "app.core.native_dialog.select_native_file_powershell",
            return_value="C:\\Rules\\profile.ini",
        ) as mock_ps:
            assert select_native_file() == "C:\\Rules\\profile.ini"
            mock_ps.assert_called_once()

    with patch("sys.platform", "darwin"):
        mock_mac_ok = MagicMock(
            returncode=0, stdout="/Users/alice/Exported.hazelrules\n", stderr=""
        )
        with patch(
            "app.core.native_dialog.run_background_process", return_value=mock_mac_ok
        ):
            assert select_native_file() == "/Users/alice/Exported.hazelrules"

        mock_mac_cancel = MagicMock(
            returncode=1, stdout="", stderr="User canceled (-128)"
        )
        with patch(
            "app.core.native_dialog.run_background_process",
            return_value=mock_mac_cancel,
        ):
            assert select_native_file() is None

        mock_mac_err = MagicMock(returncode=1, stdout="", stderr="WindowServer error")
        with patch(
            "app.core.native_dialog.run_background_process", return_value=mock_mac_err
        ):
            with pytest.raises(
                NativeDialogError, match="macOS native file dialog failed"
            ):
                select_native_file()

    with patch("sys.platform", "linux"):
        mock_lin_ok = MagicMock(
            returncode=0, stdout="/home/alice/profile.ini\n", stderr=""
        )
        with patch(
            "app.core.native_dialog.run_background_process", return_value=mock_lin_ok
        ):
            assert select_native_file() == "/home/alice/profile.ini"

        mock_lin_cancel = MagicMock(returncode=1, stdout="", stderr="")
        with patch(
            "app.core.native_dialog.run_background_process",
            return_value=mock_lin_cancel,
        ):
            assert select_native_file() is None

        with patch(
            "app.core.native_dialog.run_background_process",
            side_effect=FileNotFoundError("zenity not installed"),
        ):
            with pytest.raises(
                NativeDialogError, match="Linux native file dialog failed"
            ):
                select_native_file()

    with patch("sys.platform", "freebsd14"):
        with pytest.raises(NativeDialogError, match="not supported on platform"):
            select_native_file()
