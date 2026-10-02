"""Tests for setup wizard and app initialization."""

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.notifications import notify
from app.ui.wizard import show_wizard


def test_show_wizard_callable(tmp_path):
    settings = AppSettings()
    app = AutoSorterApp(settings)
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


def test_conditional_import_nicegui_active():
    """Verify wizard, settings, and app modules use real nicegui.ui when available."""
    import app.ui.app as app_mod
    import app.ui.settings as settings_mod
    import app.ui.wizard as wizard_mod

    fake_nicegui = types.ModuleType("nicegui")
    fake_ui = MagicMock()
    fake_dialog = MagicMock()
    fake_ui.dialog = MagicMock(return_value=fake_dialog)
    fake_nicegui.ui = fake_ui

    with patch.dict(sys.modules, {"nicegui": fake_nicegui, "nicegui.ui": fake_ui}):
        importlib.reload(wizard_mod)
        importlib.reload(settings_mod)
        importlib.reload(app_mod)

        assert wizard_mod.ui is fake_ui
        assert settings_mod.ui is fake_ui
        assert app_mod.ui is fake_ui
        assert wizard_mod.ui.notify == notify

        # Calling show_wizard uses fake_ui (the imported nicegui module), not a MagicMock
        settings = AppSettings()
        app_mock = MagicMock()
        wizard_mod.show_wizard(app_mock, settings)
        assert fake_ui.dialog.called
        assert fake_ui.card.called

    # Clean reload back to headless mode after test
    sys.modules.pop("nicegui", None)
    sys.modules.pop("nicegui.ui", None)
    importlib.reload(wizard_mod)
    importlib.reload(settings_mod)
    importlib.reload(app_mod)


def test_conditional_import_nicegui_missing():
    """Verify wizard, settings, and app modules fall back to MagicMock when nicegui is not installed."""
    sys.modules.pop("nicegui", None)
    sys.modules.pop("nicegui.ui", None)

    import app.ui.app as app_mod
    import app.ui.settings as settings_mod
    import app.ui.wizard as wizard_mod

    importlib.reload(wizard_mod)
    importlib.reload(settings_mod)
    importlib.reload(app_mod)

    assert isinstance(wizard_mod.ui, MagicMock)
    assert isinstance(settings_mod.ui, MagicMock)
    assert isinstance(app_mod.ui, MagicMock)
    assert wizard_mod.ui.notify == notify

