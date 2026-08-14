"""Unit tests for the Unified Offline Model Loading Utility."""

import os
import sys
import socket
from unittest.mock import MagicMock, patch

import pytest

from app.core.offline_loader import (
    OfflineModelLoader,
    Florence2VisualProcessor,
    OfflineModelLoadError,
    ModelWeightsNotFoundError,
)
from app.core.shared_registry import SharedModelRegistry


def test_model_weights_not_found_error():
    """Verify that ModelWeightsNotFoundError includes the searched locations in its message."""
    searched = ["/path/a", "/path/b"]
    err = ModelWeightsNotFoundError("test-model", searched)
    assert err.model_id == "test-model"
    assert err.searched_paths == searched
    assert "test-model" in str(err)
    assert "/path/a" in str(err)
    assert "/path/b" in str(err)


def test_dynamic_path_resolution_order(tmp_path, monkeypatch):
    """Verify that path resolution checks fallback directories in correct order of precedence."""
    # Reset/clear registry of models for isolated testing
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("dummy-model", expected_files=["config.json"])

    # Create mock paths
    env_dir = tmp_path / "env"
    meipass_dir = tmp_path / "meipass"
    workspace_dir = tmp_path / "workspace"
    home_dir = tmp_path / "home"

    # Helper to setup config.json in a dir
    def setup_dir(d):
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text("{}", encoding="utf-8")

    # Set up all directories
    setup_dir(env_dir)
    setup_dir(meipass_dir)
    setup_dir(workspace_dir)
    setup_dir(home_dir)

    # Mock MEIPASS and sys.frozen
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "sys_meipass"), raising=False)
    # Patch getcwd to point to workspace root
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))

    # Also mock home directory
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # Mock paths inside offline_loader search logic to redirect to our tmp_path test folders
    def mock_resolve(model_id):
        # We'll mock resolve_model_path's inner paths list or we can patch the os.path.exists checks
        pass

    # Let's mock os.path.exists to simulate fallback logic step-by-step
    original_exists = os.path.exists
    original_isdir = os.path.isdir
    original_listdir = os.listdir

    def mock_exists(path):
        # Redirect specific search paths to our tmp_path test folders
        if "dummy-model" in path:
            if "env" in path:
                return original_exists(str(env_dir))
            if "meipass" in path:
                return original_exists(str(meipass_dir))
            if "workspace" in path or "getcwd" in path or str(tmp_path) in path:
                # Avoid matching tmp_path parent
                if "config.json" in path:
                    return original_exists(str(workspace_dir / "config.json"))
                return original_exists(str(workspace_dir))
            if ".smart-autosorter" in path or "home" in path:
                if "config.json" in path:
                    return original_exists(str(home_dir / "config.json"))
                return original_exists(str(home_dir))
        return original_exists(path)

    def mock_isdir(path):
        if "dummy-model" in path:
            return True
        return original_isdir(path)

    with patch("os.path.exists", side_effect=mock_exists), \
         patch("os.path.isdir", side_effect=mock_isdir), \
         patch.dict(os.environ, {"DUMMY_MODEL_PATH": str(env_dir)}):

        # Precedence 1: Env variable path should win
        resolved = OfflineModelLoader.resolve_model_path("dummy-model")
        assert resolved == str(env_dir)

    # Disable env variable path -> Precedence 2: MEIPASS
    # Let's patch os.path.exists to only return True for MEIPASS path
    def mock_exists_meipass(path):
        if "dummy-model" in path:
            if "sys_meipass" in path or "_MEIPASS" in path:
                if "config.json" in path:
                    return original_exists(str(meipass_dir / "config.json"))
                return True
        return False

    with patch("os.path.exists", side_effect=mock_exists_meipass), \
         patch("os.path.isdir", return_value=True):
        resolved = OfflineModelLoader.resolve_model_path("dummy-model")
        assert "sys_meipass" in resolved or "_MEIPASS" in resolved

    # Disable MEIPASS path -> Precedence 3: Workspace folder
    def mock_exists_workspace(path):
        if "dummy-model" in path:
            if "offline_bundle" in path and not "sys_meipass" in path and not ".smart-autosorter" in path:
                if "config.json" in path:
                    return original_exists(str(workspace_dir / "config.json"))
                return True
        return False

    with patch("os.path.exists", side_effect=mock_exists_workspace), \
         patch("os.path.isdir", return_value=True):
        resolved = OfflineModelLoader.resolve_model_path("dummy-model")
        assert "offline_bundle" in resolved
        assert ".smart-autosorter" not in resolved

    # Disable Workspace -> Precedence 4: Home directory fallback
    def mock_exists_home(path):
        if "dummy-model" in path:
            if ".smart-autosorter" in path:
                if "config.json" in path:
                    return original_exists(str(home_dir / "config.json"))
                return True
        return False

    with patch("os.path.exists", side_effect=mock_exists_home), \
         patch("os.path.isdir", return_value=True):
        resolved = OfflineModelLoader.resolve_model_path("dummy-model")
        assert ".smart-autosorter" in resolved


def test_offline_sandboxing_enforced_connect():
    """Assert that a model loader trying to connect externally is blocked and raises OfflineModelLoadError."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("sandbox-model")

    def bad_loader_fn(path, *args, **kwargs):
        # Attempt an outgoing network connection
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 80))  # External DNS IP
        return "loaded_model"

    with patch("os.path.exists", return_value=True), \
         patch("os.path.isdir", return_value=True), \
         patch("os.listdir", return_value=["weights.bin"]):

        with pytest.raises(OfflineModelLoadError, match="prohibited network access"):
            OfflineModelLoader.load_model("sandbox-model", bad_loader_fn)


def test_offline_sandboxing_enforced_dns():
    """Assert that a model loader trying to resolve an external DNS name is blocked and raises OfflineModelLoadError."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("sandbox-model")

    def bad_loader_fn(path, *args, **kwargs):
        # Attempt DNS resolution
        socket.getaddrinfo("huggingface.co", 443)
        return "loaded_model"

    with patch("os.path.exists", return_value=True), \
         patch("os.path.isdir", return_value=True), \
         patch("os.listdir", return_value=["weights.bin"]):

        with pytest.raises(OfflineModelLoadError, match="prohibited network access"):
            OfflineModelLoader.load_model("sandbox-model", bad_loader_fn)


def test_automatic_injection_of_local_files_only():
    """Verify that OfflineModelLoader.load_model automatically injects local_files_only=True."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("auto-inject-model")

    mock_loader = MagicMock(return_value="success_model")

    with patch("os.path.exists", return_value=True), \
         patch("os.path.isdir", return_value=True), \
         patch("os.listdir", return_value=["some_weight"]):

        result = OfflineModelLoader.load_model("auto-inject-model", mock_loader)

        assert result == "success_model"
        # Assert loader called with local_files_only=True
        mock_loader.assert_called_once()
        _, kwargs = mock_loader.call_args
        assert kwargs.get("local_files_only") is True


def test_florence2_coordinate_normalization_and_scaling():
    """Verify that coordinate coordinates (grid 0-1000) are normalized (0-1) and scaled to original image size."""
    # Test text: Bounding box <loc_100><loc_200><loc_300><loc_400> representing a box
    # With a preceding label "cat"
    text = "cat<loc_100><loc_200><loc_300><loc_400>"
    image_size = (640, 480)  # width=640, height=480

    parsed = Florence2VisualProcessor.parse_and_sanitize(text, image_size)

    assert parsed["raw_output"] == text
    assert parsed["sanitized_text"] == "cat"
    assert len(parsed["coordinates"]) == 1

    coord = parsed["coordinates"][0]
    # ymin=100, xmin=200, ymax=300, xmax=400 (all scaled to 1000)
    assert coord["box_2d_relative"] == [0.1, 0.2, 0.3, 0.4]
    # Scaled coordinates: relative * height (for Y) or width (for X)
    # ymin_scaled = 0.1 * 480 = 48
    # xmin_scaled = 0.2 * 640 = 128
    # ymax_scaled = 0.3 * 480 = 144
    # xmax_scaled = 0.4 * 640 = 256
    assert coord["box_2d_scaled"] == [48.0, 128.0, 144.0, 256.0]
    assert coord["label"] == "cat"


def test_florence2_parse_multiple_boxes():
    """Verify that multiple boxes with various labels and locations are parsed and sanitized successfully."""
    text = "<OD> Bounding boxes: <loc_100><loc_150><loc_200><loc_250>cat <loc_500><loc_600><loc_700><loc_800> dog"
    image_size = (1000, 1000)

    parsed = Florence2VisualProcessor.parse_and_sanitize(text, image_size)

    assert parsed["sanitized_text"] == "Bounding boxes: cat dog"
    assert len(parsed["coordinates"]) == 2

    # Box 1
    c1 = parsed["coordinates"][0]
    assert c1["box_2d_relative"] == [0.1, 0.15, 0.2, 0.25]
    assert c1["box_2d_scaled"] == [100.0, 150.0, 200.0, 250.0]
    assert c1["label"] == "cat"

    # Box 2
    c2 = parsed["coordinates"][1]
    assert c2["box_2d_relative"] == [0.5, 0.6, 0.7, 0.8]
    assert c2["box_2d_scaled"] == [500.0, 600.0, 700.0, 800.0]
    assert c2["label"] == "dog"


def test_florence2_parse_empty_label_fallback():
    """Verify that when no adjacent word/label is found, the parser falls back gracefully to a default label."""
    text = "<loc_100><loc_100><loc_200><loc_200>"
    image_size = (100, 100)

    parsed = Florence2VisualProcessor.parse_and_sanitize(text, image_size)

    assert len(parsed["coordinates"]) == 1
    assert parsed["coordinates"][0]["label"] == "detected_object"


def test_florence2_visual_processor_mock_load_and_run(mocker):
    """Verify the local Florence-2 loading pipeline and process_image method with fully mocked model layers."""
    mock_model = MagicMock()
    mock_processor = MagicMock()

    # Mock from_pretrained on the classes directly
    mock_from_pretrained_model = mocker.patch(
        "transformers.AutoModelForCausalLM.from_pretrained", return_value=mock_model
    )
    mock_from_pretrained_processor = mocker.patch(
        "transformers.AutoProcessor.from_pretrained", return_value=mock_processor
    )

    # Mock file path resolution
    mocker.patch("app.core.offline_loader.OfflineModelLoader.resolve_model_path", return_value="/mock/florence-2")

    # Initialize processor
    f2 = Florence2VisualProcessor()
    f2.load()

    assert f2.model == mock_model
    assert f2.processor == mock_processor

    # Assert load was done offline/sandboxed
    mock_from_pretrained_model.assert_called_once()
    mock_from_pretrained_processor.assert_called_once()


    # Now mock run_image
    mock_image = MagicMock()
    mock_image.size = (800, 600)
    mock_image.convert.return_value = mock_image

    mocker.patch("PIL.Image.open", return_value=mock_image)
    mocker.patch("os.path.exists", return_value=True)

    # Mock processor encoding/decoding
    mock_processor.return_value = {"input_ids": "inputs", "pixel_values": "pixels"}
    mock_model.generate.return_value = ["output_tokens"]
    mock_processor.batch_decode.return_value = ["<OD><loc_100><loc_200><loc_300><loc_400>cat"]

    result = f2.process_image("/path/to/cat.jpg", task_prompt="<OD>")

    assert result["sanitized_text"] == "cat"
    assert len(result["coordinates"]) == 1
    assert result["coordinates"][0]["box_2d_relative"] == [0.1, 0.2, 0.3, 0.4]
    assert result["coordinates"][0]["label"] == "cat"


def test_shared_registry_get_florence_processor(mocker):
    """Assert that get_florence_processor on SharedModelRegistry returns a cached singleton instance of Florence2VisualProcessor."""
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    # Mock the load of Florence-2
    mock_load = mocker.patch.object(Florence2VisualProcessor, "load", return_value=None)

    proc1 = registry.get_florence_processor()
    proc2 = registry.get_florence_processor()

    assert proc1 is proc2
    assert isinstance(proc1, Florence2VisualProcessor)
    mock_load.assert_called_once()
