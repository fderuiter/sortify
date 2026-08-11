import os
import shutil
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import (
    DiskSpaceError,
    DownloadCancelledError,
    NetworkError,
    run_background_download,
    verify_downloaded_model,
)


@pytest.fixture
def temp_model_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


def test_verify_downloaded_model(temp_model_dir):
    # Setup files
    onnx_file = os.path.join(temp_model_dir, "model.onnx")
    config_file = os.path.join(temp_model_dir, "config.json")

    # Empty dir -> False
    assert not verify_downloaded_model(temp_model_dir)

    # Missing config -> False
    with open(onnx_file, "w") as f:
        f.write("dummy onnx content")
    assert not verify_downloaded_model(temp_model_dir)

    # Empty config -> False
    with open(config_file, "w") as f:
        f.write("")
    assert not verify_downloaded_model(temp_model_dir)

    # Valid config and non-empty ONNX -> True
    with open(config_file, "w") as f:
        f.write('{"model_type": "onnx"}')
    assert verify_downloaded_model(temp_model_dir)


def test_downloader_sandboxing_bypass(temp_model_dir):
    # Set thread local sandboxed to True globally to simulate sandbox mode
    from app.core.shared_registry import _thread_local
    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    _thread_local.sandboxed = True

    try:
        success_called = threading.Event()
        failure_called = threading.Event()
        captured_error = []

        def on_success():
            success_called.set()

        def on_failure(err):
            captured_error.append(err)
            failure_called.set()

        # We mock urllib.request.build_opener and open to return a mock response
        mock_response = MagicMock()
        mock_response.info.return_value.get.return_value = "100"
        mock_response.read.side_effect = [b"chunk1", b"chunk2", b""]

        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_response

        with (
            patch("urllib.request.build_opener", return_value=mock_opener),
            patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
        ):
            run_background_download(
                "http://example.com/model.onnx",
                temp_model_dir,
                progress_callback=None,
                on_success=on_success,
                on_failure=on_failure,
            )

            assert success_called.wait(timeout=5)
            assert verify_downloaded_model(temp_model_dir)
    finally:
        _thread_local.sandboxed = was_sandboxed


def test_downloader_cancellation(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    cancel_event = threading.Event()

    # Mock response to stream forever so we can cancel it
    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "100"
    mock_response.read.side_effect = lambda size: b"data"

    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_response

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
    ):
        run_background_download(
            "http://example.com/model.onnx",
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            cancel_event=cancel_event,
        )

        # Trigger cancellation immediately
        cancel_event.set()

        assert failure_called.wait(timeout=5)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], DownloadCancelledError)


def test_downloader_insufficient_disk_space(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "100000000"  # 100MB
    mock_response.read.return_value = b""

    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_response

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        # Return 10MB free space
        patch("shutil.disk_usage", return_value=(10000000, 1000000, 1000000)),
    ):
        run_background_download(
            "http://example.com/model.onnx",
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
        )

        assert failure_called.wait(timeout=5)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], DiskSpaceError)
