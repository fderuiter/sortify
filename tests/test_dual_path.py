import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import AppSettings
from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.session import AppSession


@pytest.fixture
def mock_app_session_env():
    # We will use temp directories to simulate base_path and app_dir
    with tempfile.TemporaryDirectory() as base_temp:
        with tempfile.TemporaryDirectory() as app_temp:
            yield str(Path(base_temp).resolve()), str(Path(app_temp).resolve())


def test_session_dual_path_resolution_local_priority(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env

    # Create offline_bundle/model in local path
    local_model = os.path.join(base_temp, "offline_bundle", "model")
    os.makedirs(local_model)

    # Create model in user app path
    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True

    # Mock get_base_path and get_app_dir to control path resolution
    with patch("app.core.session.get_app_dir", return_value=Path(app_temp)):
        with patch("app.core.path_utils.get_base_path", return_value=base_temp):
            session = AppSession(settings, base_dir=base_temp)
            assert (
                Path(session.analyzer.model_path).resolve()
                == Path(local_model).resolve()
            )


def test_session_dual_path_resolution_user_fallback(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env

    # Only create model in user app path
    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True

    with patch("app.core.session.get_app_dir", return_value=Path(app_temp)):
        with patch("app.core.path_utils.get_base_path", return_value=base_temp):
            session = AppSession(settings, base_dir=base_temp)
            assert (
                Path(session.analyzer.model_path).resolve()
                == Path(user_model).resolve()
            )


def test_session_dual_path_resolution_no_model(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True

    with patch("app.core.session.get_app_dir", return_value=Path(app_temp)):
        with patch("app.core.path_utils.get_base_path", return_value=base_temp):
            session = AppSession(settings, base_dir=base_temp)
            assert session.analyzer.model_path is None


def test_strategy_dual_path_resolution_local_priority(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env

    local_model = os.path.join(base_temp, "offline_bundle", "model")
    os.makedirs(local_model)

    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)

    with patch("app.config.get_app_dir", return_value=Path(app_temp)):
        with patch("app.core.path_utils.get_base_path", return_value=base_temp):
            strategy = GenerativeNamingStrategy()
            assert Path(strategy.model_path).resolve() == Path(local_model).resolve()


def test_strategy_dual_path_resolution_user_fallback(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env

    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)

    with patch("app.config.get_app_dir", return_value=Path(app_temp)):
        with patch("app.core.path_utils.get_base_path", return_value=base_temp):
            strategy = GenerativeNamingStrategy()
            assert Path(strategy.model_path).resolve() == Path(user_model).resolve()


def test_setup_wizard_requires_explicit_consent_with_local_models(mock_app_session_env):
    from app.ui.app import AutoSorterApp

    base_temp, app_temp = mock_app_session_env

    # Create local config.json
    local_model = os.path.join(base_temp, "offline_bundle", "model")
    os.makedirs(local_model)
    with open(os.path.join(local_model, "config.json"), "w") as f:
        f.write("{}")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = None
    app = AutoSorterApp(settings)

    with patch("app.config.get_app_dir", return_value=Path(app_temp)):
        with patch("app.core.path_utils.get_base_path", return_value=base_temp):
            with patch("app.ui.wizard.show_wizard") as mock_show_wizard:
                app.check_setup_wizard()
                # Consent must NOT be automatically granted
                assert settings.AI_CONSENT_GRANTED is None
                # Show wizard must be invoked
                mock_show_wizard.assert_called_once_with(app, settings)


def test_setup_wizard_skips_when_consent_already_decided(mock_app_session_env):
    from app.ui.app import AutoSorterApp

    base_temp, app_temp = mock_app_session_env

    for consent_state in (True, False):
        settings = AppSettings()
        settings.AI_CONSENT_GRANTED = consent_state
        app = AutoSorterApp(settings)

        with patch("app.ui.wizard.show_wizard") as mock_show_wizard:
            app.check_setup_wizard()
            assert settings.AI_CONSENT_GRANTED is consent_state
            mock_show_wizard.assert_not_called()
