from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.ui.app import AutoSorterApp, PlanRebuilderThread


class DummyOverlay:
    def __init__(self):
        self.visible = False

    def set_visibility(self, visible):
        self.visible = visible


class DummyButton:
    def __init__(self):
        self.enabled = True

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False


class DummyLabel:
    def __init__(self):
        self.text = ""

    def set_text(self, text):
        self.text = text


@pytest.fixture
def mock_app():
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    app = AutoSorterApp(settings)
    app.base_dir = "/dummy/base"
    app.app_session = MagicMock()
    app.app_session.analyzer = MagicMock()

    # Setup dummy UI elements for rebuilder
    app.tree_overlay = DummyOverlay()
    app.execute_btn = DummyButton()
    app.status_label = DummyLabel()

    return app


def test_rebuilder_success(mock_app):
    mock_plan = {"FolderA": {"file1.txt": {"__type__": "file"}}}
    mock_app.app_session.analyzer.generate_sorting_plan.return_value = mock_plan
    mock_app.app_session.analyzer.strategy_name = "default"

    # Mock asyncio loop
    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        thread = PlanRebuilderThread(
            mock_app,
            mock_app.base_dir,
            mock_app.settings,
            mock_app.locked_files,
            timeout=5.0,
        )

        # Run thread logic directly
        thread.run()

    # Verify loop callback was scheduled
    mock_loop.call_soon_threadsafe.assert_called_once()

    # Call complete handler directly to simulate main loop execution
    mock_app._on_rebuild_complete(thread)

    assert mock_app.plan == mock_plan
    assert mock_app.tree_overlay.visible is False
    assert mock_app.execute_btn.enabled is True
    assert "Plan rebuilt" in mock_app.status_label.text


def test_rebuilder_cancel(mock_app):
    mock_app.plan = {"Previous": {"old.txt": None}}
    mock_app._cancel_recalc_flag = True

    def mock_generate(base_dir, settings, locked_files, check_cancel):
        check_cancel()
        return {}
    mock_app.app_session.analyzer.generate_sorting_plan.side_effect = mock_generate

    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        thread = PlanRebuilderThread(
            mock_app,
            mock_app.base_dir,
            mock_app.settings,
            mock_app.locked_files,
            timeout=5.0,
        )
        thread.run()

    assert thread.cancelled is True

    # Run complete handler
    mock_app._previous_stable_plan = mock_app.plan.copy()
    mock_app._on_rebuild_complete(thread)

    # Previous stable plan should be restored
    assert mock_app.plan == {"Previous": {"old.txt": None}}
    assert "cancelled" in mock_app.status_label.text


def test_rebuilder_generative_fallback(mock_app):
    mock_app.app_session.analyzer.strategy_name = "generative"

    # First call with generative returns None/empty (failure),
    # second call with default strategy returns a fallback plan.
    fallback_plan = {"Fallback": {"file.txt": {"__type__": "file"}}}

    def mock_generate(base_dir, settings, locked_files, check_cancel):
        # Call check_cancel to verify it is called
        check_cancel()
        if mock_app.app_session.analyzer.strategy_name == "generative":
            return {}  # Indicates model/process failure or empty plan
        else:
            return fallback_plan

    mock_app.app_session.analyzer.generate_sorting_plan.side_effect = mock_generate

    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        thread = PlanRebuilderThread(
            mock_app,
            mock_app.base_dir,
            mock_app.settings,
            mock_app.locked_files,
            timeout=5.0,
        )
        thread.run()

    # Complete handler
    mock_app._on_rebuild_complete(thread)

    # Fallback plan should be applied with warning
    assert mock_app.plan == fallback_plan
    assert thread.warning is not None
    assert "warning" in mock_app.status_label.text


def test_rebuilder_timeout(mock_app):
    mock_app.plan = {"Stable": {"file.txt": None}}
    mock_app._previous_stable_plan = mock_app.plan.copy()

    def mock_generate(base_dir, settings, locked_files, check_cancel):
        check_cancel()
        return {}
    mock_app.app_session.analyzer.generate_sorting_plan.side_effect = mock_generate

    mock_loop = MagicMock()
    with patch("asyncio.get_running_loop", return_value=mock_loop):
        # Configure tiny timeout to trigger timeout immediately
        thread = PlanRebuilderThread(
            mock_app,
            mock_app.base_dir,
            mock_app.settings,
            mock_app.locked_files,
            timeout=-1.0,
        )
        thread.run()

    assert thread.timed_out is True

    mock_app._on_rebuild_complete(thread)

    # Stable plan is restored on failure/timeout
    assert mock_app.plan == {"Stable": {"file.txt": None}}
    assert "Error rebuilding plan" in mock_app.status_label.text
