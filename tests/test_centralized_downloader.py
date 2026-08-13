import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import (
    DownloadError,
    DownloadManager,
    ThreadSafeState,
)
from app.ui.wizard import show_wizard


def test_download_manager_singleton():
    """Verify that DownloadManager is a singleton."""
    dm1 = DownloadManager.get_instance()
    dm2 = DownloadManager.get_instance()
    assert dm1 is dm2


def test_download_manager_single_download_rejection():
    """Verify that multiple simultaneous downloads are rejected."""
    dm = DownloadManager.get_instance()

    # Reset state
    dm.state["is_downloading"] = False
    dm.state["error"] = None
    dm.state["success"] = False

    # Simulate active download
    dm.state["is_downloading"] = True

    with pytest.raises(DownloadError, match="An installation is already underway"):
        dm.start_download(url="http://dummy", model_dir="/dummy/dir")

    # Clean up
    dm.state["is_downloading"] = False


def test_download_manager_unified_state():
    """Verify that the state object of DownloadManager is thread-safe and unified."""
    dm = DownloadManager.get_instance()
    assert isinstance(dm.state, ThreadSafeState)

    dm.state["progress"] = 0.5
    assert dm.state["progress"] == 0.5


def test_asynchronous_model_deletion():
    """Verify that delete_model_async cancels active downloads and deletes the model directory asynchronously."""
    dm = DownloadManager.get_instance()
    dm.state["is_downloading"] = True
    dm.state["success"] = True

    temp_dir = tempfile.mkdtemp()
    dummy_file = os.path.join(temp_dir, "dummy_model.onnx")
    with open(dummy_file, "w") as f:
        f.write("dummy model content")

    assert os.path.exists(temp_dir)

    done_event = threading.Event()
    callback_results = []

    def on_done(success, err):
        callback_results.append((success, err))
        done_event.set()

    dm.delete_model_async(temp_dir, on_done=on_done)

    # Wait for async deletion to complete
    assert done_event.wait(timeout=5)
    assert callback_results == [(True, None)]
    assert not os.path.exists(temp_dir)
    assert dm.state["is_downloading"] is False
    assert dm.state["progress"] == 0.0
    assert dm.state["success"] is False
    assert dm.state["status_text"] == "Model deleted."


@patch("nicegui.ui.timer")
@patch("nicegui.ui.dialog")
def test_dismiss_does_not_abort_download(mock_dialog, mock_timer):
    """Verify that dismissing/closing the wizard dialog does NOT cancel the active download."""
    # Reset/ensure DownloadManager state
    dm = DownloadManager.get_instance()
    dm.state["is_downloading"] = True
    dm.cancel_event.clear()

    # Mock dialog setup to extract the "dismiss" handler
    dialog_instance = MagicMock()
    mock_dialog.return_value.__enter__.return_value = dialog_instance

    parent_app = MagicMock()
    settings = MagicMock()
    settings.PROXY = ""

    from nicegui import Client

    with Client(None):
        show_wizard(parent_app, settings)

    # Locate the dismiss event callback
    dismiss_handler = None
    for call in dialog_instance.on.call_args_list:
        if call[0][0] == "dismiss":
            dismiss_handler = call[0][1]
            break

    assert dismiss_handler is not None, (
        "Dismiss handler not registered on wizard dialog"
    )

    # Call the dismiss handler
    dismiss_handler()

    # The active background download must NOT be aborted or cancelled
    assert not dm.cancel_event.is_set(), (
        "Dismissing wizard aborted the active download!"
    )

    # Clean up
    dm.state["is_downloading"] = False
