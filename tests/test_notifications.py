"""Unit tests for the unified context-aware notification bus with help_url support."""

import io
import logging
from unittest import mock
from unittest.mock import MagicMock

import pytest

from app.ui.notifications import (
    NotificationManager,
    NotificationMode,
    notify,
)


@pytest.fixture(autouse=True)
def reset_notification_manager():
    """Reset singleton instance before and after each test."""
    NotificationManager.reset_instance()
    yield
    NotificationManager.reset_instance()


def test_notification_mode_enum():
    """Test NotificationMode enum values and mode getter/setter."""
    mgr = NotificationManager.get_instance()
    assert mgr.get_mode() == NotificationMode.AUTO

    mgr.set_mode(NotificationMode.CLI)
    assert mgr.get_mode() == NotificationMode.CLI
    assert mgr.detect_mode() == NotificationMode.CLI

    mgr.set_mode("tui")
    assert mgr.get_mode() == NotificationMode.TUI
    assert mgr.detect_mode() == NotificationMode.TUI

    mgr.set_mode("gui")
    assert mgr.get_mode() == NotificationMode.GUI
    assert mgr.detect_mode() == NotificationMode.GUI


def test_cli_notification_dispatch_with_help_url(caplog):
    """Test that CLI notification formats messages with help_url in stream and logger."""
    mgr = NotificationManager.get_instance()
    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.set_mode(NotificationMode.CLI)

    help_url = "https://docs.smartautosorter.com/troubleshooting/#scan-errors"

    with caplog.at_level(logging.ERROR):
        res = notify(
            "Scan operation failed due to permission error",
            type="error",
            caption="Scan Error",
            help_url=help_url,
        )

    assert res["message"] == "Scan operation failed due to permission error"
    assert res["type"] == "error"
    assert res["mode"] == "cli"
    assert res["help_url"] == help_url

    stream_content = stream.getvalue()
    expected = f"[NOTIFICATION] [ERROR] Scan Error: Scan operation failed due to permission error [Help: {help_url}]"
    assert expected in stream_content
    assert f"Scan Error: Scan operation failed due to permission error [Help: {help_url}]" in caplog.text


def test_cli_notification_without_help_url():
    """Test CLI notification when help_url is None falls back to standard format."""
    mgr = NotificationManager.get_instance()
    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.set_mode(NotificationMode.CLI)

    res = notify("Batch move completed", type="info")
    assert res["help_url"] is None
    assert "[Help:" not in stream.getvalue()
    assert "[NOTIFICATION] [INFO] Batch move completed" in stream.getvalue()


def test_cli_notification_sensitive_data_scrubbing_with_help_url():
    """Test that CLI notifications scrub user home paths and secrets before appending help_url."""
    mgr = NotificationManager.get_instance()
    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.set_mode(NotificationMode.CLI)

    secret_key = "sk_live_1234567890123456789012345"
    home_path = f"/home/jules/documents/{secret_key}"
    help_url = "https://docs.smartautosorter.com/troubleshooting/#path-errors"

    with mock.patch(
        "app.ui.notifications.scrub_user_home_paths",
        return_value=f"<USER_HOME>/documents/{secret_key}",
    ):
        notify(
            f"Processing path: {home_path}",
            type="error",
            help_url=help_url,
        )

    output = stream.getvalue()
    assert secret_key not in output or "<USER_HOME>" in output
    assert f"[Help: {help_url}]" in output


def test_tui_notification_dispatch_with_help_url():
    """Test routing notifications with help_url to Textual TUI toast popups and log widget."""
    mgr = NotificationManager.get_instance()
    mgr.set_mode(NotificationMode.TUI)

    mock_tui_app = MagicMock()
    mock_log_widget = MagicMock()
    mock_tui_app.query_one.return_value = mock_log_widget
    mock_tui_app.is_headless = True

    mgr.register_tui(mock_tui_app)

    help_url = "https://docs.smartautosorter.com/troubleshooting/#recovery"
    res = notify(
        "Automatic recovery in progress",
        type="warning",
        caption="Recovery",
        help_url=help_url,
    )

    assert res["mode"] == "tui"
    assert res["help_url"] == help_url

    expected_msg = f"Automatic recovery in progress [Help: {help_url}]"
    mock_tui_app.notify.assert_called_once_with(
        expected_msg, title="Recovery", severity="warning", timeout=5.0
    )
    mock_log_widget.write_line.assert_called_once_with(f"[WARNING] {expected_msg}")


def test_custom_notification_handler_with_help_url():
    """Test registering and invoking custom notification callbacks with help_url payload."""
    mgr = NotificationManager.get_instance()
    received_events = []

    def custom_handler(event):
        received_events.append(event)

    mgr.register_handler(custom_handler)
    help_url = "https://docs.smartautosorter.com/troubleshooting/#custom"
    notify("Custom event payload", type="info", help_url=help_url)

    assert len(received_events) == 1
    assert received_events[0]["message"] == "Custom event payload"
    assert received_events[0]["help_url"] == help_url


def test_notification_error_fallback():
    """Test graceful handling when UI handlers raise exceptions."""
    mgr = NotificationManager.get_instance()
    mgr.set_mode(NotificationMode.TUI)

    failing_tui_app = MagicMock()
    failing_tui_app.notify.side_effect = RuntimeError("TUI rendering failure")
    failing_tui_app.query_one.side_effect = RuntimeError("Widget query failure")
    failing_tui_app.is_headless = True

    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.register_tui(failing_tui_app)

    res = notify(
        "Fallback notification test",
        type="error",
        help_url="https://docs.smartautosorter.com/troubleshooting/#fallback",
    )
    assert res["message"] == "Fallback notification test"
    assert "Fallback notification test" in stream.getvalue()
