import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import AppSettings
from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.session import AppSession


@pytest.fixture
def mock_app_session_env(tmp_path):
    # We will use temp directories to simulate base_path and app_dir
    base_temp = str(tmp_path / "base")
    app_temp = str(tmp_path / "app")
    os.makedirs(base_temp)
    os.makedirs(app_temp)
    yield base_temp, app_temp

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
    
    # Mock sys.frozen and __file__ to control base_path, and get_app_dir to control user app dir
    with patch("app.core.session.get_app_dir", return_value=Path(app_temp)):
        import sys
        with patch.object(sys, "frozen", False, create=True):
            with patch.object(sys, "executable", os.path.join(base_temp, "app.exe"), create=True):
                with patch("app.core.session.os.path.abspath", return_value=os.path.join(base_temp, "app", "core", "session.py")):
                    session = AppSession(settings, base_dir=base_temp)
                    assert session.analyzer.model_path == local_model
                    session.close()

def test_session_dual_path_resolution_user_fallback(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env
    
    # Only create model in user app path
    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)
    
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True
    
    with patch("app.core.session.get_app_dir", return_value=Path(app_temp)):
        import sys
        with patch.object(sys, "frozen", False, create=True):
            with patch("app.core.session.os.path.abspath", return_value=os.path.join(base_temp, "app", "core", "session.py")):
                session = AppSession(settings, base_dir=base_temp)
                assert session.analyzer.model_path == user_model
                session.close()

def test_session_dual_path_resolution_no_model(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env
    
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True
    
    with patch("app.core.session.get_app_dir", return_value=Path(app_temp)):
        import sys
        with patch.object(sys, "frozen", False, create=True):
            with patch("app.core.session.os.path.abspath", return_value=os.path.join(base_temp, "app", "core", "session.py")):
                session = AppSession(settings, base_dir=base_temp)
                assert session.analyzer.model_path is None
                session.close()

def test_strategy_dual_path_resolution_local_priority(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env
    
    local_model = os.path.join(base_temp, "offline_bundle", "model")
    os.makedirs(local_model)
    
    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)
    
    with patch("app.config.get_app_dir", return_value=Path(app_temp)):
        import sys
        with patch.object(sys, "frozen", False, create=True):
            with patch("app.core.analyzer_strategies.os.path.abspath", return_value=os.path.join(base_temp, "app", "core", "analyzer_strategies.py")):
                strategy = GenerativeNamingStrategy()
                assert strategy.model_path == local_model

def test_strategy_dual_path_resolution_user_fallback(mock_app_session_env):
    base_temp, app_temp = mock_app_session_env
    
    user_model = os.path.join(app_temp, "model")
    os.makedirs(user_model)
    
    with patch("app.config.get_app_dir", return_value=Path(app_temp)):
        import sys
        with patch.object(sys, "frozen", False, create=True):
            with patch("app.core.analyzer_strategies.os.path.abspath", return_value=os.path.join(base_temp, "app", "core", "analyzer_strategies.py")):
                strategy = GenerativeNamingStrategy()
                assert strategy.model_path == user_model

def test_setup_wizard_bypass_dual_path(mock_app_session_env):
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
        import sys
        with patch.object(sys, "frozen", False, create=True):
            with patch("app.ui.app.os.path.abspath", return_value=os.path.join(base_temp, "app", "ui", "app.py")):
                app.check_setup_wizard()
                assert settings.AI_CONSENT_GRANTED is True
