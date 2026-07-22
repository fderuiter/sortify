import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.core.db_conn import clear_connection_cache


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
            
    def clear(self):
        self.passwords.clear()

_memory_keyring = MemoryKeyring()
keyring.set_keyring(_memory_keyring)

@pytest.fixture(autouse=True)
def reset_memory_keyring():
    pass # _memory_keyring.clear() removed to preserve session scoped keys in tests

@pytest.fixture(autouse=True)
def sync_db_worker():
    """Ensure all database writes happen synchronously during tests to prevent race conditions on Windows."""
    from app.core.db_worker import DBWorker
    
    def sync_execute(self, func, *args, **kwargs):
        return self.execute_write(func, *args, **kwargs)
        
    with patch.object(DBWorker, 'execute_write_async', sync_execute):
        yield

@pytest.fixture(autouse=True)
def cleanup_db_connections():
    """Ensure database connections are closed after each test to prevent Windows file locking issues."""
    yield
    clear_connection_cache()

@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
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

