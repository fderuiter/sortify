import contextlib
from unittest.mock import patch

import pytest

from app.ui.help_modal import show_help


@pytest.fixture
def mock_nicegui():
    with contextlib.ExitStack() as stack:
        mock_dialog = stack.enter_context(patch("nicegui.ui.dialog"))
        mock_card = stack.enter_context(patch("nicegui.ui.card"))
        mock_label = stack.enter_context(patch("nicegui.ui.label"))
        mock_row = stack.enter_context(patch("nicegui.ui.row"))
        mock_button = stack.enter_context(patch("nicegui.ui.button"))
        mock_scroll_area = stack.enter_context(patch("nicegui.ui.scroll_area"))
        mock_markdown = stack.enter_context(patch("nicegui.ui.markdown"))

        yield {
            "dialog": mock_dialog,
            "card": mock_card,
            "label": mock_label,
            "row": mock_row,
            "button": mock_button,
            "scroll_area": mock_scroll_area,
            "markdown": mock_markdown,
        }


def test_show_help_dev_mode_success(mock_nicegui):
    """Test show_help when running in development mode and the user guide exists."""
    mock_content = "# Dev User Guide\nSome instructions."

    with (
        patch("app.ui.help_modal.is_packaged", return_value=False),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_content),
    ):
        show_help()

        # Verify dialog was opened
        mock_nicegui["dialog"].assert_called_once()
        # Verify markdown content was rendered
        mock_nicegui["markdown"].assert_called_once_with(mock_content)


def test_show_help_packaged_mode_success(mock_nicegui):
    """Test show_help when running in packaged mode and the user guide exists."""
    mock_content = "# Packaged User Guide\nSome instructions."

    # Setup mock for sys._MEIPASS
    with (
        patch("app.ui.help_modal.is_packaged", return_value=True),
        patch("sys._MEIPASS", "/mock/meipass", create=True),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_content),
    ):
        show_help()

        # Verify read path corresponds to sys._MEIPASS
        mock_nicegui["dialog"].assert_called_once()
        mock_nicegui["markdown"].assert_called_once_with(mock_content)


def test_show_help_file_missing_error(mock_nicegui):
    """Test show_help handles file missing error gracefully."""
    with (
        patch("app.ui.help_modal.is_packaged", return_value=False),
        patch("pathlib.Path.exists", return_value=False),
    ):
        show_help()

        # Verify dialog opens but shows error markdown
        mock_nicegui["dialog"].assert_called_once()
        mock_nicegui["markdown"].assert_called_once()
        called_arg = mock_nicegui["markdown"].call_args[0][0]
        assert "Error: User guide not found" in called_arg
