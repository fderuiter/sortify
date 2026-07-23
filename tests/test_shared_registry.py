import hashlib
import socket
from unittest.mock import MagicMock, patch

import pytest

from app.core.shared_registry import (
    SharedModelRegistry,
    SharedWorkerPool,
)


def test_shared_model_registry_singleton():
    """Assert that get_instance always returns the same centralized model registry."""
    reg1 = SharedModelRegistry.get_instance()
    reg2 = SharedModelRegistry.get_instance()
    assert reg1 is reg2


def test_shared_model_registry_defer_loading():
    """Verify that model loading is deferred until explicitly requested."""
    # Reset registry instance for a clean test
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    # Ensure no model weights/pipelines are in _models initially
    assert "generative_naming" not in registry._models
    assert "easyocr" not in registry._models


@patch("transformers.AutoTokenizer.from_pretrained")
@patch("transformers.AutoModelForSeq2SeqLM.from_pretrained")
@patch("transformers.pipelines.pipeline")
@patch("torch.quantization.quantize_dynamic")
def test_shared_model_registry_integrity_check(
    mock_quantize, mock_pipeline, mock_model, mock_tokenizer, tmp_path
):
    """Test SHA-256 integrity checks on loaded models."""
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    model_dir = tmp_path / "dummy_model"
    model_dir.mkdir()
    config_file = model_dir / "config.json"
    config_content = b'{"model_type": "t5"}'
    config_file.write_bytes(config_content)

    # Compute expected SHA-256
    config_hash = hashlib.sha256(config_content).hexdigest()

    # Case 1: Register expected hash, matches actual -> should load successfully
    registry.register_expected_hashes("generative_naming", {"config.json": config_hash})

    mock_tokenizer.return_value = MagicMock()
    mock_model.return_value = MagicMock()
    mock_pipeline.return_value = MagicMock()

    gen, task, tok = registry.get_generative_model(str(model_dir))
    assert gen is not None
    # Verify we cached the model in registry
    assert "generative_naming" in registry._models

    # Case 2: Register expected hash, mismatch -> should raise ValueError and prevent execution
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "generative_naming", {"config.json": "wrong_hash"}
    )

    with pytest.raises(ValueError, match="Integrity check failed"):
        registry.get_generative_model(str(model_dir))


def test_shared_worker_pool_singleton():
    """Assert that get_instance always returns the same global worker pool."""
    SharedWorkerPool._instance = None
    pool1 = SharedWorkerPool.get_instance(max_workers=3)
    pool2 = SharedWorkerPool.get_instance(max_workers=5)
    assert pool1 is pool2
    assert pool1.max_workers == 3  # Initial creation max_workers respected


def test_shared_worker_pool_offline_enforcement():
    """Assert that tasks submitted to the global pool are blocked from external connections."""
    pool = SharedWorkerPool.get_instance()

    def task_trying_to_connect():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 53))

    future = pool.submit(task_trying_to_connect)
    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        future.result()

    def task_trying_to_connect_ex():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect_ex(("8.8.8.8", 53))

    future_ex = pool.submit(task_trying_to_connect_ex)
    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        future_ex.result()


def test_session_db_and_cache_isolation():
    """Assert routing tasks preserves session-specific database and settings references."""
    pool = SharedWorkerPool.get_instance()

    def process_task(db_instance, settings_instance):
        return db_instance.get_id(), settings_instance.get_val()

    mock_db_1 = MagicMock()
    mock_db_1.get_id.return_value = "session_1_db"
    mock_settings_1 = MagicMock()
    mock_settings_1.get_val.return_value = "session_1_settings"

    mock_db_2 = MagicMock()
    mock_db_2.get_id.return_value = "session_2_db"
    mock_settings_2 = MagicMock()
    mock_settings_2.get_val.return_value = "session_2_settings"

    fut1 = pool.submit(process_task, mock_db_1, mock_settings_1)
    fut2 = pool.submit(process_task, mock_db_2, mock_settings_2)

    assert fut1.result() == ("session_1_db", "session_1_settings")
    assert fut2.result() == ("session_2_db", "session_2_settings")


def test_socket_sandbox_blocking_of_external_and_allow_localhost():
    """Verify that socket sandboxing blocks external domains while allowing localhost/loopback."""
    from app.core.shared_registry import (
        apply_global_socket_sandbox,
        safe_connect,
        safe_connect_ex,
        block_external_network,
    )

    apply_global_socket_sandbox()

    # Create a mock socket
    mock_socket = MagicMock()

    # Try connecting to external domain
    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        with block_external_network():
            safe_connect(mock_socket, ("8.8.8.8", 80))

    with pytest.raises(
        PermissionError, match="External network connections are blocked"
    ):
        with block_external_network():
            safe_connect_ex(mock_socket, ("8.8.8.8", 80))

    # Try connecting to localhost
    with (
        patch("app.core.shared_registry._original_connect") as mock_connect,
        patch("app.core.shared_registry._original_connect_ex") as mock_connect_ex,
    ):
        with block_external_network():
            safe_connect(mock_socket, ("127.0.0.1", 8080))
        mock_connect.assert_called_once_with(mock_socket, ("127.0.0.1", 8080))

        with block_external_network():
            safe_connect_ex(mock_socket, ("localhost", 8080))
        mock_connect_ex.assert_called_once_with(mock_socket, ("localhost", 8080))


def test_socket_sandbox_inactive_allows_external_connections():
    """Verify that when block_external_network is not active, external connections are allowed."""
    from app.core.shared_registry import safe_connect, safe_connect_ex
    mock_socket = MagicMock()
    with (
        patch("app.core.shared_registry._original_connect") as mock_connect,
        patch("app.core.shared_registry._original_connect_ex") as mock_connect_ex,
    ):
        safe_connect(mock_socket, ("8.8.8.8", 80))
        mock_connect.assert_called_once_with(mock_socket, ("8.8.8.8", 80))

        safe_connect_ex(mock_socket, ("8.8.8.8", 80))
        mock_connect_ex.assert_called_once_with(mock_socket, ("8.8.8.8", 80))


def test_check_ai_status_corrupt_or_missing(tmp_path, monkeypatch):
    """Verify check_ai_status correctly warns when models are corrupt/missing."""
    from app.config import AppSettings
    from app.core.verifier import check_ai_status

    settings = AppSettings()
    settings.AI_ASSISTED_NAMING = True

    # Case 1: missing/uninstalled dependencies -> mock is_ml_available returning False
    with patch("app.core.verifier.is_ml_available", return_value=False):
        is_healthy, warn_msg = check_ai_status(settings)
        assert not is_healthy
        assert "dependencies" in warn_msg

    # Case 2: ML is available but files are missing
    with (
        patch("app.core.verifier.is_ml_available", return_value=True),
        patch("os.path.exists", return_value=False),
    ):
        is_healthy, warn_msg = check_ai_status(settings)
        assert not is_healthy
        assert "weights are missing or corrupt" in warn_msg
