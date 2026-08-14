import hashlib
import os
import shutil
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import (
    DiskSpaceError,
    DownloadCancelledError,
    ModelVerificationError,
    run_background_download,
    verify_downloaded_model,
    verify_temp_file_hash,
)
from app.core.shared_registry import SharedModelRegistry


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

        # Compute and register mock hash for the expected download to pass verification
        mock_data = b"chunk1chunk2"
        mock_hash = hashlib.sha256(mock_data).hexdigest()
        SharedModelRegistry.get_instance().register_expected_hashes(
            "model_download", {"model.onnx": mock_hash}
        )

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


def test_verify_temp_file_hash_success(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
    target_path = os.path.join(temp_model_dir, "model.onnx")

    # Write some dummy model content
    dummy_content = b"valid onnx content data stream"
    with open(temp_path, "wb") as f:
        f.write(dummy_content)

    # Calculate mock expected hash and register it
    expected_hash = hashlib.sha256(dummy_content).hexdigest()
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("model_download", {"model.onnx": expected_hash})

    # Call verification -> should succeed
    assert verify_temp_file_hash(temp_path, target_path) is True
    # The temp file should still exist since it passed
    assert os.path.exists(temp_path)


def test_verify_temp_file_hash_mismatch_raises_error(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
    target_path = os.path.join(temp_model_dir, "model.onnx")

    # Write some dummy model content
    dummy_content = b"corrupted content data stream"
    with open(temp_path, "wb") as f:
        f.write(dummy_content)

    # Register an incorrect hash in registry
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "model_download", {"model.onnx": "incorrect_hash_value"}
    )

    # Call verification -> should raise ModelVerificationError
    with pytest.raises(
        ModelVerificationError, match="Cryptographic signature verification failed"
    ):
        verify_temp_file_hash(temp_path, target_path)

    # Temp file must be immediately deleted/discarded from disk
    assert not os.path.exists(temp_path)


def test_verify_temp_file_hash_no_hash_registered(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
    target_path = os.path.join(temp_model_dir, "model.onnx")

    with open(temp_path, "wb") as f:
        f.write(b"some model content")

    # Reset registry or clear expected hashes for model_download / generative_naming
    registry = SharedModelRegistry.get_instance()
    if "model_download" in registry._expected_hashes:
        del registry._expected_hashes["model_download"]
    if "generative_naming" in registry._expected_hashes:
        # Save old one to restore later
        old_hashes = registry._expected_hashes["generative_naming"]
        del registry._expected_hashes["generative_naming"]
    else:
        old_hashes = None

    try:
        with pytest.raises(
            ModelVerificationError, match="No cryptographic hash registered"
        ):
            verify_temp_file_hash(temp_path, target_path)
    finally:
        # Restore old hashes if deleted
        if old_hashes:
            registry.register_expected_hashes("generative_naming", old_hashes)


def test_downloader_fails_on_hash_mismatch(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    # Register an incorrect hash in registry to force mismatch
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "model_download", {"model.onnx": "some_mismatched_hash"}
    )

    # We mock urllib.request.build_opener and open to return a mock response
    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "100"
    mock_response.read.side_effect = [b"data1", b"data2", b""]

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

        assert failure_called.wait(timeout=5)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], ModelVerificationError)
        assert "Cryptographic signature verification failed" in str(captured_error[0])
        # Assert temp file is gone
        temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
        assert not os.path.exists(temp_path)


def test_downloader_aborts_on_placeholder_proxy(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    from app.core.downloader import NetworkError
    
    run_background_download(
        "http://example.com/model.onnx",
        temp_model_dir,
        proxy="<DECRYPTION_FAILED>",
        progress_callback=None,
        on_success=on_success,
        on_failure=on_failure,
    )

    assert failure_called.wait(timeout=5)
    assert len(captured_error) == 1
    assert isinstance(captured_error[0], NetworkError)
    assert "Invalid proxy configuration" in str(captured_error[0])

