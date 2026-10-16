"""Unit and integration tests for TUI ContinuousWatchdogDaemon status bar indicators and pause/resume controls."""

import asyncio
import tempfile
from unittest.mock import MagicMock

import pytest
from textual.widgets import Static

from app.config import AppSettings
from app.core.daemon import ContinuousWatchdogDaemon
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings and TUI environment are isolated."""
    import app.config
    import app.core.session

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(
        app.core.session,
        "scan_abandoned_sessions_async",
        AsyncMock(return_value=[]),
    )


def AsyncMock(*args, **kwargs):
    from unittest.mock import AsyncMock as AM
    return AM(*args, **kwargs)


@pytest.fixture
def temp_workspace():
    """Create a temporary directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_tui_status_bar_displays_watch_indicator_and_queue_metrics(temp_workspace):
    """Verify AutoSorterTUI status bar renders live watch folder indicator and queue activity metrics."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        daemon = ContinuousWatchdogDaemon(settings, temp_workspace)

        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace, daemon=daemon)
        async with app.run_test() as pilot:
            sb = app.query_one("#status-bar", Static)
            status_text = str(sb.render())

            assert "[AI: Active]" in status_text
            assert "[Watch: Stopped" in status_text
            assert "Queue: 0" in status_text

    asyncio.run(_test())


def test_tui_toggle_watch_shortcut_pauses_and_resumes_daemon(temp_workspace):
    """Verify pressing toggle_watch key action immediately pauses and resumes background folder watching."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        daemon = ContinuousWatchdogDaemon(settings, temp_workspace)
        daemon._is_running = True

        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace, daemon=daemon)
        async with app.run_test() as pilot:
            sb = app.query_one("#status-bar", Static)

            # Initially active and not paused
            assert daemon.is_paused is False
            assert "[Watch: Active" in str(sb.render())

            # Pause via shortcut/action
            app.action_toggle_watch()
            await pilot.pause(0.1)

            assert daemon.is_paused is True
            assert "[Watch: Paused" in str(sb.render())
            assert "Background folder watching paused." in str(sb.render())

            # Resume via shortcut/action
            app.action_toggle_watch()
            await pilot.pause(0.1)

            assert daemon.is_paused is False
            assert "[Watch: Active" in str(sb.render())
            assert "Background folder watching resumed." in str(sb.render())

    asyncio.run(_test())


def test_tui_key_shortcut_p_triggers_toggle_watch(temp_workspace):
    """Verify pressing 'ctrl+p' or 'p' key shortcut triggers action_toggle_watch."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        daemon = ContinuousWatchdogDaemon(settings, temp_workspace)
        daemon._is_running = True

        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, daemon=daemon, skip_wizard=True
        )
        async with app.run_test() as pilot:
            sb = app.query_one("#status-bar", Static)

            # Ensure plan tree is focused
            app.query_one("#plan-tree").focus()
            await pilot.pause(0.05)

            # Press 'p' to pause
            await pilot.press("p")
            await pilot.pause(0.1)

            assert daemon.is_paused is True
            assert "[Watch: Paused" in str(sb.render())

            # Press 'p' again to resume
            await pilot.press("p")
            await pilot.pause(0.1)

            assert daemon.is_paused is False
            assert "[Watch: Active" in str(sb.render())

    asyncio.run(_test())


def test_tui_status_bar_updates_live_queue_metrics(temp_workspace):
    """Verify status bar reflects live queue activity metrics when events are enqueued in daemon."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        daemon = ContinuousWatchdogDaemon(settings, temp_workspace)

        # Mock queue size
        mock_queue = MagicMock()
        mock_queue.qsize.return_value = 7
        daemon._event_queue = mock_queue

        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace, daemon=daemon)
        async with app.run_test() as pilot:
            sb = app.query_one("#status-bar", Static)
            app.update_status()

            assert "Queue: 7" in str(sb.render())

    asyncio.run(_test())


def test_tui_explicit_pause_and_resume_watch_actions(temp_workspace):
    """Verify action_pause_watch and action_resume_watch update state immediately."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        daemon = ContinuousWatchdogDaemon(settings, temp_workspace)
        daemon._is_running = True

        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace, daemon=daemon)
        async with app.run_test() as pilot:
            sb = app.query_one("#status-bar", Static)

            app.action_pause_watch()
            await pilot.pause(0.1)
            assert daemon.is_paused is True
            assert "[Watch: Paused" in str(sb.render())

            app.action_resume_watch()
            await pilot.pause(0.1)
            assert daemon.is_paused is False
            assert "[Watch: Active" in str(sb.render())

    asyncio.run(_test())


def test_tui_toggle_watch_with_no_directory():
    """Verify action_toggle_watch handles unconfigured workspace gracefully."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir="")

        async with app.run_test() as pilot:
            app.action_toggle_watch()
            await pilot.pause(0.1)

            assert app.get_last_announcement() == "No watch folder configured. Select a directory first."

    asyncio.run(_test())
