"""Tests for setup wizard and app initialization."""

from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.wizard import show_wizard


def test_show_wizard_callable(tmp_path):
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.build_ui()
    # Ensure show_wizard can be invoked safely without nicegui
    show_wizard(app, settings)
    assert True


def test_show_wizard_with_local_models():
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = None
    app = MagicMock()

    with patch("app.ui.wizard.check_local_models_exist", return_value=True):
        with patch("app.ui.wizard.OverflowToolbar") as mock_toolbar_cls:
            mock_toolbar = MagicMock()
            mock_toolbar_cls.return_value = mock_toolbar
            show_wizard(app, settings)

            # Check primary action button added with "Enable AI Features"
            add_action_calls = mock_toolbar.add_action.call_args_list
            first_call_args = add_action_calls[0][0]
            assert first_call_args[0] == "Enable AI Features"

            # Execute the on_click handler for accept
            accept_fn = add_action_calls[0][1]["on_click"]
            accept_fn()

            assert settings.AI_CONSENT_GRANTED is True


def test_show_wizard_without_local_models():
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = None
    app = MagicMock()

    with patch("app.ui.wizard.check_local_models_exist", return_value=False):
        with patch("app.ui.wizard.OverflowToolbar") as mock_toolbar_cls:
            mock_toolbar = MagicMock()
            mock_toolbar_cls.return_value = mock_toolbar
            show_wizard(app, settings)

            add_action_calls = mock_toolbar.add_action.call_args_list
            first_call_args = add_action_calls[0][0]
            assert first_call_args[0] == "Accept & Download"


def test_show_wizard_decline():
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = None
    app = MagicMock()

    with patch("app.ui.wizard.OverflowToolbar") as mock_toolbar_cls:
        mock_toolbar = MagicMock()
        mock_toolbar_cls.return_value = mock_toolbar
        show_wizard(app, settings)

        add_action_calls = mock_toolbar.add_action.call_args_list
        # Second action is "Decline"
        decline_fn = add_action_calls[1][1]["on_click"]
        decline_fn()

        assert settings.AI_CONSENT_GRANTED is False
