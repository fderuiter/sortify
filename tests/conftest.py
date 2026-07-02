import shutil
import tempfile
from pathlib import Path

import pytest

from app.core.db import db


@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
    # Use a temporary directory for all app data during tests
    temp_dir = tempfile.mkdtemp(prefix="test_autosorter_appdir_")
    
    def mock_get_app_dir():
        return Path(temp_dir)
        
    import app.config
    monkeypatch_session.setattr(app.config, "get_app_dir", mock_get_app_dir)
    
    # Also update the db singleton since it was initialized at import time
    old_db_path = db.db_path
    db.db_path = str(mock_get_app_dir() / "autosorter.db")
    db._init_db()
    
    # Also update cache module since it was evaluated at import time
    import app.core.cache
    app.core.cache.DB_PATH = mock_get_app_dir() / "cache.db"
    
    yield
    
    db.db_path = old_db_path
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()

