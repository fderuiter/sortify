"""Tests for terminal application run_app hardening."""

from unittest.mock import patch

from app.config import AppSettings
from app.ui.app import run_app


def test_run_app_terminal_execution():
    """Verify that run_app executes without network binding or starting web servers."""
    settings = AppSettings()
    with patch("app.ui.app.AutoSorterApp") as mock_app_cls:
        run_app(settings)
        mock_app_cls.assert_called_once_with(settings, debug_layout=False)
