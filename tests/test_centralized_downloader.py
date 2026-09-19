import os
import tempfile
import threading

import pytest

from app.core.downloader import (
    DownloadError,
    DownloadManager,
    ThreadSafeState,
)


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
