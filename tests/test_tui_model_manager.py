"""Tests for dedicated interactive ModelManagerModal in Textual TUI."""

import asyncio
import os
import shutil
import tempfile
from unittest.mock import patch

import pytest
from textual.widgets import Input, Label, ProgressBar

from app.config import AppSettings
from app.core.downloader import DownloadManager
from app.ui.tui import (
    AutoSorterTUI,
    ModelManagerModal,
    SettingsModal,
    inspect_tui_component,
)

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def temp_workspace():
    tmp = tempfile.mkdtemp()
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_dm():
    DownloadManager.reset_instance()
    yield
    DownloadManager.reset_instance()


def test_model_manager_modal_accessibility_audit():
    """Verify ModelManagerModal passes WCAG 2.1 accessibility inspection."""
    settings = AppSettings()
    modal = ModelManagerModal(settings=settings)
    violations = inspect_tui_component(modal)
    assert len(violations) == 0, f"Accessibility violations found: {violations}"


def test_model_manager_open_via_hotkey(temp_workspace):
    """Verify Ctrl+M hotkey opens ModelManagerModal."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            # Close via Escape
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ModelManagerModal)

    asyncio.run(_test())


def test_model_manager_open_via_settings_button(temp_workspace):
    """Verify clicking Model Manager button in SettingsModal opens ModelManagerModal."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            settings_modal = app.screen
            assert isinstance(settings_modal, SettingsModal)

            settings_modal.action_open_model_manager()
            await pilot.pause(0.1)

            assert isinstance(app.screen, ModelManagerModal)

    asyncio.run(_test())


def test_model_manager_display_status_missing(temp_workspace):
    """Verify status labels when model file is missing."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            lbl_status = modal.query_one("#lbl-model-status", Label)
            assert "Missing" in str(lbl_status.content) or "Not Downloaded" in str(
                lbl_status.content
            )

    asyncio.run(_test())


def test_model_manager_start_download_and_progress(temp_workspace):
    """Verify starting download updates progress bar, throughput label, and screen reader announcements."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            dm = DownloadManager.get_instance()
            dm.state["is_downloading"] = True
            dm.state["progress"] = 0.45
            dm.state["status_text"] = "Downloaded 15.00MB of 33.33MB (45.0%)"

            modal._poll_download_status()
            await pilot.pause(0.1)

            pb = modal.query_one("#progress-bar", ProgressBar)
            assert pb.progress == pytest.approx(45.0)

            lbl_prog = modal.query_one("#lbl-progress-status", Label)
            assert "Downloaded 15.00MB" in str(lbl_prog.content)

            # Finish download
            dm.state["is_downloading"] = False
            dm.state["progress"] = 1.0
            dm.state["success"] = True
            dm.state["status_text"] = "Download complete!"

            modal._poll_download_status()
            await pilot.pause(0.1)

            assert (
                modal.get_last_announcement()
                == "Model download completed successfully."
            )

    asyncio.run(_test())


def test_model_manager_cancel_download(temp_workspace):
    """Verify clicking Cancel button stops background download."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            dm = DownloadManager.get_instance()
            dm.state["is_downloading"] = True
            modal._refresh_ui_state()

            modal.action_cancel_download()
            await pilot.pause(0.1)

            assert dm.state["is_downloading"] is False
            assert modal.get_last_announcement() == "Cancelled model download."

    asyncio.run(_test())


def test_model_manager_retry_download(temp_workspace):
    """Verify clicking Retry Download restarts model download process."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            with patch.object(DownloadManager, "start_download") as mock_start:
                modal.action_retry_download()
                await pilot.pause(0.1)

                mock_start.assert_called_once()
                assert modal.get_last_announcement() == "Retrying model download."

    asyncio.run(_test())


def test_model_manager_delete_model(temp_workspace):
    """Verify clicking Delete Model invokes delete_model_async thread-safely."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            model_dir = modal._resolve_model_dir()
            os.makedirs(model_dir, exist_ok=True)
            with open(os.path.join(model_dir, "model.onnx"), "wb") as f:
                f.write(b"dummy_weights")
            modal._refresh_ui_state()

            with patch.object(DownloadManager, "delete_model_async") as mock_delete:
                modal.action_delete_model()
                await pilot.pause(0.1)

                mock_delete.assert_called_once()
                assert "Deleting model weights" in modal.get_last_announcement()

    asyncio.run(_test())


def test_model_manager_proxy_setting_sync(temp_workspace):
    """Verify proxy input syncs with AppSettings and DownloadManager dynamically."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            inp_proxy = modal.query_one("#input-proxy", Input)
            inp_proxy.value = "http://127.0.0.1:8080"

            with patch.object(DownloadManager, "start_download"):
                modal.action_start_download()
                await pilot.pause(0.1)

                assert settings.PROXY == "http://127.0.0.1:8080"
                assert DownloadManager.get_instance()._proxy == "http://127.0.0.1:8080"

    asyncio.run(_test())


def test_model_manager_responsive_layout_narrow(temp_workspace):
    """Verify ModelManagerModal applies narrow class on narrow viewports (<80 width)."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test(size=(70, 24)) as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)
            assert "narrow" in modal.classes

    asyncio.run(_test())
