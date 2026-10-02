"""Unit tests for Dual-Backend Hybrid Clipboard Service."""

import base64
import os
from unittest.mock import MagicMock, patch

from app.ui.clipboard import (
    ClipboardService,
    _copy_via_platform_binary,
    _emit_osc52_to_stdout,
    generate_osc52_sequence,
)


def test_generate_osc52_sequence_standard():
    """Verify OSC 52 sequence generation for standard terminal."""
    with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
        text = "sample_file_path.pdf"
        seq = generate_osc52_sequence(text)
        expected_payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        assert f"\x1b]52;c;{expected_payload}\x07" == seq


def test_generate_osc52_sequence_tmux():
    """Verify tmux DCS passthrough wrapping when TMUX environment variable is set."""
    with patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default", "TERM": "tmux-256color"}):
        text = "tmux_test_payload"
        seq = generate_osc52_sequence(text)
        expected_payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        assert seq.startswith("\x1bPtmux;\x1b\x1b]52;c;")
        assert seq.endswith("\x07\x1b\\")
        assert expected_payload in seq


def test_generate_osc52_sequence_screen():
    """Verify screen DCS passthrough wrapping when STY or TERM=screen is set."""
    with patch.dict(os.environ, {"STY": "1234.pts-0.host", "TERM": "screen"}, clear=True):
        text = "screen_test_payload"
        seq = generate_osc52_sequence(text)
        expected_payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
        assert seq.startswith("\x1bP\x1b]52;c;")
        assert seq.endswith("\x07\x1b\\")
        assert expected_payload in seq


def test_emit_osc52_to_stdout():
    """Verify writing OSC 52 escape sequence to standard output."""
    mock_stdout = MagicMock()
    with patch("sys.stdout", mock_stdout), patch("sys.__stdout__", mock_stdout):
        res = _emit_osc52_to_stdout("\x1b]52;c;test\x07")
        assert res is True
        mock_stdout.write.assert_called_once_with("\x1b]52;c;test\x07")
        mock_stdout.flush.assert_called_once()


def test_clipboard_service_copy_empty():
    """Verify copying empty string returns False without errors."""
    service = ClipboardService()
    assert service.copy("") is False


def test_clipboard_service_app_integration():
    """Verify ClipboardService invokes app.copy_to_clipboard if available."""
    mock_app = MagicMock()
    service = ClipboardService(app=mock_app)
    
    with patch("app.ui.clipboard._emit_osc52_to_stdout", return_value=True):
        res = service.copy("/path/to/file.txt")
        assert res is True
        mock_app.copy_to_clipboard.assert_called_once_with("/path/to/file.txt")


def test_clipboard_service_fallback_when_app_fails():
    """Verify ClipboardService continues to OSC 52 and platform binary when app.copy_to_clipboard fails."""
    mock_app = MagicMock()
    mock_app.copy_to_clipboard.side_effect = Exception("Clipboard driver error")
    
    service = ClipboardService(app=mock_app)
    
    with patch("app.ui.clipboard._emit_osc52_to_stdout", return_value=True):
        res = service.copy("test text")
        assert res is True


def test_platform_binary_fallback_darwin():
    """Verify macOS pbcopy binary fallback invocation."""
    with patch("sys.platform", "darwin"), \
         patch("shutil.which", return_value="/usr/bin/pbcopy"), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value.returncode = 0
        res = _copy_via_platform_binary("test_mac_copy")
        assert res is True
        mock_sub.assert_called_once_with(
            ["/usr/bin/pbcopy"],
            input=b"test_mac_copy",
            stdout=-3,  # DEVNULL
            stderr=-3,  # DEVNULL
            timeout=1.0,
            check=False,
        )


def test_platform_binary_fallback_linux():
    """Verify Linux xclip binary fallback invocation."""
    with patch("sys.platform", "linux"), \
         patch("shutil.which", side_effect=lambda x: "/usr/bin/xclip" if x == "xclip" else None), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value.returncode = 0
        res = _copy_via_platform_binary("test_linux_copy")
        assert res is True
        mock_sub.assert_called_once_with(
            ["/usr/bin/xclip", "-selection", "clipboard"],
            input=b"test_linux_copy",
            stdout=-3,  # DEVNULL
            stderr=-3,  # DEVNULL
            timeout=1.0,
            check=False,
        )
