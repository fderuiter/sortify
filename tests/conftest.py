import contextlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.core.db_conn import clear_connection_cache

# Clear pre-loaded standard database modules to prevent library collision
for mod_name in list(sys.modules.keys()):
    if mod_name in (
        "sqlcipher3",
        "_sqlite3",
        "sqlite3",
        "pysqlcipher3",
    ) or mod_name.startswith(("sqlcipher3.", "sqlite3.", "pysqlcipher3.")):
        sys.modules.pop(mod_name, None)

_REAL_OS_CHMOD = os.chmod
_REAL_OS_OPEN = os.open
_REAL_OS_FDOPEN = os.fdopen
_REAL_OS_REPLACE = os.replace
_REAL_OS_UNLINK = os.unlink


class FileBackedKeyring(KeyringBackend):
    """File-backed test keyring persisting entries under AUTOSORTER_KEYS_DIR."""

    priority = 1

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bootstrap_dir: Path | None = None

    def _resolve_store_path(self) -> Path:
        raw_dir = os.environ.get("SORTIFY_KEYS_DIR") or os.environ.get(
            "AUTOSORTER_KEYS_DIR"
        )
        if raw_dir:
            keys_dir = Path(raw_dir)
        else:
            if self._bootstrap_dir is None:
                self._bootstrap_dir = Path(
                    tempfile.mkdtemp(prefix="test_autosorter_keys_")
                )
            keys_dir = self._bootstrap_dir
        keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            _REAL_OS_CHMOD(keys_dir, 0o700)
        return keys_dir / ".keyring_store.json"

    def _read_store(self) -> dict[str, dict[str, str]]:
        store_path = self._resolve_store_path()
        if not store_path.exists():
            return {}
        try:
            fd = _REAL_OS_OPEN(str(store_path), os.O_RDONLY)
            with _REAL_OS_FDOPEN(fd, "rb") as handle:
                data = json.loads(handle.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_store(self, data: dict[str, dict[str, str]]) -> None:
        store_path = self._resolve_store_path()
        payload = json.dumps(data, sort_keys=True).encode("utf-8")
        tmp_path = store_path.with_name(f"{store_path.name}.{os.getpid()}.tmp")
        fd = _REAL_OS_OPEN(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with _REAL_OS_FDOPEN(fd, "wb") as handle:
                handle.write(payload)
            with contextlib.suppress(OSError):
                _REAL_OS_CHMOD(tmp_path, 0o600)
            _REAL_OS_REPLACE(tmp_path, store_path)
            with contextlib.suppress(OSError):
                _REAL_OS_CHMOD(store_path, 0o600)
        except BaseException:
            with contextlib.suppress(OSError):
                _REAL_OS_UNLINK(tmp_path)
            raise

    def get_password(self, service: str, username: str) -> str | None:
        with self._lock:
            return self._read_store().get(service, {}).get(username)

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._lock:
            store = self._read_store()
            store.setdefault(service, {})[username] = password
            self._write_store(store)

    def delete_password(self, service: str, username: str) -> None:
        with self._lock:
            store = self._read_store()
            if service in store and username in store[service]:
                del store[service][username]
                if not store[service]:
                    del store[service]
                self._write_store(store)

    def clear(self) -> None:
        with self._lock:
            store_path = self._resolve_store_path()
            with contextlib.suppress(OSError):
                _REAL_OS_UNLINK(store_path)


_file_keyring = FileBackedKeyring()
keyring.set_keyring(_file_keyring)


@pytest.fixture(scope="module", autouse=True)
def isolate_module_keys_dir(request, isolate_test_environment):
    """Scope AUTOSORTER_KEYS_DIR per test module under the session keys root so module fixtures stay isolated."""
    from _pytest.monkeypatch import MonkeyPatch

    base_keys_dir = Path(os.environ["AUTOSORTER_KEYS_DIR"])
    mod_slug = (
        request.node.nodeid.replace("/", "_").replace("::", "_").replace(".", "_")
    )
    mod_keys_dir = base_keys_dir / mod_slug
    mod_keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        _REAL_OS_CHMOD(mod_keys_dir, 0o700)

    mpatch = MonkeyPatch()
    mpatch.setenv("AUTOSORTER_KEYS_DIR", str(mod_keys_dir))
    try:
        yield mod_keys_dir
    finally:
        mpatch.undo()


@pytest.fixture(autouse=True)
def reset_memory_keyring():
    """Restore per-test keyring and fallback key files to module-fixture baseline after each test."""
    keys_dir_raw = os.environ.get("AUTOSORTER_KEYS_DIR")
    keys_dir = Path(keys_dir_raw) if keys_dir_raw else None
    before_store = _file_keyring._read_store()
    before_files = (
        {p.name for p in keys_dir.iterdir()}
        if keys_dir is not None and keys_dir.exists()
        else set()
    )
    yield
    if keys_dir is not None and keys_dir.exists():
        for entry in list(keys_dir.iterdir()):
            if entry.name not in before_files and entry.name != ".keyring_store.json":
                with contextlib.suppress(OSError):
                    if entry.is_file() or entry.is_symlink():
                        _REAL_OS_UNLINK(entry)
        if before_store:
            _file_keyring._write_store(before_store)
        else:
            _file_keyring.clear()
    else:
        _file_keyring.clear()
    if "app.core.crypto" in sys.modules:
        sys.modules["app.core.crypto"]._reset_keyring_probe_cache()


@pytest.fixture(autouse=True)
def sync_db_worker(request):
    """Ensure all database writes happen synchronously during tests to prevent race conditions on Windows.

    Mark a test ``real_db_worker`` to run it against the real asynchronous write queue.
    """
    from app.core.db_worker import DBWorker
    from app.core.db_worker import logger as db_worker_logger

    if request.node.get_closest_marker("real_db_worker"):
        yield
        return

    def sync_execute(self, func, *args, **kwargs):
        if threading.current_thread() is getattr(self, "thread", None):
            try:
                func(*args, **kwargs)
            except Exception as e:
                func_name = getattr(func, "__name__", str(func))
                db_worker_logger.error(
                    f"Task exception encountered in write operation '{func_name}': {e}",
                    exc_info=True,
                )
            return
        with self._lock:
            if self._stopped:
                return
        try:
            self.execute_write(func, *args, **kwargs)
        except Exception:
            # DBWorker._run already logged the task exception with exc_info=True
            return

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
    import shutil

    from app.config import AppSettings, get_app_dir
    from app.core.db_conn import clear_connection_cache
    from app.core.downloader import _VERIFICATION_CACHE, DownloadManager
    from app.core.network_guard import _thread_local
    from app.core.shared_registry import SharedModelRegistry, SharedWorkerPool

    def _cleanup_test_app_dir():
        if DownloadManager._instance is not None:
            try:
                DownloadManager._instance.cancel_download()
                thread = DownloadManager._instance.current_thread
                if thread is not None and thread.is_alive():
                    thread.join(timeout=1.0)
            except Exception:
                pass
        _VERIFICATION_CACHE.clear()
        # Some older UI tests do not close their AppSession. Drain managed
        # services before deleting their stores, just as other singletons reset.
        from app.core import services

        for graph in list(services._services.values()):
            graph.worker.stop(stop_embeddings=False)
        services._services.clear()
        clear_connection_cache(only_current_and_inactive=False)
        try:
            app_dir = get_app_dir()
            for filename in (
                "settings.json",
                "sortify.db",
                "rollback_journal.json",
                "transaction_ledger.db",
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

    _cleanup_test_app_dir()
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
    _cleanup_test_app_dir()
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
    keys_dir = Path(temp_dir) / "keys"
    keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(keys_dir, 0o700)
    monkeypatch_session.setenv("AUTOSORTER_APP_DIR", temp_dir)
    monkeypatch_session.setenv("AUTOSORTER_KEYS_DIR", str(keys_dir))

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


_failure_log_path: Path = Path("pytest_failures.txt")
_recorded_failure_keys: set[tuple[str, str]] = set()


def _is_xdist_worker(config=None) -> bool:
    if "PYTEST_XDIST_WORKER" in os.environ:
        return True
    return config is not None and hasattr(config, "workerinput")


def pytest_sessionstart(session):
    """Truncate stale pytest_failures.txt on the controller before tests execute."""
    global _failure_log_path
    if _is_xdist_worker(session.config):
        return
    root_dir = (
        getattr(session.config, "rootpath", None)
        or Path(__file__).resolve().parent.parent
    )
    _failure_log_path = Path(root_dir) / "pytest_failures.txt"
    _recorded_failure_keys.clear()
    with contextlib.suppress(OSError):
        _failure_log_path.unlink(missing_ok=True)


def pytest_runtest_logreport(report):
    """Record deduplicated failure tracebacks on the controller and emit GitHub Actions annotations in CI."""
    if not report.failed or _is_xdist_worker():
        return

    failure_key = (report.nodeid, report.when)
    if failure_key in _recorded_failure_keys:
        return
    _recorded_failure_keys.add(failure_key)

    tb = str(report.longrepr)
    message = f"Test Failed ({report.when}): {report.nodeid}\n\n{tb}"

    try:
        with open(_failure_log_path, "a", encoding="utf-8") as f:
            f.write(f"=== [{report.when}] {report.nodeid} ===\n{tb}\n\n")
    except Exception:
        pass

    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            message_escaped = message.replace("\r", "").replace("\n", "%0A")
            sys.__stdout__.write(f"\n::error:: {message_escaped}\n")
            sys.__stdout__.flush()
        except Exception:
            pass
