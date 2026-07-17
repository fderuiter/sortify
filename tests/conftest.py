import json
import os
import shutil
import tempfile
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

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
    _memory_keyring.clear()

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
