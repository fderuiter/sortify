import hashlib
import os
import shutil
import tempfile
import threading
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import (
    DiskSpaceError,
    NetworkError,
    run_background_download,
    verify_downloaded_model,
)
from app.core.shared_registry import SharedModelRegistry
from app.core.user_space_bootstrap import check_internet_connection


@pytest.fixture
def temp_model_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


def test_check_internet_connection_retries_and_succeeds():
    """Verify check_internet_connection retries on failure and returns True when it eventually succeeds."""
    call_count = 0

    def mock_urlopen(url, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception("Temporary connection drop")
        return MagicMock()

    with (
        patch("urllib.request.urlopen", side_effect=mock_urlopen),
        patch("time.sleep") as mock_sleep,
    ):
        result = check_internet_connection(max_attempts=5, base_delay=0.01)
        assert result is True
        assert call_count == 3
        assert mock_sleep.call_count == 2


def test_check_internet_connection_exhausts_retries():
    """Verify check_internet_connection returns False after exhausting all attempts."""
    with (
        patch("urllib.request.urlopen", side_effect=Exception("Permanent failure")),
        patch("time.sleep") as mock_sleep,
    ):
        result = check_internet_connection(max_attempts=5, base_delay=0.01)
        assert result is False
        assert mock_sleep.call_count == 4


def test_downloader_retries_on_network_drop_and_succeeds(temp_model_dir):
    """Verify that downloader retries on network error during chunk read and succeeds."""
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_errors = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_errors.append(err)
        failure_called.set()

    # Create expected hash
    mock_data = b"completed_model_content_data"
    mock_hash = hashlib.sha256(mock_data).hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", {"model.onnx": mock_hash}
    )

    open_count = 0

    def mock_open(req, timeout=15):
        nonlocal open_count
        open_count += 1
        response = MagicMock()
        response.__enter__.return_value = response
        response.info.return_value.get.return_value = str(len(mock_data))

        if open_count == 1:
            # First attempt fails instantly on opener.open
            raise Exception("Initial Connection Timeout")
        elif open_count == 2:
            # Second attempt fails mid-stream
            response.read.side_effect = [
                b"completed_",
                Exception("Mid-stream Network Drop"),
            ]
        else:
            # Third attempt succeeds
            response.read.side_effect = [mock_data, b""]

        return response

    mock_opener = MagicMock()
    mock_opener.open.side_effect = mock_open

    notification_queue = Queue()

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(100000, 50000, 50000)),
        patch("time.sleep") as mock_sleep,
    ):
        run_background_download(
            url="http://example.com/model.onnx",
            model_dir=temp_model_dir,
            on_success=on_success,
            on_failure=on_failure,
            notification_queue=notification_queue,
            base_delay=0.01,
        )

        assert success_called.wait(timeout=5)
        assert verify_downloaded_model(temp_model_dir)
        assert open_count == 3
        assert mock_sleep.call_count == 2

        # Assert notifications were placed in the queue
        notifications = []
        while not notification_queue.empty():
            notifications.append(notification_queue.get_nowait())

        assert len(notifications) == 2
        assert "attempt 1/5" in notifications[0]
        assert "attempt 2/5" in notifications[1]


def test_downloader_exhausts_retries_and_cleans_up(temp_model_dir):
    """Verify that downloader stops after 5 failed attempts and deletes the partial tmp file."""
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_errors = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_errors.append(err)
        failure_called.set()

    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.info.return_value.get.return_value = "100"
    mock_response.read.side_effect = Exception("Persistent Stream Drop")

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_response

    notification_queue = Queue()

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(100000, 50000, 50000)),
        patch("time.sleep") as mock_sleep,
    ):
        run_background_download(
            url="http://example.com/model.onnx",
            model_dir=temp_model_dir,
            on_success=on_success,
            on_failure=on_failure,
            notification_queue=notification_queue,
            base_delay=0.001,
        )

        assert failure_called.wait(timeout=5)
        assert len(captured_errors) == 1
        assert isinstance(captured_errors[0], NetworkError)
        assert mock_sleep.call_count == 4

        # Verify that temp file model.onnx.tmp was cleaned up and does not exist
        temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
        assert not os.path.exists(temp_path)

        # 4 retries (for attempts 1, 2, 3, 4) enqueued warning messages
        notifications = []
        while not notification_queue.empty():
            notifications.append(notification_queue.get_nowait())
        assert len(notifications) == 4


def test_downloader_no_retry_on_cancel_or_disk_space(temp_model_dir):
    """Verify that downloader does not retry if cancelled or if disk space is insufficient."""
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_errors = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_errors.append(err)
        failure_called.set()

    # Simulating disk space error
    mock_response = MagicMock()
    mock_response.__enter__.return_value = mock_response
    mock_response.info.return_value.get.return_value = "100000"
    mock_response.read.return_value = b""

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_response

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        # 10 bytes free -> triggers DiskSpaceError immediately
        patch("shutil.disk_usage", return_value=(100000, 10, 10)),
        patch("time.sleep") as mock_sleep,
    ):
        run_background_download(
            url="http://example.com/model.onnx",
            model_dir=temp_model_dir,
            on_success=on_success,
            on_failure=on_failure,
            base_delay=0.01,
        )

        assert failure_called.wait(timeout=5)
        assert len(captured_errors) == 1
        assert isinstance(captured_errors[0], DiskSpaceError)
        # Verify no retries occurred
        assert mock_sleep.call_count == 0
