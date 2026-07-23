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
    registry.register_expected_hashes("generative_naming", {"config.json": "wrong_hash"})

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
    with pytest.raises(PermissionError, match="External network connections are blocked"):
        future.result()


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
