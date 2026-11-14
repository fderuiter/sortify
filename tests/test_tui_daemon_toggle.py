"""Tests for watchdog daemon status badge, async polling, and toggle hotkey in AutoSorterTUI."""

import asyncio
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Static

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for TUI testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_daemon_idle_badge_by_default(temp_workspace):
    """Criterion 1: Status bar displays an idle badge when no daemon runs in the workspace folder."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            sb = app.query_one("#status-bar", Static)
            rendered = str(sb.render())
            assert "[Daemon: Idle]" in rendered
            assert app._get_daemon_status_badge() == "[Daemon: Idle]"

    asyncio.run(_test())


def test_daemon_active_badge_when_daemon_running(temp_workspace):
    """Criterion 2: Status bar displays an active badge when a background watchdog daemon runs."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        fake_pid_info = {"pid": 99999, "socket_path": "/fake/socket"}
        telemetry = {
            "status": "running",
            "is_running": True,
            "is_paused": False,
            "is_moving": False,
        }

        with (
            patch("app.core.ipc.read_pid_file", return_value=fake_pid_info),
            patch("app.core.ipc.is_process_alive", return_value=True),
            patch(
                "app.core.ipc.DaemonIPCClient.send_command",
                new_callable=AsyncMock,
                return_value=telemetry,
            ),
        ):
            async with app.run_test() as pilot:
                await app._poll_daemon_status()
                await pilot.pause()
                sb = app.query_one("#status-bar", Static)
                rendered = str(sb.render())
                assert "[Daemon: Active]" in rendered
                assert app._get_daemon_status_badge() == "[Daemon: Active]"

    asyncio.run(_test())


def test_daemon_toggle_hotkey_sends_pause_request(temp_workspace):
    """Criterion 3: Pressing the assigned toggle shortcut sends a pause request when the daemon runs."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        fake_pid_info = {"pid": 99999, "socket_path": "/fake/socket"}
        mock_send_command = AsyncMock(
            return_value={
                "status": "paused",
                "message": "Daemon triage pipeline paused",
            }
        )

        with (
            patch("app.core.ipc.read_pid_file", return_value=fake_pid_info),
            patch("app.core.ipc.is_process_alive", return_value=True),
            patch("app.core.ipc.DaemonIPCClient.send_command", mock_send_command),
        ):
            async with app.run_test() as pilot:
                app._daemon_state = "active"
                app.update_status()
                await pilot.pause()

                # Press Ctrl+T shortcut
                await pilot.press("ctrl+t")
                await pilot.pause()

                mock_send_command.assert_awaited_with("pause", timeout=2.0)
                assert app._daemon_state == "paused"
                sb = app.query_one("#status-bar", Static)
                assert "[Daemon: Paused]" in str(sb.render())
                assert any(
                    "Watchdog daemon paused." in a["message"]
                    and a["priority"] == "assertive"
                    for a in app.announcements
                )

    asyncio.run(_test())


def test_daemon_toggle_hotkey_sends_resume_request(temp_workspace):
    """Criterion 4: Pressing the assigned toggle shortcut sends a resume request when the daemon is paused."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        fake_pid_info = {"pid": 99999, "socket_path": "/fake/socket"}
        mock_send_command = AsyncMock(
            return_value={
                "status": "resumed",
                "message": "Daemon triage pipeline resumed",
            }
        )

        with (
            patch("app.core.ipc.read_pid_file", return_value=fake_pid_info),
            patch("app.core.ipc.is_process_alive", return_value=True),
            patch("app.core.ipc.DaemonIPCClient.send_command", mock_send_command),
        ):
            async with app.run_test() as pilot:
                app._daemon_state = "paused"
                app.update_status()
                await pilot.pause()

                # Press 'p' shortcut (alternative toggle shortcut)
                await pilot.press("p")
                await pilot.pause()

                mock_send_command.assert_awaited_with("resume", timeout=2.0)
                assert app._daemon_state == "active"
                sb = app.query_one("#status-bar", Static)
                assert "[Daemon: Active]" in str(sb.render())
                assert any(
                    "Watchdog daemon resumed." in a["message"]
                    and a["priority"] == "assertive"
                    for a in app.announcements
                )

    asyncio.run(_test())


def test_screen_reader_assertive_announcements_on_transitions(temp_workspace):
    """Criterion 5: Screen readers receive assertive announcements when the daemon transitions between states."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        fake_pid_info = {"pid": 99999, "socket_path": "/fake/socket"}
        telemetry_paused = {"status": "paused", "is_paused": True, "is_running": True}
        telemetry_active = {"status": "running", "is_paused": False, "is_running": True}

        with (
            patch("app.core.ipc.read_pid_file", return_value=fake_pid_info),
            patch("app.core.ipc.is_process_alive", return_value=True),
            patch(
                "app.core.ipc.DaemonIPCClient.send_command",
                new_callable=AsyncMock,
                side_effect=[telemetry_active, telemetry_paused],
            ),
        ):
            async with app.run_test() as pilot:
                app._daemon_initialized = True
                app._daemon_state = "idle"

                # Transition idle -> active
                await app._poll_daemon_status()
                await pilot.pause()

                active_announcements = [
                    a
                    for a in app.announcements
                    if "Watchdog daemon resumed." in a["message"]
                    or "Watchdog daemon active." in a["message"]
                ]
                assert len(active_announcements) > 0
                assert active_announcements[-1]["priority"] == "assertive"

                # Transition active -> paused
                await app._poll_daemon_status()
                await pilot.pause()

                paused_announcements = [
                    a
                    for a in app.announcements
                    if "Watchdog daemon paused." in a["message"]
                ]
                assert len(paused_announcements) > 0
                assert paused_announcements[-1]["priority"] == "assertive"

    asyncio.run(_test())


def test_pipeline_busy_guard_prevents_moves_when_daemon_is_moving(temp_workspace):
    """Requirement 5: Interactive scan and execute pipelines warn users when conflicts arise."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=temp_workspace, skip_wizard=True)

    app._daemon_is_moving = True
    is_busy = app._announce_if_busy("execute the plan")
    assert is_busy is True

    conflict_announcements = [
        a
        for a in app.announcements
        if "Watchdog daemon is currently moving files" in a["message"]
    ]
    assert len(conflict_announcements) == 1
    assert conflict_announcements[0]["priority"] == "assertive"


def test_daemon_toggle_hotkey_idle_announces_no_daemon(temp_workspace):
    """Test pressing toggle hotkey when no daemon runs emits assertive announcement."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t")
            await pilot.pause()

            idle_announcements = [
                a
                for a in app.announcements
                if "No background watchdog daemon is running for this workspace."
                in a["message"]
            ]
            assert len(idle_announcements) >= 1
            assert idle_announcements[-1]["priority"] == "assertive"

    asyncio.run(_test())


def test_scan_pipeline_aborts_when_daemon_is_moving(temp_workspace):
    """Requirement 5: Interactive scan pipeline refuses to start while daemon moves files."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(
            settings=settings, base_dir=temp_workspace, skip_wizard=True
        )

        async with app.run_test() as pilot:
            await pilot.pause()
            app._daemon_is_moving = True
            app.action_scan_directory()
            await pilot.pause()

            conflict_announcements = [
                a
                for a in app.announcements
                if "Watchdog daemon is currently moving files" in a["message"]
            ]
            assert len(conflict_announcements) >= 1
            assert conflict_announcements[-1]["priority"] == "assertive"

    asyncio.run(_test())
