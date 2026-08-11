import os
import sys
from unittest.mock import MagicMock, patch, ANY

import pytest

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.wizard import show_wizard


@pytest.fixture
def mock_nicegui():
    with (
        patch("nicegui.ui.dialog") as mock_dialog,
        patch("nicegui.ui.card") as mock_card,
        patch("nicegui.ui.label") as mock_label,
        patch("nicegui.ui.column") as mock_column,
        patch("nicegui.ui.row") as mock_row,
        patch("nicegui.ui.button") as mock_button,
        patch("nicegui.ui.input") as mock_input,
        patch("nicegui.ui.linear_progress") as mock_progress,
        patch("nicegui.ui.timer") as mock_timer,
        patch("nicegui.ui.expansion") as mock_expansion,
        patch("nicegui.ui.tabs") as mock_tabs,
        patch("nicegui.ui.tab") as mock_tab,
        patch("nicegui.ui.tab_panels") as mock_tab_panels,
        patch("nicegui.ui.tab_panel") as mock_tab_panel,
        patch("nicegui.ui.icon") as mock_icon,
        patch("nicegui.ui.notify") as mock_notify,
    ):
        yield {
            "dialog": mock_dialog,
            "card": mock_card,
            "label": mock_label,
            "column": mock_column,
            "row": mock_row,
            "button": mock_button,
            "input": mock_input,
            "linear_progress": mock_progress,
            "timer": mock_timer,
            "icon": mock_icon,
            "notify": mock_notify,
        }


def test_show_wizard_renders_with_mocked_ui(mock_nicegui):
    settings = AppSettings()
    # Ensure PROXY is initially empty
    settings.PROXY = ""
    settings.AI_CONSENT_GRANTED = None

    parent_app = MagicMock()

    show_wizard(parent_app, settings)

    # Verify dialog was initialized
    mock_nicegui["dialog"].assert_called()
    # Verify we put a title on it
    mock_nicegui["label"].assert_any_call("AI Features Setup")


def test_check_setup_wizard_triggers_when_model_missing(mock_nicegui):
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = None

    app = AutoSorterApp(settings)

    # We mock path existences to ensure local_model_dir and user_model_dir config.json do not exist
    with (
        patch("os.path.exists", return_value=False),
        patch("pathlib.Path.exists", return_value=False),
        patch("app.ui.wizard.show_wizard") as mock_show_wizard,
    ):
        app.check_setup_wizard()
        mock_show_wizard.assert_called_once_with(app, settings)


def test_settings_panel_contains_proxy_and_download(mock_nicegui):
    settings = AppSettings()
    settings.PROXY = "http://test-proxy:8080"

    app = AutoSorterApp(settings)

    with (
        patch("app.core.verifier.check_ai_status", return_value=(False, "Warning")),
    ):
        app.show_settings_view()

        # The proxy input should have been instantiated with settings value
        mock_nicegui["input"].assert_any_call(
            "Proxy Server (e.g. http://127.0.0.1:8080)",
            value="http://test-proxy:8080"
        )

        # Let's verify the Download button exists
        mock_nicegui["button"].assert_any_call(
            "Download AI Model", on_click=ANY
        )
