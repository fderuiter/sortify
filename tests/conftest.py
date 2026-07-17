import shutil
import tempfile
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.core.db import db


class MemoryKeyring(KeyringBackend):
    priority = 1
    def __init__(self):
        self.passwords = {}
    def get_password(self, service, username):
        return self.passwords.get(service, {}).get(username)
    def set_password(self, service, username, password):
        self.passwords.setdefault(service, {})[username] = password
    def delete_password(self, service, username):
        if service in self.passwords and username in self.passwords[service]:
            del self.passwords[service][username]

_memory_keyring = MemoryKeyring()

@pytest.fixture(autouse=True)
def reset_memory_keyring():
    _memory_keyring.passwords.clear()

@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
    # Use in-memory keyring for all tests
    keyring.set_keyring(_memory_keyring)
    
    # Use a temporary directory for all app data during tests
    temp_dir = tempfile.mkdtemp(prefix="test_autosorter_appdir_")
    
    def mock_get_app_dir():
        return Path(temp_dir)
        
    import app.config
    monkeypatch_session.setattr(app.config, "get_app_dir", mock_get_app_dir)
    
    # Also update the db singleton since it was initialized at import time
    import app.core.db
    app.core.db.clear_connection_cache()
    
    old_db_path = db.db_path
    db.db_path = str(mock_get_app_dir() / "autosorter.db")
    
    # Also update cache module since it was evaluated at import time
    import app.core.cache
    app.core.cache.DB_PATH = mock_get_app_dir() / "cache.db"

    import app.core.history
    app.core.history.history_manager.db_path = str(mock_get_app_dir() / "history.db")
    
    from app.core.db_init import init_databases
    init_databases()
    
    import app.core.history
    app.core.history.history_manager.db_path = str(mock_get_app_dir() / "history.db")
    app.core.history.init_history_db(app.core.history.history_manager.db_path)
    
    yield
    
    db.db_path = old_db_path
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()

