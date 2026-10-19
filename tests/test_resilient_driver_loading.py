"""Tests for loading the SQLCipher driver and degrading without it."""

import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core import db_conn
from app.core.db_conn import clear_connection_cache, get_db_connection

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not db_conn.HAS_SQLCIPHER, reason="SQLCipher driver not installed")
def test_installed_driver_encrypts_without_setup():
    """The sqlcipher3 wheel works as installed, with no path or DLL setup."""
    with closing(db_conn.sqlite3.connect(":memory:")) as conn:
        conn.execute("PRAGMA key = 'test-key'")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('secret')")
        assert conn.execute("SELECT v FROM t").fetchone()[0] == "secret"
        assert conn.execute("PRAGMA cipher_version").fetchone()[0]


def test_importing_db_conn_does_not_touch_the_network():
    """Importing db_conn opens no sockets and changes no library search paths."""
    code = (
        "import os, socket\n"
        "def _no_network(*a, **k):\n"
        "    raise AssertionError('network access during import')\n"
        "socket.socket.connect = _no_network\n"
        "socket.create_connection = _no_network\n"
        "before = {k: os.environ.get(k) for k in ('PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH')}\n"
        "import app.core.db_conn\n"
        "after = {k: os.environ.get(k) for k in before}\n"
        "assert before == after, (before, after)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_get_db_connection_graceful_fallback_cross_platform(tmp_path, caplog):
    """Verify get_db_connection degrades gracefully with security warnings on Linux, macOS, and Windows when SQLCipher is unavailable or unverified."""
    test_db = tmp_path / "fallback_test.db"

    for platform_name in ("linux", "darwin", "win32"):
        caplog.clear()
        with patch("sys.platform", platform_name):
            with patch("app.core.db_conn.HAS_SQLCIPHER", False):
                with patch("app.core.db_conn._disable_pytest_win_fallback", False):
                    try:
                        conn = get_db_connection(str(test_db))
                        assert conn is not None
                        # Verify security warning logged
                        assert any(
                            "SECURITY WARNING" in record.message
                            for record in caplog.records
                        )

                        # Verify read/write functionality on fallback connection
                        with conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "CREATE TABLE IF NOT EXISTS test_tb (id INT)"
                            )
                            cursor.execute("INSERT INTO test_tb VALUES (100)")
                            cursor.execute("SELECT id FROM test_tb")
                            row = cursor.fetchone()
                            assert row[0] == 100
                    finally:
                        clear_connection_cache(only_current_and_inactive=False)
