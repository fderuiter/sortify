import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

import keyring
import pytest
from keyring.backends.fail import Keyring as FailKeyring

from app.core.db_conn import (
    _connection_cache,
    clear_connection_cache,
)

# Clear pre-loaded standard database modules to prevent library collision
for mod_name in list(sys.modules.keys()):
    if mod_name in (
        "sqlcipher3",
        "_sqlite3",
        "sqlite3",
        "pysqlcipher3",
    ) or mod_name.startswith(("sqlcipher3.", "sqlite3.", "pysqlcipher3.")):
        sys.modules.pop(mod_name, None)

keyring.set_keyring(FailKeyring())


@pytest.fixture
def sync_db_worker(request):
    """Run all database writes synchronously for white-box tests that explicitly opt in.

    Tests run against the real asynchronous ``DBWorker`` write queue by default;
    request ``sync_db_worker`` or mark a test ``@pytest.mark.sync_db_worker`` to
    execute ``execute_write_async`` synchronously.
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


@pytest.fixture
def inline_clustering(request, monkeypatch):
    """Cluster in-process for white-box tests that patch or inspect strategy state.

    Tests run ``RecursiveKMeansStrategy`` in its spawned child process by default;
    request ``inline_clustering`` or mark a test ``@pytest.mark.inline_clustering``
    to run clustering inline.
    """
    if request.node.get_closest_marker("subprocess_clustering"):
        return
    from app.core.analyzer_strategies import RecursiveKMeansStrategy

    monkeypatch.setattr(RecursiveKMeansStrategy, "run_in_subprocess", False)


def pytest_collection_modifyitems(items):
    """Attach opt-in white-box runtime override fixtures when requested via markers."""
    for item in items:
        fixturenames = getattr(item, "fixturenames", None)
        if fixturenames is None:
            continue
        if (
            item.get_closest_marker("inline_clustering") is not None
            and "inline_clustering" not in fixturenames
        ):
            fixturenames.append("inline_clustering")
        if (
            item.get_closest_marker("sync_db_worker") is not None
            and "sync_db_worker" not in fixturenames
        ):
            fixturenames.append("sync_db_worker")


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
    """Ensure embedding workers are stopped after each test before registry teardown closes connections."""
    yield
    try:
        from app.core.semantic_embeddings import SemanticEmbeddingManager

        SemanticEmbeddingManager.stop_all()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_shared_registry():
    """Reset singletons (SharedModelRegistry, SharedWorkerPool, DownloadManager) and AppSettings observers before and after each test to prevent test pollution."""
    import shutil

    from app.config import AppSettings, get_app_dir
    from app.core.downloader import _VERIFICATION_CACHE, DownloadManager
    from app.core.network_guard import _thread_local
    from app.core.shared_registry import SharedModelRegistry, SharedWorkerPool

    def _cleanup_test_app_dir(*, clear_cache: bool = True):
        if DownloadManager._instance is not None:
            try:
                DownloadManager._instance.cancel_download()
                thread = DownloadManager._instance.current_thread
                if thread is not None and thread.is_alive():
                    thread.join(timeout=1.0)
            except Exception:
                pass
        _VERIFICATION_CACHE.clear()
        try:
            from app.core.semantic_embeddings import SemanticEmbeddingManager

            SemanticEmbeddingManager.stop_all()
        except Exception:
            pass
        # Some older UI tests do not close their AppSession. Drain managed
        # services before deleting their stores, just as other singletons reset.
        from app.core import services

        had_services = bool(services._services)
        for graph in list(services._services.values()):
            graph.worker.stop(stop_embeddings=False)
        services._services.clear()
        if clear_cache or had_services or _connection_cache:
            clear_connection_cache(only_current_and_inactive=False)
        try:
            app_dir = get_app_dir()
            for filename in (
                "settings.json",
                "sortify.db",
                "sortify.db-wal",
                "sortify.db-shm",
                "rollback_journal.json",
                "transaction_ledger.db",
                "transaction_ledger.db-wal",
                "transaction_ledger.db-shm",
            ):
                target = app_dir / filename
                if target.exists():
                    target.unlink()
            for dirname in ("sessions", "model"):
                sub_dir = app_dir / dirname
                if sub_dir.exists():
                    shutil.rmtree(sub_dir, ignore_errors=True)
        except Exception:
            pass

    _cleanup_test_app_dir(clear_cache=False)
    AppSettings.clear_observers()
    DownloadManager.reset_instance()
    SharedModelRegistry._instance = None
    _thread_local.sandboxed = False
    _thread_local.reason = ""
    if SharedWorkerPool._instance is not None:
        try:
            SharedWorkerPool._instance.shutdown(wait=False)
        except Exception:
            pass
        SharedWorkerPool._instance = None
    yield
    _cleanup_test_app_dir(clear_cache=True)
    AppSettings.clear_observers()
    DownloadManager.reset_instance()
    SharedModelRegistry._instance = None
    _thread_local.sandboxed = False
    _thread_local.reason = ""
    if SharedWorkerPool._instance is not None:
        try:
            SharedWorkerPool._instance.shutdown(wait=False)
        except Exception:
            pass
        SharedWorkerPool._instance = None


@pytest.fixture(autouse=True)
def block_live_external_downloads(monkeypatch):
    """Prevent unmocked DownloadManager calls in unit tests from downloading remote model weights."""
    import urllib.request

    from app.core.downloader import DownloadError, DownloadManager

    orig_get_opener = DownloadManager.get_opener

    def _guarded_get_opener(self, proxy: str | None = None):
        opener = orig_get_opener(self, proxy)
        if type(opener) is urllib.request.OpenerDirector:

            def _blocked_open(fullurl, data=None, timeout=None):
                raise DownloadError("External model download blocked in unit tests")

            opener.open = _blocked_open
        return opener

    monkeypatch.setattr(DownloadManager, "get_opener", _guarded_get_opener)


@pytest.fixture(scope="session", autouse=True)
def isolate_test_environment(monkeypatch_session):
    from app.core.resilient_file_ops import resilient_rmtree

    temp_dir = tempfile.mkdtemp(prefix="test_autosorter_appdir_")
    keys_dir = os.path.join(temp_dir, "keys")
    os.makedirs(keys_dir, mode=0o700, exist_ok=True)
    monkeypatch_session.setenv("AUTOSORTER_APP_DIR", temp_dir)
    monkeypatch_session.setenv("AUTOSORTER_KEYS_DIR", keys_dir)
    monkeypatch_session.setenv(
        "PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring"
    )

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
    keys_dir_raw = os.environ.get("AUTOSORTER_KEYS_DIR")
    keys_dir = Path(keys_dir_raw) if keys_dir_raw else None
    existing_keys = (
        set(keys_dir.iterdir()) if keys_dir is not None and keys_dir.is_dir() else set()
    )
    yield
    if keys_dir is not None and keys_dir.is_dir():
        for entry in list(keys_dir.iterdir()):
            if entry not in existing_keys:
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass


@pytest.fixture(scope="session")
def monkeypatch_session():
    from _pytest.monkeypatch import MonkeyPatch

    mpatch = MonkeyPatch()
    yield mpatch
    mpatch.undo()


@pytest.fixture
def test_history_env(tmp_path):
    """Consolidated test environment helper for history, database, and cache sharing one Store."""
    import os

    from app.core.db_worker import DBWorker
    from app.core.store import open_store

    base_dir = str(tmp_path / "test_base")
    os.makedirs(base_dir, exist_ok=True)

    db_worker = DBWorker()

    def _build():
        store = open_store(tmp_path / "store", legacy_session_root=tmp_path / "none")
        database = store.database(db_worker)
        cache_mgr = store.cache(db_worker)
        history_mgr = store.history(database, cache_mgr)
        return database, cache_mgr, history_mgr

    try:
        db, cache, history_manager = db_worker.execute_write(_build)
        yield base_dir, db, cache, history_manager, db_worker
    finally:
        db_worker.stop(stop_embeddings=False)


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


def _scrub_failure_text(text: str) -> str:
    """Scrub secret patterns from failure tracebacks before persisting or logging."""
    try:
        from app.core.text_utils import sanitize_secret_patterns

        return sanitize_secret_patterns(text, replacement="[REDACTED]")
    except Exception:
        return text


def _is_xdist_worker_config(config=None) -> bool:
    """Return True when running inside a pytest-xdist worker process."""
    if config is not None and hasattr(config, "workerinput"):
        return True
    return "PYTEST_XDIST_WORKER" in os.environ


def pytest_configure(config):
    """Truncate pytest_failures.txt once per session on the pytest controller."""
    if not _is_xdist_worker_config(config):
        try:
            Path("pytest_failures.txt").unlink(missing_ok=True)
        except OSError:
            pass


def pytest_runtest_logreport(report):
    """Record failed test tracebacks on the pytest controller and emit GitHub Actions error annotations in CI."""
    if "PYTEST_XDIST_WORKER" in os.environ:
        return
    if report.failed:
        import sys

        tb = _scrub_failure_text(str(report.longrepr))
        safe_nodeid = _scrub_failure_text(str(report.nodeid))
        message = f"Test Failed ({report.when}): {safe_nodeid}\n\n{tb}"

        # Write to pytest_failures.txt on the controller only
        try:
            with open("pytest_failures.txt", "a", encoding="utf-8") as f:
                f.write(f"=== [{report.when}] {safe_nodeid} ===\n{tb}\n\n")
        except Exception:
            pass

        # Emit GitHub Actions ::error:: annotations only when GITHUB_ACTIONS == "true"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            try:
                message_escaped = message.replace("\r", "").replace("\n", "%0A")
                sys.__stdout__.write(f"\n::error:: {message_escaped}\n")
                sys.__stdout__.flush()
            except Exception:
                pass
