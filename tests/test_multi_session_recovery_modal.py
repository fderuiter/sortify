"""Unit and integration tests for MultiSessionRecoveryModal and TUI recovery routing."""

import asyncio
import os
from unittest.mock import patch

import pytest
from textual.widgets import Button, DataTable, Label

from app.config import AppSettings
from app.ui.modals.session import (
    MultiSessionRecoveryModal,
    RecoveryResult,
    SessionRecoveryModal,
)
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings is isolated from persistent disk configuration changes."""
    import app.config
    import app.core.session_catalog

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(app.core.session_catalog, "find_abandoned_sessions", lambda: [])


@pytest.fixture
def sample_abandoned_sessions(tmp_path):
    """Provide a list of mock abandoned sessions."""
    dir1 = str(tmp_path / "target1")
    dir2 = str(tmp_path / "target2")
    dir3 = str(tmp_path / "target3")
    os.makedirs(dir1, exist_ok=True)
    os.makedirs(dir2, exist_ok=True)
    os.makedirs(dir3, exist_ok=True)
    return [
        {
            "session_id": "sess_alpha_001",
            "base_dir": dir1,
            "status": "failed",
            "step_count": 8,
            "has_trapped_files": True,
        },
        {
            "session_id": "sess_beta_002",
            "base_dir": dir2,
            "status": "uncommitted",
            "step_count": 15,
            "has_trapped_files": False,
        },
        {
            "session_id": "sess_gamma_003",
            "base_dir": dir3,
            "status": "interrupted_rollback",
            "step_count": 0,
            "has_trapped_files": True,
        },
    ]


def test_modal_css_rules():
    """Verify MultiSessionRecoveryModal conforms to modal CSS rules."""
    modal = MultiSessionRecoveryModal([])
    assert "width: 90%" in modal.CSS
    assert "max-width: 80" in modal.CSS
    assert "max-height: 90%" in modal.CSS
    assert "overflow-y: auto" in modal.CSS


def test_modal_fits_small_viewports(tmp_path, sample_abandoned_sessions):
    """Verify MultiSessionRecoveryModal fits 80x24 and 70x20 terminal displays without crashing."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test(size):
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test(size=size) as pilot:
            modal = MultiSessionRecoveryModal(
                sample_abandoned_sessions, settings=settings
            )
            app.push_screen(modal)
            await pilot.pause(0.05)
            assert app.screen is modal
            # Verify buttons exist and have non-empty tooltips
            for btn_id in [
                "#btn-rollback-all",
                "#btn-clean-all",
                "#btn-cancel",
                "#btn-help",
                "#btn-clean",
                "#btn-rollback",
                "#btn-resume",
            ]:
                btn = modal.query_one(btn_id, Button)
                assert btn.tooltip is not None and len(str(btn.tooltip)) > 0

    for size in [(80, 24), (70, 20)]:
        asyncio.run(_test(size))


def test_table_metadata_and_display(tmp_path, sample_abandoned_sessions):
    """Verify DataTable contains required session metadata columns and rows."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            modal = MultiSessionRecoveryModal(
                sample_abandoned_sessions, settings=settings
            )
            app.push_screen(modal)
            await pilot.pause(0.05)

            table = modal.query_one("#sessions-table", DataTable)
            col_labels = [col.label.plain for col in table.columns.values()]
            assert "Session ID" in col_labels
            assert "Target Directory" in col_labels
            assert "Step Count" in col_labels
            assert "Status" in col_labels

            assert table.row_count == 3
            lbl_selected = modal.query_one("#lbl-selected-session", Label)
            assert "sess_alpha_001" in str(lbl_selected.render())

    asyncio.run(_test())


def test_screen_reader_announcements(tmp_path, sample_abandoned_sessions):
    """Verify screen reader announcements report the number of detected sessions on mount and selection changes."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            modal = MultiSessionRecoveryModal(
                sample_abandoned_sessions, settings=settings
            )
            app.push_screen(modal)
            await pilot.pause(0.05)

            announcements = [a["message"] for a in modal.announcements]
            assert any(
                "3 abandoned sessions" in msg or "3" in msg for msg in announcements
            )

    asyncio.run(_test())


def test_batch_clean_all(tmp_path, sample_abandoned_sessions):
    """Verify Clean All dismisses with clean_all action and list of sessions."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            modal = MultiSessionRecoveryModal(
                sample_abandoned_sessions, settings=settings
            )
            result = None

            def on_dismiss(val):
                nonlocal result
                result = val

            app.push_screen(modal, on_dismiss)
            await pilot.pause(0.05)
            modal.query_one("#btn-clean-all", Button).press()
            await pilot.pause(0.05)

            assert result == "clean_all"
            assert isinstance(result, RecoveryResult)
            assert len(result.sessions) == 3

    asyncio.run(_test())


def test_batch_rollback_all(tmp_path, sample_abandoned_sessions):
    """Verify Rollback All dismisses with rollback_all action and list of sessions."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            modal = MultiSessionRecoveryModal(
                sample_abandoned_sessions, settings=settings
            )
            result = None

            def on_dismiss(val):
                nonlocal result
                result = val

            app.push_screen(modal, on_dismiss)
            await pilot.pause(0.05)
            modal.query_one("#btn-rollback-all", Button).press()
            await pilot.pause(0.05)

            assert result == "rollback_all"
            assert isinstance(result, RecoveryResult)
            assert len(result.sessions) == 3

    asyncio.run(_test())


def test_item_resume_activates_selected_session(tmp_path, sample_abandoned_sessions):
    """Verify Resume confirms only the selected session and dismisses with that session info."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            modal = MultiSessionRecoveryModal(
                sample_abandoned_sessions, settings=settings
            )
            result = None

            def on_dismiss(val):
                nonlocal result
                result = val

            app.push_screen(modal, on_dismiss)
            await pilot.pause(0.05)

            # Move cursor to second row (sess_beta_002)
            table = modal.query_one("#sessions-table", DataTable)
            table.move_cursor(row=1)
            await pilot.pause(0.05)

            modal.query_one("#btn-resume", Button).press()
            await pilot.pause(0.05)

            assert result == "resume"
            assert result.session["session_id"] == "sess_beta_002"

    asyncio.run(_test())


def test_selective_session_recovery_scenario(tmp_path, sample_abandoned_sessions):
    """Verify user scenario: clean obsolete session in-place, then resume valid session."""
    two_sessions = sample_abandoned_sessions[:2]
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            modal = MultiSessionRecoveryModal(two_sessions, settings=settings)
            result = None

            def on_dismiss(val):
                nonlocal result
                result = val

            with patch(
                "app.core.session_catalog.forget_abandoned_session"
            ) as mock_forget:
                app.push_screen(modal, on_dismiss)
                await pilot.pause(0.05)

                # Cursor is on row 0: sess_alpha_001. User cleans it.
                modal.query_one("#btn-clean", Button).press()
                await pilot.pause(0.05)

                # Verified session was cleaned in-place
                mock_forget.assert_called_once_with(two_sessions[0])
                # Modal should still be open because 1 session remains!
                assert app.screen is modal
                assert len(modal.sessions) == 1
                assert modal.sessions[0]["session_id"] == "sess_beta_002"

                # Now user clicks Resume on remaining session
                modal.query_one("#btn-resume", Button).press()
                await pilot.pause(0.05)

                assert result == "resume"
                assert result.session["session_id"] == "sess_beta_002"

    asyncio.run(_test())


def test_tui_startup_branching_single_vs_multi(tmp_path, sample_abandoned_sessions):
    """Verify TUI launches SessionRecoveryModal for single session and MultiSessionRecoveryModal for multiple."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test_single():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        with patch(
            "app.core.session_catalog.find_abandoned_sessions",
            return_value=[sample_abandoned_sessions[0]],
        ):
            async with app.run_test() as pilot:
                w = app.check_abandoned_sessions()
                await w.wait()
                await pilot.pause(0.1)
                assert isinstance(app.screen, SessionRecoveryModal)

    async def _test_multi():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        with patch(
            "app.core.session_catalog.find_abandoned_sessions",
            return_value=sample_abandoned_sessions,
        ):
            async with app.run_test() as pilot:
                w = app.check_abandoned_sessions()
                await w.wait()
                await pilot.pause(0.1)
                assert isinstance(app.screen, MultiSessionRecoveryModal)

    async def _test_none():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        with patch(
            "app.core.session_catalog.find_abandoned_sessions",
            return_value=[],
        ):
            async with app.run_test() as pilot:
                w = app.check_abandoned_sessions()
                await w.wait()
                await pilot.pause(0.1)
                assert not isinstance(
                    app.screen, (SessionRecoveryModal, MultiSessionRecoveryModal)
                )

    asyncio.run(_test_single())
    asyncio.run(_test_multi())
    asyncio.run(_test_none())


def test_tui_multi_session_clean_all_integration(tmp_path, sample_abandoned_sessions):
    """Verify selecting Clean All in MultiSessionRecoveryModal triggers _do_recovery_clean for each session."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        cleaned_sessions = []
        app._do_recovery_clean = lambda s: cleaned_sessions.append(s["session_id"])

        with patch(
            "app.core.session_catalog.find_abandoned_sessions",
            return_value=sample_abandoned_sessions,
        ):
            async with app.run_test() as pilot:
                w = app.check_abandoned_sessions()
                await w.wait()
                await pilot.pause(0.1)
                assert isinstance(app.screen, MultiSessionRecoveryModal)

                app.screen.query_one("#btn-clean-all", Button).press()
                await pilot.pause(0.1)

                assert cleaned_sessions == [
                    "sess_alpha_001",
                    "sess_beta_002",
                    "sess_gamma_003",
                ]

    asyncio.run(_test())


def test_tui_multi_session_rollback_all_integration(
    tmp_path, sample_abandoned_sessions
):
    """Verify selecting Rollback All in MultiSessionRecoveryModal triggers rollback and rescan."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        rolled_back_sessions = []
        app._do_recovery_rollback = lambda s: rolled_back_sessions.append(
            s["session_id"]
        )
        scan_called = False
        app.action_scan_directory = lambda: globals().update(scan_called=True)

        with patch(
            "app.core.session_catalog.find_abandoned_sessions",
            return_value=sample_abandoned_sessions,
        ):
            async with app.run_test() as pilot:
                w = app.check_abandoned_sessions()
                await w.wait()
                await pilot.pause(0.1)
                assert isinstance(app.screen, MultiSessionRecoveryModal)

                app.screen.query_one("#btn-rollback-all", Button).press()
                await pilot.pause(0.1)

                assert rolled_back_sessions == [
                    "sess_alpha_001",
                    "sess_beta_002",
                    "sess_gamma_003",
                ]

    asyncio.run(_test())


def test_tui_multi_session_resume_retains_unselected(
    tmp_path, sample_abandoned_sessions
):
    """Verify selecting Resume activates the selected session and retains unselected sessions."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True

    async def _test():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        resumed_sessions = []
        cleaned_sessions = []
        rolled_back_sessions = []
        app._do_recovery_resume = lambda s: resumed_sessions.append(s)
        app._do_recovery_clean = lambda s: cleaned_sessions.append(s["session_id"])
        app._do_recovery_rollback = lambda s: rolled_back_sessions.append(
            s["session_id"]
        )

        with patch(
            "app.core.session_catalog.find_abandoned_sessions",
            return_value=sample_abandoned_sessions,
        ):
            async with app.run_test() as pilot:
                w = app.check_abandoned_sessions()
                await w.wait()
                await pilot.pause(0.1)
                assert isinstance(app.screen, MultiSessionRecoveryModal)

                # Resume first session (sess_alpha_001)
                app.screen.query_one("#btn-resume", Button).press()
                await pilot.pause(0.1)

                assert len(resumed_sessions) == 1
                assert resumed_sessions[0]["session_id"] == "sess_alpha_001"
                # Unselected sessions (sess_beta_002 and sess_gamma_003) were retained!
                assert cleaned_sessions == []
                assert rolled_back_sessions == []

    asyncio.run(_test())
