from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.integration]


def test_main_cli_directory_argument():
    """Verify that launching main() with a directory argument executes run_tui with that directory."""
    from app.main import main

    mock_args = MagicMock()
    mock_args.tui = True
    mock_args.daemon = False
    mock_args.demo = False
    mock_args.directory = "/some/test/directory"

    with (
        patch("app.main.argparse.ArgumentParser.parse_args", return_value=mock_args),
        patch("app.ui.tui.run_tui") as mock_run_tui,
        patch("app.main.AppSettings") as mock_settings_class,
    ):
        main()

        mock_run_tui.assert_called_once_with(
            mock_settings_class.return_value, "/some/test/directory"
        )
