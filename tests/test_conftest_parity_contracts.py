"""Regression tests for conftest fixture parity, cross-process FileBackedKeyring, and xdist failure logging."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import keyring
import pytest

from app.core.db import Database
from app.core.db_conn import clear_connection_cache
from app.core.db_worker import DBWorker


def test_sync_db_worker_catches_and_logs_task_exceptions_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sync_db_worker must catch and log task exceptions like DBWorker.execute_write_async."""
    worker = DBWorker()
    executed_after_error: list[str] = []

    def _boom() -> None:
        raise RuntimeError("boom inside async write")

    def _reentrant_boom() -> None:
        worker.execute_write_async(_boom)
        executed_after_error.append("reentrant-survived")

    try:
        with caplog.at_level(logging.ERROR, logger="app.core.db_worker"):
            worker.execute_write_async(_boom)
            worker.execute_write_async(_reentrant_boom)
            worker.execute_write_async(lambda: executed_after_error.append("next-task"))

        assert executed_after_error == ["reentrant-survived", "next-task"]
        error_messages = [
            rec.getMessage()
            for rec in caplog.records
            if "Task exception encountered in write operation" in rec.getMessage()
        ]
        assert len(error_messages) == 2
    finally:
        worker.stop()


@pytest.mark.real_db_worker
def test_real_db_worker_matches_sync_exception_contract(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Real background DBWorker thread must match sync_db_worker exception handling."""
    worker = DBWorker()
    done = threading.Event()
    executed_after_error: list[str] = []

    def _boom() -> None:
        raise RuntimeError("real worker task failure")

    def _after() -> None:
        executed_after_error.append("after-boom")
        done.set()

    try:
        with caplog.at_level(logging.ERROR, logger="app.core.db_worker"):
            worker.execute_write_async(_boom)
            worker.execute_write_async(_after)
            assert done.wait(timeout=5.0)

        assert executed_after_error == ["after-boom"]
        assert any(
            "Task exception encountered in write operation" in rec.getMessage()
            for rec in caplog.records
        )
    finally:
        worker.stop()


def test_file_backed_keyring_shares_sqlcipher_keys_with_child_processes(
    tmp_path: Path,
) -> None:
    """FileBackedKeyring under AUTOSORTER_KEYS_DIR must persist keys across subprocess boundaries."""
    keys_dir = Path(os.environ["AUTOSORTER_KEYS_DIR"])
    assert keys_dir.exists()

    backend = keyring.get_keyring()
    assert type(backend).__name__ == "FileBackedKeyring"

    db_file = tmp_path / "cross_proc_parity.db"
    worker = DBWorker()
    db = Database(db_file, worker)
    base_dir = str(tmp_path / "docs")
    try:
        db.upsert_document(base_dir, "report.txt", "hash123", "confidential payload")
    finally:
        worker.stop()
        clear_connection_cache()

    if os.name != "nt":
        dir_mode = stat.S_IMODE(keys_dir.stat().st_mode)
        assert dir_mode == 0o700
        store_file = keys_dir / ".keyring_store.json"
        assert store_file.exists()
        assert stat.S_IMODE(store_file.stat().st_mode) == 0o600

    repo_root = Path(__file__).resolve().parent.parent
    child_code = (
        "import sys; "
        "from app.core.db_conn import clear_connection_cache, get_db_connection; "
        "from app.core.path_utils import resolve_db_crypto; "
        "db_path = sys.argv[1]; "
        "conn = get_db_connection(db_path); "
        "row = conn.execute('SELECT extracted_text FROM documents WHERE filepath = ?', ('report.txt',)).fetchone(); "
        "crypto = resolve_db_crypto(db_path); "
        "print(crypto.decrypt_text(row[0])); "
        "clear_connection_cache()"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    proc = subprocess.run(
        [sys.executable, "-c", child_code, str(db_file)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "confidential payload"


def test_pytest_failures_controller_truncation_deduplication_and_gha_gating(
    tmp_path: Path,
) -> None:
    """Controller must truncate stale pytest_failures.txt, write each xdist failure once, and gate ::error:: on GITHUB_ACTIONS."""
    repo_root = Path(__file__).resolve().parent.parent
    mini_proj = tmp_path / "mini_pytest_proj"
    mini_proj.mkdir()

    (mini_proj / "pytest.ini").write_text("[pytest]\naddopts =\n", encoding="utf-8")
    (mini_proj / "conftest.py").write_text(
        f"import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        f"from tests.conftest import pytest_runtest_logreport, pytest_sessionstart\n",
        encoding="utf-8",
    )
    (mini_proj / "test_sample_failures.py").write_text(
        "def test_alpha_fail():\n"
        "    assert 1 == 2\n\n"
        "def test_beta_fail():\n"
        "    assert 'x' == 'y'\n",
        encoding="utf-8",
    )

    failures_file = mini_proj / "pytest_failures.txt"
    failures_file.write_text("STALE_PREVIOUS_RUN_FAILURE\n", encoding="utf-8")

    local_env = os.environ.copy()
    local_env.pop("GITHUB_ACTIONS", None)
    local_env.pop("PYTEST_XDIST_WORKER", None)

    res_local = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "2", "test_sample_failures.py"],
        cwd=mini_proj,
        env=local_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_local.returncode != 0
    assert "::error::" not in res_local.stdout

    logged = failures_file.read_text(encoding="utf-8")
    assert "STALE_PREVIOUS_RUN_FAILURE" not in logged
    assert logged.count("=== [call] test_sample_failures.py::test_alpha_fail ===") == 1
    assert logged.count("=== [call] test_sample_failures.py::test_beta_fail ===") == 1

    gha_env = dict(local_env)
    gha_env["GITHUB_ACTIONS"] = "true"
    res_gha = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "2", "test_sample_failures.py"],
        cwd=mini_proj,
        env=gha_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_gha.returncode != 0
    assert "::error:: Test Failed (call):" in res_gha.stdout
