import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.core.db_conn import clear_connection_cache

os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

# Clear pre-loaded standard database modules to prevent library collision
for mod_name in list(sys.modules.keys()):
    if (
        mod_name in ("sqlcipher3", "_sqlite3", "sqlite3", "pysqlcipher3")
        or mod_name.startswith("sqlcipher3.")
        or mod_name.startswith("sqlite3.")
        or mod_name.startswith("pysqlcipher3.")
    ):
        sys.modules.pop(mod_name, None)


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
def sync_db_worker(request):
    """Ensure all database writes happen synchronously during tests to prevent race conditions on Windows.

    Mark a test ``real_db_worker`` to run it against the real asynchronous write queue.
    """
    from app.core.db_worker import DBWorker

    if request.node.get_closest_marker("real_db_worker"):
        yield
        return

    def sync_execute(self, func, *args, **kwargs):
        import threading

        if threading.current_thread() is getattr(self, "thread", None):
            return func(*args, **kwargs)
        return self.execute_write(func, *args, **kwargs)

    with patch.object(DBWorker, "execute_write_async", sync_execute):
        yield


@pytest.fixture
def session_db(tmp_path):
    """A real Database for a stand-in AppSession, so quarantine staging hits real tables."""
    from app.core.db import Database
    from app.core.db_worker import DBWorker

    worker = DBWorker()
    try:
        yield Database(tmp_path / "session.db", worker)
    finally:
        worker.stop(stop_embeddings=False)


@pytest.fixture(autouse=True)
def inline_clustering(request, monkeypatch):
    """Cluster in-process so tests can patch and inspect strategy state.

    Mark a test ``subprocess_clustering`` to run the spawned child-process path.
    """
    if request.node.get_closest_marker("subprocess_clustering"):
        return
    from app.core.analyzer_strategies import RecursiveKMeansStrategy

    monkeypatch.setattr(RecursiveKMeansStrategy, "run_in_subprocess", False)


@pytest.fixture(autouse=True)
def embedding_test_defaults(monkeypatch):
    """Keep test threads at normal priority and skip model checks on stand-in models."""
    from app.core import semantic_embeddings

    monkeypatch.setattr(semantic_embeddings, "LOWER_THREAD_PRIORITY", False)
    monkeypatch.setattr(
        semantic_embeddings.SemanticEmbeddingManager, "validate_on_init", False
    )


@pytest.fixture(autouse=True)
def cleanup_db_connections():
    """Ensure database connections are closed after each test to prevent Windows file locking issues."""
    yield
    try:
        from app.core.semantic_embeddings import SemanticEmbeddingManager

        SemanticEmbeddingManager.stop_all()
    except Exception:
        pass
    clear_connection_cache(only_current_and_inactive=False)


@pytest.fixture(autouse=True)
def reset_shared_registry():
    """Reset singletons (SharedModelRegistry, SharedWorkerPool, DownloadManager) and AppSettings observers before and after each test to prevent test pollution."""
    from app.config import AppSettings
    from app.core.downloader import DownloadManager
    from app.core.shared_registry import (
        SharedModelRegistry,
        SharedWorkerPool,
        _thread_local,
    )

    def _cleanup_thread_local():
        if hasattr(_thread_local, "sandboxed"):
            try:
                delattr(_thread_local, "sandboxed")
            except Exception:
                pass
        if hasattr(_thread_local, "reason"):
            try:
                delattr(_thread_local, "reason")
            except Exception:
                pass

    _cleanup_thread_local()
    AppSettings.clear_observers()
    DownloadManager.reset_instance()
    SharedModelRegistry._instance = None
    if SharedWorkerPool._instance is not None:
        try:
            SharedWorkerPool._instance.shutdown(wait=False)
        except Exception:
            pass
        SharedWorkerPool._instance = None
    yield
    _cleanup_thread_local()
    AppSettings.clear_observers()
    DownloadManager.reset_instance()
    SharedModelRegistry._instance = None
    if SharedWorkerPool._instance is not None:
        try:
            SharedWorkerPool._instance.shutdown(wait=False)
        except Exception:
            pass
        SharedWorkerPool._instance = None


@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
    from app.core.resilient_file_ops import resilient_rmtree

    temp_dir = tempfile.mkdtemp(prefix="test_autosorter_appdir_")
    monkeypatch_session.setenv("AUTOSORTER_APP_DIR", temp_dir)

    def mock_get_app_dir():
        app_dir = os.environ.get("AUTOSORTER_APP_DIR")
        if app_dir:
            return Path(app_dir).resolve()
        return Path(temp_dir).resolve()

    import app.config

    monkeypatch_session.setattr(app.config, "get_app_dir", mock_get_app_dir)

    yield

    resilient_rmtree(temp_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))


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
    """Replace the real socket connect calls behind the network guard with mocks.

    The guard's sandbox check still runs, so tests can assert both that blocked
    hosts raise and that allowed hosts reach the (mocked) real connect.
    """
    from unittest.mock import MagicMock

    from app.core import network_guard

    network_guard.install()
    mock_connect = MagicMock()
    mock_connect_ex = MagicMock()
    monkeypatch.setattr(network_guard, "_real_connect", mock_connect)
    monkeypatch.setattr(network_guard, "_real_connect_ex", mock_connect_ex)

    yield mock_connect, mock_connect_ex


def pytest_runtest_logreport(report):
    """Intercept pytest failures across all phases and output them as GitHub Actions error annotations to capture raw tracebacks."""
    if report.failed:
        import sys

        tb = str(report.longrepr)
        message = f"Test Failed ({report.when}): {report.nodeid}\n\n{tb}"

        # Write to pytest_failures.txt
        try:
            with open("pytest_failures.txt", "a", encoding="utf-8") as f:
                f.write(f"=== [{report.when}] {report.nodeid} ===\n{tb}\n\n")
        except Exception:
            pass

        # Write to uncaptured stdout to bypass pytest buffering
        try:
            message_escaped = message.replace("\r", "").replace("\n", "%0A")
            sys.__stdout__.write(f"\n::error:: {message_escaped}\n")
            sys.__stdout__.flush()
        except Exception:
            pass
