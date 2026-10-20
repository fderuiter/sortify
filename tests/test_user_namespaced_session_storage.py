"""Unit tests for user-namespaced session storage path resolution, sanitization, and session recovery isolation."""

import json
import os
from unittest import mock

import pytest

from app.core.path_utils import (
    get_sanitized_username,
    get_session_base_dir,
    setup_session_directory,
)
from app.core.session_catalog import find_abandoned_sessions
from app.core.store import open_store


def test_get_sanitized_username_standard():
    """Verify standard OS usernames pass through intact."""
    with mock.patch("getpass.getuser", return_value="jules"):
        assert get_sanitized_username() == "jules"

    with mock.patch("getpass.getuser", return_value="alice_doe"):
        assert get_sanitized_username() == "alice_doe"


def test_get_sanitized_username_domain_and_special_chars():
    """Verify domain prefixes, slashes, colons, and illegal path chars are sanitized."""
    with mock.patch("getpass.getuser", return_value=r"DOMAIN\john.doe"):
        assert get_sanitized_username() == "DOMAIN_john.doe"

    with mock.patch("getpass.getuser", return_value="user@host:8080"):
        assert get_sanitized_username() == "user_host_8080"

    with mock.patch("getpass.getuser", return_value="user<name>?*"):
        assert get_sanitized_username() == "user_name"


def test_get_sanitized_username_fallback_on_exception(monkeypatch):
    """Verify fallback to environment variables when getpass.getuser() raises an exception."""
    with mock.patch("getpass.getuser", side_effect=OSError("No login name")):
        monkeypatch.setenv("USER", "env_user")
        assert get_sanitized_username() == "env_user"


def test_get_sanitized_username_fallback_when_empty(monkeypatch):
    """Verify fallback to unknown_user when no username can be resolved."""
    with mock.patch("getpass.getuser", return_value=""):
        monkeypatch.delenv("USER", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        monkeypatch.delenv("LOGNAME", raising=False)
        assert get_sanitized_username() == "unknown_user"


def test_get_sanitized_username_reserved_names():
    """Verify Windows reserved filenames default to unknown_user."""
    for reserved in ["CON", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
        with mock.patch("getpass.getuser", return_value=reserved):
            assert get_sanitized_username() == "unknown_user"


def test_get_session_base_dir_creates_user_namespaced_directory(monkeypatch, tmp_path):
    """Verify get_session_base_dir constructs a path containing the sanitized username in system temp."""
    monkeypatch.delenv("AUTOSORTER_APP_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    with mock.patch(
        "app.core.path_utils.get_sanitized_username", return_value="test_user"
    ):
        base_dir = get_session_base_dir()
        assert base_dir == tmp_path / "autosorter_sessions_test_user"
        assert base_dir.exists()
        assert base_dir.is_dir()


def test_setup_session_directory_isolated(monkeypatch, tmp_path):
    """Verify setup_session_directory creates the session subfolder inside the user-isolated session root."""
    monkeypatch.delenv("AUTOSORTER_APP_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    with mock.patch("app.core.path_utils.get_sanitized_username", return_value="alice"):
        session_id, session_dir = setup_session_directory("session_123")
        assert session_id == "session_123"
        assert session_dir == tmp_path / "autosorter_sessions_alice" / "session_123"
        assert session_dir.exists()


def test_multi_user_isolation_startup_no_crash(monkeypatch, tmp_path):
    """Verify secondary user can launch cleanly when another user's session folder exists in system temp."""
    monkeypatch.delenv("AUTOSORTER_APP_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    # User A creates their session base directory
    user_a_dir = tmp_path / "autosorter_sessions_usera"
    user_a_dir.mkdir(parents=True, exist_ok=True)
    (user_a_dir / "session_a").mkdir()
    (user_a_dir / "session_a" / "history.db").touch()

    # On POSIX systems, make User A's folder non-readable/non-writable by others if permission changes are supported
    if hasattr(os, "chmod"):
        try:
            os.chmod(user_a_dir, 0o700)
        except Exception:
            pass

    # Now User B launches application
    with mock.patch("app.core.path_utils.get_sanitized_username", return_value="userb"):
        session_id, session_dir = setup_session_directory()
        assert "autosorter_sessions_userb" in str(session_dir)
        assert session_dir.exists()

        # User B can write databases and artifacts into their isolated directory
        test_db = session_dir / "autosorter.db"
        test_db.write_text("dummy database content")
        assert test_db.exists()


@pytest.mark.anyio
async def test_session_recovery_scans_only_active_user_root(monkeypatch, tmp_path):
    """Verify find_abandoned_sessions restricts scanning strictly to the active user's session root."""
    monkeypatch.delenv("AUTOSORTER_APP_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    # Setup User A session directory with an abandoned session
    user_a_base = tmp_path / "autosorter_sessions_usera"
    user_a_session = user_a_base / "session_a1"
    user_a_session.mkdir(parents=True, exist_ok=True)
    journal_a = user_a_session / "rollback_journal.json"
    journal_a.write_text(
        json.dumps(
            {
                "session_id": "session_a1",
                "safety_session_id": "safety_a1",
                "base_dir": "/tmp/a",
            }
        )
    )

    # Setup User B session directory with an abandoned session
    user_b_base = tmp_path / "autosorter_sessions_userb"
    user_b_session = user_b_base / "session_b1"
    user_b_session.mkdir(parents=True, exist_ok=True)
    journal_b = user_b_session / "rollback_journal.json"
    journal_b.write_text(
        json.dumps(
            {
                "session_id": "session_b1",
                "safety_session_id": "safety_b1",
                "base_dir": "/tmp/b",
            }
        )
    )

    # Lock User A's directory if on POSIX to test permission resilience
    if hasattr(os, "chmod"):
        try:
            os.chmod(user_a_base, 0o000)
        except Exception:
            pass

    try:
        # Run session recovery scan as User B
        with mock.patch(
            "app.core.path_utils.get_sanitized_username", return_value="userb"
        ):
            abandoned = find_abandoned_sessions(open_store(tmp_path / "app"))

            # Should find ONLY User B's abandoned session, ignoring User A's
            assert len(abandoned) == 1
            assert abandoned[0]["session_id"] == "session_b1"
    finally:
        # Restore permissions for cleanup
        if hasattr(os, "chmod"):
            try:
                os.chmod(user_a_base, 0o700)
            except Exception:
                pass
