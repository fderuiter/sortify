import os
import sys

if sys.platform == "win32":
    # Inject DLL directory paths for Windows to allow direct import of sqlcipher3
    import importlib.util

    try:
        # 1. Add sqlcipher3 package directory
        spec = importlib.util.find_spec("sqlcipher3")
        if spec and spec.submodule_search_locations:
            pkg_dir = spec.submodule_search_locations[0]
            if os.path.isdir(pkg_dir):
                try:
                    os.add_dll_directory(pkg_dir)
                except Exception:
                    pass

        # 2. Collect other potential DLL directories
        dirs_to_add = []

        # Add virtualenv paths
        venv_dirs = []
        v_env = os.environ.get("VIRTUAL_ENV")
        if v_env:
            venv_dirs.append(v_env)
        if sys.prefix and sys.prefix not in venv_dirs:
            venv_dirs.append(sys.prefix)

        for vd in venv_dirs:
            for sub in [
                ".",
                "Library/bin",
                "Scripts",
                "DLLs",
                "Lib/site-packages/sqlcipher3",
            ]:
                try:
                    p = os.path.abspath(os.path.join(vd, sub))
                    if os.path.isdir(p) and p not in dirs_to_add:
                        dirs_to_add.append(p)
                except Exception:
                    pass

        # Add common OpenSSL paths
        common_openssl_dirs = [
            "C:\\Program Files\\OpenSSL-Win64\\bin",
            "C:\\Program Files\\OpenSSL\\bin",
            "C:\\Program Files\\OpenSSL-Win64",
            "C:\\Program Files\\OpenSSL",
            "C:\\OpenSSL-Win64\\bin",
            "C:\\OpenSSL-Win64",
            "C:\\Program Files\\Common Files\\SSL",
        ]
        for cod in common_openssl_dirs:
            try:
                if os.path.isdir(cod) and cod not in dirs_to_add:
                    dirs_to_add.append(cod)
            except Exception:
                pass

        # Register all these paths via os.add_dll_directory and prepending to PATH
        for p in dirs_to_add:
            try:
                os.add_dll_directory(p)
            except Exception:
                pass

        # Update PATH environment variable without duplicating entries
        current_path_dirs = [d.strip().strip('"') for d in os.environ.get("PATH", "").replace(os.pathsep, ";").split(";") if d.strip()]
        current_path_dirs_normalized = set()
        for d in current_path_dirs:
            try:
                current_path_dirs_normalized.add(os.path.abspath(d).lower())
            except Exception:
                pass
        new_path_dirs = []
        for p in dirs_to_add:
            try:
                abs_p = os.path.abspath(p)
                if abs_p.lower() not in current_path_dirs_normalized and abs_p.lower() not in [np.lower() for np in new_path_dirs]:
                    new_path_dirs.append(abs_p)
            except Exception:
                pass
        if new_path_dirs:
            os.environ["PATH"] = (
                ";".join(new_path_dirs) + ";" + os.environ.get("PATH", "")
            )
    except Exception:
        pass

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
    pass  # _memory_keyring.clear() removed to preserve session scoped keys in tests


@pytest.fixture(autouse=True)
def sync_db_worker():
    """Ensure all database writes happen synchronously during tests to prevent race conditions on Windows."""
    from app.core.db_worker import DBWorker

    def sync_execute(self, func, *args, **kwargs):
        return self.execute_write(func, *args, **kwargs)

    with patch.object(DBWorker, "execute_write_async", sync_execute):
        yield


@pytest.fixture(autouse=True)
def cleanup_db_connections():
    """Ensure database connections are closed after each test to prevent Windows file locking issues."""
    yield
    try:
        from app.core.semantic_embeddings import SemanticEmbeddingManager

        SemanticEmbeddingManager.stop_all()
    except Exception:
        pass
    clear_connection_cache()


@pytest.fixture(autouse=True)
def reset_shared_registry():
    """Reset the SharedModelRegistry and SharedWorkerPool singletons before and after each test to prevent test pollution."""
    from app.core.shared_registry import SharedModelRegistry, SharedWorkerPool

    SharedModelRegistry._instance = None
    if SharedWorkerPool._instance is not None:
        try:
            SharedWorkerPool._instance.shutdown(wait=False)
        except Exception:
            pass
        SharedWorkerPool._instance = None
    yield
    SharedModelRegistry._instance = None
    if SharedWorkerPool._instance is not None:
        try:
            SharedWorkerPool._instance.shutdown(wait=False)
        except Exception:
            pass
        SharedWorkerPool._instance = None


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


@pytest.fixture
def test_history_env(tmp_path):
    """Consolidated test environment helper for history, database, and cache."""
    import os

    from app.core.cache import CacheManager
    from app.core.db import Database
    from app.core.db_worker import DBWorker
    from app.core.history import HistoryManager

    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()
    db_path = tmp_path / "test_docs.db"
    db = Database(db_path, worker=db_worker)

    cache_path = tmp_path / "test_cache.db"
    cache = CacheManager(str(cache_path), worker=db_worker)

    history_manager = HistoryManager(db, cache, str(tmp_path / "test_history.db"))

    yield base_dir, db, cache, history_manager, db_worker
    db_worker.stop()


@pytest.fixture
def socket_mock(monkeypatch):
    """Fixture that selectively mocks socket connect operations only for test-created sockets,
    preventing any potential background/system loopback connection failures on Windows.
    """
    import sys
    from unittest.mock import MagicMock

    import app.core.shared_registry

    real_connect = app.core.shared_registry._original_connect
    real_connect_ex = app.core.shared_registry._original_connect_ex

    def is_called_from_test():
        try:
            frame = sys._getframe()
            while frame:
                filename = getattr(frame.f_code, "co_filename", None)
                if filename:
                    filename_lower = filename.lower()
                    if (
                        "test_db_worker_sandbox" in filename_lower
                        or "test_shared_registry" in filename_lower
                    ):
                        return True
                frame = frame.f_back
        except Exception:
            pass
        return False

    mock_connect = MagicMock()
    mock_connect_ex = MagicMock()

    def side_effect_connect(self, address):
        if is_called_from_test():
            return mock_connect(self, address)
        return real_connect(self, address)

    def side_effect_connect_ex(self, address):
        if is_called_from_test():
            return mock_connect_ex(self, address)
        return real_connect_ex(self, address)

    mock_connect_wrapper = MagicMock(side_effect=side_effect_connect)
    mock_connect_ex_wrapper = MagicMock(side_effect=side_effect_connect_ex)

    monkeypatch.setattr(
        app.core.shared_registry, "_original_connect", mock_connect_wrapper
    )
    monkeypatch.setattr(
        app.core.shared_registry, "_original_connect_ex", mock_connect_ex_wrapper
    )

    yield mock_connect, mock_connect_ex
