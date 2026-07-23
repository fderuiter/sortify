from unittest.mock import patch

# Ensure dummy settings are injected similar to other UI snapshot tests
from app.config import AppSettings
from app.ui.app import run_app


def test_run_app_hardening():
    """Verify that run_app binds exclusively to 127.0.0.1 (local-only hardening)."""
    with (
        patch("app.ui.app.ui") as mock_ui,
        patch("app.ui.app.AutoSorterApp"),
    ):
        settings = AppSettings()
        run_app(settings)

        # Verify ui.run was called once
        mock_ui.run.assert_called_once()

        # Check arguments
        kwargs = mock_ui.run.call_args[1]
        assert kwargs.get("host") == "127.0.0.1"
        assert kwargs.get("port") == 8080
        assert kwargs.get("reload") is False
        assert kwargs.get("show") is True
