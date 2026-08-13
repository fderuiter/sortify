import hashlib
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import run_background_download
from app.core.shared_registry import (
    ContextPropagatingThread,
    ContextPropagatingThreadPoolExecutor,
    SharedModelRegistry,
    SharedWorkerPool,
    _thread_local,
    block_external_network,
)


def test_extraction_tasks_inherit_sandbox_state(socket_mock):
    """Verify that background text extraction tasks fail to make external connections when sandboxing is active."""
    mock_connect, _ = socket_mock
    pool = SharedWorkerPool.get_instance()

    def task_trying_to_connect():
        s = socket.socket()
        try:
            s.connect(("8.8.8.8", 80))
        finally:
            s.close()

    # When initiator is sandboxed, the task must execute under sandbox constraints
    with block_external_network(reason="active extraction session"):
        future = pool.submit(task_trying_to_connect)
        with pytest.raises(
            PermissionError, match="External network connections are blocked"
        ):
            future.result()

    mock_connect.assert_not_called()


def test_database_decryption_pools_inherit_sandbox(socket_mock):
    """Verify that database decryption worker pools block outgoing socket traffic without manual wrap statements."""
    mock_connect, _ = socket_mock

    def decrypt_task():
        s = socket.socket()
        try:
            s.connect(("1.1.1.1", 80))
        finally:
            s.close()

    # When initiator has active sandbox, the pool must inherit it without manual wrapping
    with block_external_network(reason="secure cache decryption"):
        with ContextPropagatingThreadPoolExecutor() as executor:
            future = executor.submit(decrypt_task)
            with pytest.raises(
                PermissionError, match="External network connections are blocked"
            ):
                future.result()

    mock_connect.assert_not_called()


def test_vector_reconstruction_automatically_sandboxed():
    """Verify that vector reconstruction threads run in an entirely offline sandbox state automatically."""
    status_captured = []

    def reconstruction_target():
        # Read the sandbox state inside the thread execution
        sandboxed = getattr(_thread_local, "sandboxed", False)
        reason = getattr(_thread_local, "reason", "")
        status_captured.append((sandboxed, reason))

    # A ContextPropagatingThread initialized as VectorReconstructionThread should enforce offline boundaries automatically
    thread = ContextPropagatingThread(
        target=reconstruction_target,
        name="VectorReconstructionThread",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    assert len(status_captured) == 1
    assert status_captured[0][0] is True
    assert status_captured[0][1] == "background vector reconstruction"


def test_model_downloader_bypasses_sandbox(tmp_path):
    """Verify that the model downloader is still able to connect to external sites even when the parent thread is sandboxed."""
    success_called = threading.Event()
    failure_called = threading.Event()

    def on_success():
        success_called.set()

    def on_failure(err):
        failure_called.set()
        print(f"DOWNLOAD FAILURE: {err}")


    # Compute and register mock hash for the expected download to pass verification
    mock_data = b"modeldata"
    mock_hash = hashlib.sha256(mock_data).hexdigest()
    # Store old hashes to restore them later
    registry = SharedModelRegistry.get_instance()
    original_expected_hashes = dict(registry._expected_hashes)
    registry.register_expected_hashes("model_download", {"model.onnx": mock_hash})

    mock_opener = MagicMock()
    # Mock response to simulate model downloading successfully
    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "10"
    mock_response.read.side_effect = [mock_data, b""]
    mock_opener.open.return_value.__enter__.return_value = mock_response
    from app.core.hashes_registry import HASHES

    original_hashes_model_download = HASHES["model_download"]["model.onnx"]
    original_hashes_generative_naming = HASHES["generative_naming"]["model.onnx"]

    HASHES["model_download"]["model.onnx"] = mock_hash
    HASHES["generative_naming"]["model.onnx"] = mock_hash
    try:
        # Start with active sandbox on initiator thread
        with block_external_network(reason="enterprise restriction"):
            with (
                patch("urllib.request.build_opener", return_value=mock_opener),
                patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
                patch("app.core.downloader.verify_temp_file_hash", return_value=True),
            ):
                thread = run_background_download(
                    "http://example.com/external-model.onnx",
                    str(tmp_path / "model_dir"),
                    on_success=on_success,
                    on_failure=on_failure,
                )
                thread.join(timeout=5.0)

        # The download should have run successfully because the downloader thread clears the sandbox state
        assert success_called.is_set() or success_called.wait(timeout=1)
    finally:
        registry._expected_hashes = original_expected_hashes
        HASHES["model_download"]["model.onnx"] = original_hashes_model_download
        HASHES["generative_naming"]["model.onnx"] = original_hashes_generative_naming


def test_localhost_connections_succeed_under_sandbox():
    """Verify that localhost socket connections succeed when the network sandbox is active."""
    with block_external_network(reason="test sandbox active"):
        # Local loopback addresses must bypass sandboxing restrictions
        s1 = socket.socket()
        try:
            # We mock the connect/connect_ex call because there may not be an actual listener,
            # but we assert it doesn't raise a PermissionError!
            with patch("socket.socket.connect") as mock_connect:
                s1.connect(("127.0.0.1", 8080))
                mock_connect.assert_called_once_with(("127.0.0.1", 8080))
        finally:
            s1.close()

        s2 = socket.socket()
        try:
            with patch("socket.socket.connect") as mock_connect:
                s2.connect(("localhost", 5432))
                mock_connect.assert_called_once_with(("localhost", 5432))
        finally:
            s2.close()
