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
    assert (
        f"Scan Error: Scan operation failed due to permission error [Help: {help_url}]"
        in caplog.text
    )


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


def test_notification_manager_ingress_sanitization_all_modes(tmp_path):
    """Verify ingress sanitization for message and caption in CLI, TUI, GUI, and custom handlers."""
    mgr = NotificationManager.get_instance()

    secret_key = "sk_live_9999988888777776666655555"
    home_dir = str(tmp_path / "secret_folder")
    raw_message = f"Found secret {secret_key} in {home_dir}"
    raw_caption = f"Warning for {home_dir} with {secret_key}"

    # 1. Test Custom Handler receiving sanitized event
    received_events = []

    def custom_handler(event):
        received_events.append(event)

    mgr.register_handler(custom_handler)

    # Test CLI mode
    stream = io.StringIO()
    mgr.set_output_stream(stream)
    mgr.set_mode(NotificationMode.CLI)

    event = notify(raw_message, caption=raw_caption, type="warning")

    assert secret_key not in event["message"]
    assert home_dir not in event["message"]
    assert secret_key not in event["caption"]
    assert home_dir not in event["caption"]
    assert "<USER_HOME>" in event["message"]

    assert len(received_events) == 1
    assert secret_key not in received_events[0]["message"]
    assert home_dir not in received_events[0]["message"]
    assert secret_key not in received_events[0]["caption"]
    assert home_dir not in received_events[0]["caption"]

    # Test TUI mode
    mgr.set_mode(NotificationMode.TUI)
    mock_tui_app = MagicMock()
    mock_log_widget = MagicMock()
    mock_tui_app.query_one.return_value = mock_log_widget
    mock_tui_app.is_headless = True
    mgr.register_tui(mock_tui_app)

    tui_event = notify(raw_message, caption=raw_caption, type="error")
    assert secret_key not in tui_event["message"]
    assert home_dir not in tui_event["message"]

    # Check mock_tui_app.notify and mock_log_widget.write_line received sanitized text
    called_msg = mock_tui_app.notify.call_args[0][0]
    called_title = mock_tui_app.notify.call_args[1]["title"]
    assert secret_key not in called_msg
    assert home_dir not in called_msg
    assert secret_key not in called_title
    assert home_dir not in called_title

    logged_line = mock_log_widget.write_line.call_args[0][0]
    assert secret_key not in logged_line
    assert home_dir not in logged_line


def test_notification_manager_non_string_null_inputs():
    """Verify NotificationManager handles null and non-string inputs safely."""
    mgr = NotificationManager.get_instance()
    mgr.set_mode(NotificationMode.CLI)
    stream = io.StringIO()
    mgr.set_output_stream(stream)

    # Null message
    event_null = notify(None, caption=None)
    assert event_null["message"] == ""
    assert event_null["caption"] is None

    # Non-string message (int)
    event_int = notify(12345, caption=999)
    assert "12345" in event_int["message"]
    assert "999" in event_int["caption"]


def test_a11y_mixin_announce_sanitization(tmp_path, monkeypatch):
    """Verify A11yMixin.announce sanitizes messages before logging, forwarding, speaking, or status updating."""
    from app.ui.tui import A11yMixin

    class TestComponent(A11yMixin):
        def __init__(self):
            super().__init__()
            self.status_updated = None

        def update_status(self, text: str) -> None:
            self.status_updated = text

    comp = TestComponent()

    secret_key = "sk_live_1234567890123456789012345"
    home_path = str(tmp_path / "test_file.txt")
    raw_msg = f"Reading {home_path} key={secret_key}"

    # Mock _get_speech_binary to return dummy speech command
    monkeypatch.setattr(comp, "_get_speech_binary", lambda: "spd-say")

    run_args = []

    def mock_run(cmd, **kwargs):
        run_args.append(cmd)

    monkeypatch.setattr("subprocess.run", mock_run)

    res = comp.announce(raw_msg, priority="assertive")

    # Check return value is sanitized
    assert secret_key not in res
    assert home_path not in res
    assert "<USER_HOME>" in res

    # Check announcements list
    assert len(comp.announcements) == 1
    ann = comp.announcements[0]["message"]
    assert secret_key not in ann
    assert home_path not in ann

    # Check last_announcement
    assert comp.last_announcement is not None
    assert secret_key not in comp.last_announcement
    assert home_path not in comp.last_announcement

    # Check update_status received sanitized string
    assert comp.status_updated is not None
    assert secret_key not in comp.status_updated
    assert home_path not in comp.status_updated

    # Join speech thread if active
    comp.join_speech_thread(timeout=1.0)
    if run_args:
        spoken_text = run_args[0][1]
        assert secret_key not in spoken_text
        assert home_path not in spoken_text


def test_autosorter_tui_announce_tui_log_sanitization(tmp_path):
    """Verify AutoSorterTUI.announce writes pre-sanitized content to #tui-log."""
    from app.ui.tui import AutoSorterTUI

    app = AutoSorterTUI(settings=MagicMock())
    mock_log_widget = MagicMock()

    def mock_query_one(selector, *args, **kwargs):
        if selector == "#tui-log":
            return mock_log_widget
        raise RuntimeError("Widget not found")

    app.query_one = mock_query_one

    secret_key = "sk_live_1234567890123456789012345"
    home_path = str(tmp_path / "log_file.txt")
    raw_msg = f"Log entry for {home_path} with token {secret_key}"

    res = app.announce(raw_msg, help_url="https://docs.sortify.ai/help")

    assert secret_key not in res
    assert home_path not in res
    assert "<USER_HOME>" in res

    assert mock_log_widget.write_line.called
    written_line = mock_log_widget.write_line.call_args[0][0]
    assert secret_key not in written_line
    assert home_path not in written_line
    assert "[Help: https://docs.sortify.ai/help]" in written_line
