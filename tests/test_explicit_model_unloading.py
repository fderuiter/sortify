"""Unit and integration tests for Explicit Model Unloading & Pipeline Lifecycle Management."""

import os
import threading
from unittest.mock import MagicMock

import pytest

from app.core.cro_multi_study_pipeline import CROMultiStudyPipeline
from app.core.downloader import DownloadManager
from app.core.shared_registry import SharedModelRegistry


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure SharedModelRegistry is cleared before and after each test."""
    registry = SharedModelRegistry.get_instance()
    registry.unload_all_models()
    yield
    registry.unload_all_models()


def test_unload_model_and_is_model_loaded():
    """Test explicit model unloading and loaded state tracking."""
    registry = SharedModelRegistry.get_instance()

    # Pre-populate mock model objects in registry
    mock_ocr = MagicMock()
    mock_onnx = MagicMock()
    mock_gen = (MagicMock(), "text-generation", MagicMock())

    registry._models["easyocr"] = mock_ocr
    registry._models["easyocr_info"] = (["en"], False)
    registry._models["onnx_/fake/path.onnx"] = mock_onnx
    registry._models["generative_naming"] = mock_gen

    assert registry.is_model_loaded("easyocr") is True
    assert registry.is_model_loaded("onnx") is True
    assert registry.is_model_loaded("generative_naming") is True

    # Unload easyocr
    unloaded = registry.unload_model("easyocr")
    assert unloaded is True
    assert registry.is_model_loaded("easyocr") is False
    assert "easyocr" not in registry._models
    assert "easyocr_info" not in registry._models

    # Unload onnx
    unloaded_onnx = registry.unload_model("onnx")
    assert unloaded_onnx is True
    assert registry.is_model_loaded("onnx") is False

    # Unload all remaining
    registry.unload_all_models()
    assert len(registry._models) == 0


def test_free_memory_clears_hardware_buffers(mocker):
    """Test that unloading models triggers garbage collection and hardware buffer clearing."""
    mock_gc = mocker.patch("gc.collect")
    mock_torch = MagicMock()
    mock_torch.cuda.is_available.return_value = True
    mocker.patch.dict("sys.modules", {"torch": mock_torch})

    registry = SharedModelRegistry.get_instance()
    registry._models["test_model"] = MagicMock()

    registry.unload_model("test_model")

    mock_gc.assert_called()
    mock_torch.cuda.empty_cache.assert_called()


def test_concurrent_load_and_unload():
    """Test thread lock synchronization under concurrent load/unload requests."""
    registry = SharedModelRegistry.get_instance()
    errors = []

    def load_task(i):
        try:
            for _ in range(20):
                registry._models[f"onnx_model_{i}"] = MagicMock()
                registry.is_model_loaded("onnx")
                registry.unload_model(f"onnx_model_{i}")
        except Exception as e:
            errors.append(e)

    def unload_all_task():
        try:
            for _ in range(20):
                registry.unload_all_models()
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=load_task, args=(i,)) for i in range(5)
    ] + [
        threading.Thread(target=unload_all_task) for _ in range(2)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0


def test_auto_reinitialization_on_demand(mocker):
    """Test that models automatically re-initialize on demand after prior unload."""
    registry = SharedModelRegistry.get_instance()

    mocker.patch("onnxruntime.InferenceSession", return_value=MagicMock())
    mocker.patch("os.path.exists", return_value=True)

    session1 = registry.get_onnx_session("/fake/path.onnx")
    assert session1 is not None
    assert registry.is_model_loaded("onnx") is True

    # Explicit unload
    registry.unload_model("onnx")
    assert registry.is_model_loaded("onnx") is False

    # Request again -> re-initializes on demand
    session2 = registry.get_onnx_session("/fake/path.onnx")
    assert session2 is not None
    assert registry.is_model_loaded("onnx") is True


def test_model_cache_reset_purges_in_memory_models(tmp_path, mocker):
    """Test that reset/deletion of model cache purges in-memory model instances."""
    registry = SharedModelRegistry.get_instance()
    registry._models["easyocr"] = MagicMock()
    registry._models["generative_naming"] = (MagicMock(), "task", MagicMock())

    assert len(registry._models) > 0

    downloader = DownloadManager.get_instance()
    dummy_dir = str(tmp_path / "model")
    os.makedirs(dummy_dir, exist_ok=True)

    done_event = threading.Event()

    def on_done(success, err):
        done_event.set()

    downloader.delete_model_async(dummy_dir, on_done=on_done)
    done_event.wait(timeout=5.0)

    # All in-memory models should be purged
    assert len(registry._models) == 0


def test_cro_pipeline_stage_unloading_hooks(tmp_path, mocker):
    """Test that CROMultiStudyPipeline triggers model memory release at stage transitions."""
    registry = SharedModelRegistry.get_instance()
    unload_calls = []

    original_unload = registry.unload_model
    original_unload_all = registry.unload_all_models

    def track_unload(model_id):
        unload_calls.append(model_id)
        return original_unload(model_id)

    def track_unload_all():
        unload_calls.append("ALL")
        return original_unload_all()

    mocker.patch.object(registry, "unload_model", side_effect=track_unload)
    mocker.patch.object(registry, "unload_all_models", side_effect=track_unload_all)

    src_dir = str(tmp_path / "src")
    dst_dir = str(tmp_path / "dst")
    os.makedirs(src_dir, exist_ok=True)

    # Populate a dummy file in src
    with open(os.path.join(src_dir, "test.txt"), "w") as f:
        f.write("Clinical protocol document content")

    pipeline = CROMultiStudyPipeline()
    pipeline.run_pipeline(src_dir, dst_dir)

    # Verify stage transition unload calls occurred
    assert "easyocr" in unload_calls
    assert "florence-2" in unload_calls
    assert "onnx" in unload_calls
    assert "generative_naming" in unload_calls
    assert "ALL" in unload_calls
