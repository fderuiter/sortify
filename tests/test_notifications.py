"""Unit tests for the unified context-aware notification bus."""

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


def test_cli_notification_dispatch(caplog):
    """Test that CLI notification writes formatted messages to standard stream and logger."""
    mgr = NotificationManager.get_instance()
    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.set_mode(NotificationMode.CLI)

    with caplog.at_level(logging.INFO):
        res = notify("Batch move completed", type="positive", caption="Status Alert")

    assert res["message"] == "Batch move completed"
    assert res["type"] == "positive"
    assert res["mode"] == "cli"

    stream_content = stream.getvalue()
    assert "[NOTIFICATION] [POSITIVE] Status Alert: Batch move completed" in stream_content
    assert "Status Alert: Batch move completed" in caplog.text


def test_cli_notification_sensitive_data_scrubbing():
    """Test that CLI notifications scrub user home paths and high entropy secrets."""
    mgr = NotificationManager.get_instance()
    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.set_mode(NotificationMode.CLI)

    secret_key = "sk_live_1234567890123456789012345"
    home_path = f"/home/jules/documents/{secret_key}"

    with mock.patch("app.ui.notifications.scrub_user_home_paths", return_value=f"<USER_HOME>/documents/{secret_key}"):
        notify(f"Processing path: {home_path}", type="info")

    output = stream.getvalue()
    assert secret_key not in output or "<USER_HOME>" in output


def test_tui_notification_dispatch():
    """Test routing notifications to Textual TUI toast popups and log widget."""
    mgr = NotificationManager.get_instance()
    mgr.set_mode(NotificationMode.TUI)

    mock_tui_app = MagicMock()
    mock_log_widget = MagicMock()
    mock_tui_app.query_one.return_value = mock_log_widget
    mock_tui_app.is_headless = True

    mgr.register_tui(mock_tui_app)

    res = notify("Automatic recovery in progress", type="warning", caption="Recovery")

    assert res["mode"] == "tui"
    mock_tui_app.notify.assert_called_once_with(
        "Automatic recovery in progress", title="Recovery", severity="warning", timeout=5.0
    )
    mock_log_widget.write_line.assert_called_once_with("[WARNING] Automatic recovery in progress")


def test_custom_notification_handler():
    """Test registering and invoking custom notification callbacks."""
    mgr = NotificationManager.get_instance()
    received_events = []

    def custom_handler(event):
        received_events.append(event)

    mgr.register_handler(custom_handler)
    notify("Custom event payload", type="info")

    assert len(received_events) == 1
    assert received_events[0]["message"] == "Custom event payload"


def test_notification_error_fallback():
    """Test graceful handling when UI handlers raise exceptions."""
    mgr = NotificationManager.get_instance()
    mgr.set_mode(NotificationMode.TUI)

    failing_tui_app = MagicMock()
    failing_tui_app.notify.side_effect = RuntimeError("TUI rendering failure")
    failing_tui_app.is_headless = True

    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.register_tui(failing_tui_app)

    # Should not raise exception
    res = notify("Fallback notification test", type="negative")
    assert res["message"] == "Fallback notification test"
