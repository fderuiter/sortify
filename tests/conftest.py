import shutil
import tempfile
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.core.db import db


class InMemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.passwords = {}

    def get_password(self, servicename, username):
        return self.passwords.get((servicename, username))

    def set_password(self, servicename, username, password):
        self.passwords[(servicename, username)] = password

    def delete_password(self, servicename, username):
        if (servicename, username) in self.passwords:
            del self.passwords[(servicename, username)]
        else:
            from keyring.errors import PasswordDeleteError
            raise PasswordDeleteError("Password not found")

@pytest.fixture(scope="session", autouse=True)
def mock_keyring():
    original_keyring = keyring.get_keyring()
    mock_backend = InMemoryKeyring()
    keyring.set_keyring(mock_backend)
    yield mock_backend
    keyring.set_keyring(original_keyring)


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
    
    # Also update cache module since it was evaluated at import time
    import app.core.cache
    app.core.cache.DB_PATH = mock_get_app_dir() / "cache.db"

    import app.core.history
    app.core.history.history_manager.db_path = str(mock_get_app_dir() / "history.db")
    
    from app.core.db_init import init_databases
    init_databases()
    
    yield
    
    db.db_path = old_db_path
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()

