import contextlib
from unittest.mock import ANY, MagicMock, patch

import pytest

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.wizard import show_wizard


@pytest.fixture
def mock_nicegui():
    with contextlib.ExitStack() as stack:
        mock_dialog = stack.enter_context(patch("nicegui.ui.dialog"))
        mock_card = stack.enter_context(patch("nicegui.ui.card"))
        mock_label = stack.enter_context(patch("nicegui.ui.label"))
        mock_column = stack.enter_context(patch("nicegui.ui.column"))
        mock_row = stack.enter_context(patch("nicegui.ui.row"))
        mock_button = stack.enter_context(patch("nicegui.ui.button"))
        mock_input = stack.enter_context(patch("nicegui.ui.input"))
        mock_progress = stack.enter_context(patch("nicegui.ui.linear_progress"))
        mock_timer = stack.enter_context(patch("nicegui.ui.timer"))
        mock_expansion = stack.enter_context(patch("nicegui.ui.expansion"))
        mock_tabs = stack.enter_context(patch("nicegui.ui.tabs"))
        mock_tab = stack.enter_context(patch("nicegui.ui.tab"))
        mock_tab_panels = stack.enter_context(patch("nicegui.ui.tab_panels"))
        mock_tab_panel = stack.enter_context(patch("nicegui.ui.tab_panel"))
        mock_icon = stack.enter_context(patch("nicegui.ui.icon"))
        mock_notify = stack.enter_context(patch("nicegui.ui.notify"))
        mock_switch = stack.enter_context(patch("nicegui.ui.switch"))
        mock_number = stack.enter_context(patch("nicegui.ui.number"))
        mock_slider = stack.enter_context(patch("nicegui.ui.slider"))
        mock_select = stack.enter_context(patch("nicegui.ui.select"))
        mock_checkbox = stack.enter_context(patch("nicegui.ui.checkbox"))
        mock_link = stack.enter_context(patch("nicegui.ui.link"))
        mock_scroll_area = stack.enter_context(patch("nicegui.ui.scroll_area"))
        mock_markdown = stack.enter_context(patch("nicegui.ui.markdown"))

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
            "switch": mock_switch,
            "number": mock_number,
            "slider": mock_slider,
            "select": mock_select,
            "checkbox": mock_checkbox,
            "link": mock_link,
            "scroll_area": mock_scroll_area,
            "markdown": mock_markdown,
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
            value="http://test-proxy:8080",
            password=True,
            password_toggle_button=True,
        )

        # Let's verify the Download button exists
        mock_nicegui["button"].assert_any_call("Download AI Model", on_click=ANY)


def test_settings_panel_with_validation_errors(mock_nicegui):
    settings = AppSettings()
    settings._has_validation_errors = True
    settings._validation_errors = [{"field": "PROXY", "message": "Invalid proxy URL"}]

    app = AutoSorterApp(settings)

    with (
        patch("app.core.verifier.check_ai_status", return_value=(False, "Warning")),
    ):
        app.show_settings_view()

        # Should render warning banner including link and label
        mock_nicegui["label"].assert_any_call("Configuration Saves Suspended")
        mock_nicegui["link"].assert_any_call(
            "Open Troubleshooting Guide (Online)",
            "https://docs.smartautosorter.com/admin_guide/#configuration-recovery-troubleshooting",
            new_tab=True,
        )
