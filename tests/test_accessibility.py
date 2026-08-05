from unittest import mock

from app.ui.dialog_helper import make_dialog_accessible
from app.ui.settings import show_settings


def test_make_dialog_accessible():
    """Test that make_dialog_accessible registers event listeners on NiceGUI Dialog."""
    mock_dialog = mock.MagicMock()

    make_dialog_accessible(mock_dialog, "test-card-class")

    # Verify that 'custom_escape' listener was registered
    mock_dialog.on.assert_called_with("custom_escape", mock_dialog.close)
    # Verify that value change listener was registered
    mock_dialog.on_value_change.assert_called_once()


def test_show_settings_accessible_dialog():
    """Test that show_settings creates the dialog and applies make_dialog_accessible."""
    mock_parent = mock.MagicMock()
    mock_settings = mock.MagicMock()
    mock_settings.EXPLORER_INTEGRATION = True
    mock_settings.CLEANUP_EMPTY_FOLDERS = False
    mock_settings.MAX_DEPTH = 3
    mock_settings.MAX_FOLDERS = 10
    mock_settings.KEYWORD_RULES = {"invoice": "Invoices"}

    mock_dialog_instance = mock.MagicMock()
    mock_dialog_context = mock.MagicMock()
    mock_dialog_context.__enter__.return_value = mock_dialog_instance
    mock_dialog_context.__exit__.return_value = None

    with (
        mock.patch("app.core.verifier.check_ai_status", return_value=(True, None)),
        mock.patch("app.core.integration.register_context_menu"),
        mock.patch("nicegui.ui.dialog", return_value=mock_dialog_context),
        mock.patch(
            "app.ui.dialog_helper.make_dialog_accessible"
        ) as mock_make_accessible,
    ):
        show_settings(mock_parent, mock_settings)

        # Verify make_dialog_accessible was called on the dialog with settings-dialog-card class
        mock_make_accessible.assert_called_once_with(
            mock_dialog_instance, "settings-dialog-card"
        )
        assert mock_dialog_instance.open.called
