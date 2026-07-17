import os
import json
import shutil
import tempfile
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

_keyring_file = os.path.join(tempfile.gettempdir(), "test_autosorter_keyring.json")

class FileKeyring(KeyringBackend):
    priority = 1
    def __init__(self, path):
        self.path = path
        if not os.path.exists(self.path):
            with open(self.path, "w") as f:
                json.dump({}, f)
    def _load(self):
        with open(self.path, "r") as f:
            return json.load(f)
    def _save(self, data):
        with open(self.path, "w") as f:
            json.dump(data, f)
    def get_password(self, service, username):
        return self._load().get(service, {}).get(username)
    def set_password(self, service, username, password):
        data = self._load()
        data.setdefault(service, {})[username] = password
        self._save(data)
    def delete_password(self, service, username):
        data = self._load()
        if service in data and username in data[service]:
            del data[service][username]
            self._save(data)
            
    def clear(self):
        self._save({})

_file_keyring = FileKeyring(_keyring_file)
keyring.set_keyring(_file_keyring)

@pytest.fixture(autouse=True)
def reset_memory_keyring():
    _file_keyring.clear()

@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
    temp_dir = tempfile.mkdtemp(prefix="test_autosorter_appdir_")
    
    def mock_get_app_dir():
        return Path(temp_dir)
        
    import app.config
    monkeypatch_session.setattr(app.config, "get_app_dir", mock_get_app_dir)
    
    yield
    
    shutil.rmtree(temp_dir, ignore_errors=True)
    if os.path.exists(_keyring_file):
        os.remove(_keyring_file)

@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch
    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()
