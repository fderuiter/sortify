import shutil
import tempfile
from pathlib import Path

import pytest

@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
    # Use a temporary directory for all app data during tests
    temp_dir = tempfile.mkdtemp(prefix="test_autosorter_appdir_")
    
    def mock_get_app_dir():
        return Path(temp_dir)
        
    import app.config
    monkeypatch_session.setattr(app.config, "get_app_dir", mock_get_app_dir)
    
    yield
    
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()
