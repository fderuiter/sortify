"""Unit tests for the Unified Offline Model Loading Utility."""

import os
import socket
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.core.offline_loader import (
    Florence2VisualProcessor,
    ModelWeightsNotFoundError,
    OfflineModelLoader,
    OfflineModelLoadError,
)
from app.core.shared_registry import SharedModelRegistry

pytestmark = [pytest.mark.slow, pytest.mark.ml]


@pytest.fixture
def trust_model_folders(monkeypatch):
    """Skip hash verification for tests that mock the filesystem to test other behavior."""
    monkeypatch.setattr(
        OfflineModelLoader, "_verify_candidate", classmethod(lambda cls, m, p: True)
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_model_weights_not_found_error():
    """Verify that ModelWeightsNotFoundError includes the searched locations in its message."""
    searched = ["/path/a", "/path/b"]
    err = ModelWeightsNotFoundError("test-model", searched)
    assert err.model_id == "test-model"
    assert err.searched_paths == searched
    assert "test-model" in str(err)
    assert "/path/a" in str(err)
    assert "/path/b" in str(err)


def test_dynamic_path_resolution_order(tmp_path, monkeypatch, trust_model_folders):
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

    with (
        patch("os.path.exists", side_effect=mock_exists),
        patch("os.path.isdir", side_effect=mock_isdir),
        patch.dict(os.environ, {"DUMMY_MODEL_PATH": str(env_dir)}),
    ):
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

    with (
        patch("os.path.exists", side_effect=mock_exists_meipass),
        patch("os.path.isdir", return_value=True),
    ):
        resolved = OfflineModelLoader.resolve_model_path("dummy-model")
        assert "sys_meipass" in resolved or "_MEIPASS" in resolved

    # The working directory is never searched (see
    # test_working_directory_is_not_searched), so the next fallback is home.
    def mock_exists_home(path):
        if "dummy-model" in path:
            if ".smart-autosorter" in path:
                if "config.json" in path:
                    return original_exists(str(home_dir / "config.json"))
                return True
        return False

    with (
        patch("os.path.exists", side_effect=mock_exists_home),
        patch("os.path.isdir", return_value=True),
    ):
        resolved = OfflineModelLoader.resolve_model_path("dummy-model")
        assert ".smart-autosorter" in resolved


def test_offline_sandboxing_enforced_connect(trust_model_folders):
    """Assert that a model loader trying to connect externally is blocked and raises OfflineModelLoadError."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("sandbox-model")

    def bad_loader_fn(path, *args, **kwargs):
        # Attempt an outgoing network connection
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("8.8.8.8", 80))  # External DNS IP
        return "loaded_model"

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.isdir", return_value=True),
        patch("os.listdir", return_value=["weights.bin"]),
    ):
        with pytest.raises(OfflineModelLoadError, match="prohibited network access"):
            OfflineModelLoader.load_model("sandbox-model", bad_loader_fn)


def test_offline_sandboxing_enforced_dns(trust_model_folders):
    """Assert that a model loader trying to resolve an external DNS name is blocked and raises OfflineModelLoadError."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("sandbox-model")

    def bad_loader_fn(path, *args, **kwargs):
        # Attempt DNS resolution
        socket.getaddrinfo("huggingface.co", 443)
        return "loaded_model"

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.isdir", return_value=True),
        patch("os.listdir", return_value=["weights.bin"]),
    ):
        with pytest.raises(OfflineModelLoadError, match="prohibited network access"):
            OfflineModelLoader.load_model("sandbox-model", bad_loader_fn)


def test_automatic_injection_of_local_files_only(trust_model_folders):
    """Verify that OfflineModelLoader.load_model automatically injects local_files_only=True and trust_remote_code=False."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("auto-inject-model")

    mock_loader = MagicMock(return_value="success_model")

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.isdir", return_value=True),
        patch("os.listdir", return_value=["some_weight"]),
    ):
        result = OfflineModelLoader.load_model("auto-inject-model", mock_loader)

        assert result == "success_model"
        # Assert loader called with local_files_only=True and trust_remote_code=False
        mock_loader.assert_called_once()
        _, kwargs = mock_loader.call_args
        assert kwargs.get("local_files_only") is True
        assert kwargs.get("trust_remote_code") is False


def test_offline_loader_rejects_trust_remote_code_true():
    """Verify that OfflineModelLoader.load_model raises OfflineModelLoadError if trust_remote_code=True is supplied."""
    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("test-remote-code-model")

    mock_loader = MagicMock()

    with (
        patch("os.path.exists", return_value=True),
        patch("os.path.isdir", return_value=True),
        patch("os.listdir", return_value=["weights.bin"]),
    ):
        with pytest.raises(OfflineModelLoadError, match="trust_remote_code=True"):
            OfflineModelLoader.load_model(
                "test-remote-code-model", mock_loader, trust_remote_code=True
            )

        mock_loader.assert_not_called()


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
    pytest.importorskip("transformers")
    mock_model = MagicMock()
    mock_processor = MagicMock()
    mock_transformers = MagicMock()
    mock_torch = MagicMock()

    # Mock from_pretrained on native HF classes
    mock_from_pretrained_model = (
        mock_transformers.Florence2ForConditionalGeneration.from_pretrained
    )
    mock_from_pretrained_model.return_value = mock_model
    mock_from_pretrained_processor = (
        mock_transformers.Florence2Processor.from_pretrained
    )
    mock_from_pretrained_processor.return_value = mock_processor

    mocker.patch.dict(
        "sys.modules",
        {"transformers": mock_transformers, "torch": mock_torch},
    )

    # Mock file path resolution
    mocker.patch(
        "app.core.offline_loader.OfflineModelLoader.resolve_model_path",
        return_value="/mock/florence-2",
    )

    # Initialize processor
    f2 = Florence2VisualProcessor()
    f2.load()

    assert f2.model == mock_model
    assert f2.processor == mock_processor

    # Assert load was done offline/sandboxed with trust_remote_code=False
    mock_from_pretrained_model.assert_called_once()
    mock_from_pretrained_processor.assert_called_once()
    _, model_kwargs = mock_from_pretrained_model.call_args
    _, proc_kwargs = mock_from_pretrained_processor.call_args
    assert model_kwargs.get("trust_remote_code") is False
    assert proc_kwargs.get("trust_remote_code") is False

    # Now mock run_image
    mock_image = MagicMock()
    mock_image.size = (800, 600)
    mock_image.convert.return_value = mock_image

    mocker.patch("PIL.Image.open", return_value=mock_image)
    mocker.patch("os.path.exists", return_value=True)

    # Mock processor encoding/decoding
    mock_processor.return_value = {"input_ids": "inputs", "pixel_values": "pixels"}
    mock_model.generate.return_value = ["output_tokens"]
    mock_processor.batch_decode.return_value = [
        "<OD><loc_100><loc_200><loc_300><loc_400>cat"
    ]

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


def test_florence2_hashes_registry_registration():
    """Verify that required Florence-2 model files are registered in HASHES dictionary without script files."""
    from app.core.hashes_registry import HASHES

    assert "florence-2" in HASHES
    florence_hashes = HASHES["florence-2"]
    assert "config.json" in florence_hashes
    assert "model.safetensors" in florence_hashes
    assert "processing_florence2.py" not in florence_hashes
    assert "modeling_florence2.py" not in florence_hashes


def test_florence2_pre_execution_integrity_valid_bundle(tmp_path, mocker):
    """Verify that loading an unmodified Florence-2 bundle passes integrity checks and loads the model."""
    pytest.importorskip("transformers")
    import hashlib

    f1_content = b'{"model_type": "florence2"}'
    f4_content = b"model_weights"

    (tmp_path / "config.json").write_bytes(f1_content)
    (tmp_path / "model.safetensors").write_bytes(f4_content)

    h1 = hashlib.sha256(f1_content).hexdigest()
    h4 = hashlib.sha256(f4_content).hexdigest()

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "florence-2",
        {
            "config.json": h1,
            "model.safetensors": h4,
        },
    )

    mocker.patch(
        "app.core.offline_loader.OfflineModelLoader.resolve_model_path",
        return_value=str(tmp_path),
    )
    mock_model = MagicMock()
    mock_processor = MagicMock()
    mock_transformers = MagicMock()
    mock_torch = MagicMock()

    mock_from_model = (
        mock_transformers.Florence2ForConditionalGeneration.from_pretrained
    )
    mock_from_model.return_value = mock_model
    mock_from_processor = mock_transformers.Florence2Processor.from_pretrained
    mock_from_processor.return_value = mock_processor

    mocker.patch.dict(
        "sys.modules",
        {"transformers": mock_transformers, "torch": mock_torch},
    )

    processor = Florence2VisualProcessor()
    processor.load()

    assert processor.model == mock_model
    assert processor.processor == mock_processor
    mock_from_model.assert_called_once()
    mock_from_processor.assert_called_once()


def test_florence2_pre_execution_integrity_tampered_bundle_aborts(tmp_path, mocker):
    """Verify that a tampered Florence-2 bundle fails integrity check before code execution."""
    pytest.importorskip("transformers")
    import hashlib

    f1_content = b'{"model_type": "florence2"}'
    f4_content = b"model_weights_original"

    (tmp_path / "config.json").write_bytes(f1_content)
    # Tampered file content!
    (tmp_path / "model.safetensors").write_bytes(b"TAMPERED_WEIGHTS")

    h1 = hashlib.sha256(f1_content).hexdigest()
    h4_expected = hashlib.sha256(f4_content).hexdigest()

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "florence-2",
        {
            "config.json": h1,
            "model.safetensors": h4_expected,
        },
    )

    mocker.patch(
        "app.core.offline_loader.OfflineModelLoader.resolve_model_path",
        return_value=str(tmp_path),
    )
    mock_transformers = MagicMock()
    mock_torch = MagicMock()

    mock_from_model = (
        mock_transformers.Florence2ForConditionalGeneration.from_pretrained
    )
    mock_from_processor = mock_transformers.Florence2Processor.from_pretrained

    mocker.patch.dict(
        "sys.modules",
        {"transformers": mock_transformers, "torch": mock_torch},
    )

    processor = Florence2VisualProcessor()

    with pytest.raises(
        OfflineModelLoadError, match="Integrity check failed for model.safetensors"
    ):
        processor.load()

    # Crucial security assertion: Code execution / model loading was NEVER called
    mock_from_model.assert_not_called()
    mock_from_processor.assert_not_called()


def test_florence2_chunked_hash_verification(tmp_path):
    """Verify that verify_integrity computes 64KB chunked SHA-256 digests accurately."""
    import hashlib

    # Create a file larger than 64KB (128KB)
    large_data = b"X" * (128 * 1024)
    file_path = tmp_path / "model.safetensors"
    file_path.write_bytes(large_data)

    expected_hash = hashlib.sha256(large_data).hexdigest()

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "florence-2", {"model.safetensors": expected_hash}
    )

    # Valid check
    assert registry.verify_integrity("florence-2", str(tmp_path)) is True

    # Tamper check
    file_path.write_bytes(large_data + b"TAMPERED")
    with pytest.raises(ValueError, match="Integrity check failed"):
        registry.verify_integrity("florence-2", str(tmp_path))


def test_sidecar_model_hydration_success(tmp_path, monkeypatch):
    """Verify that OfflineModelLoader.resolve_model_path hydrates model weights from smart-autosorter-models.zip."""
    import zipfile

    # Create dummy model files inside a zip
    zip_dir = tmp_path / "sidecar"
    zip_dir.mkdir()
    sidecar_zip = zip_dir / "smart-autosorter-models.zip"

    target_home = tmp_path / "user_home"
    target_home.mkdir()

    monkeypatch.setenv("HOME", str(target_home))
    monkeypatch.setenv("USERPROFILE", str(target_home))
    monkeypatch.setenv("SMART_AUTOSORTER_MODELS_ZIP", str(sidecar_zip))

    with zipfile.ZipFile(sidecar_zip, "w") as zf:
        zf.writestr("model/config.json", '{"model_type": "minilm"}')
        zf.writestr("model/model.onnx", "dummy_onnx_weights")

    OfflineModelLoader._registered_models.clear()
    OfflineModelLoader.register_model("model", expected_files=["config.json"])
    SharedModelRegistry._instance = None
    SharedModelRegistry.get_instance().register_expected_hashes(
        "generative_naming",
        {
            "config.json": _sha256(b'{"model_type": "minilm"}'),
            "model.onnx": _sha256(b"dummy_onnx_weights"),
        },
    )

    progress_updates = []

    def progress_cb(update):
        progress_updates.append(update)

    resolved = OfflineModelLoader.resolve_model_path(
        "model", progress_callback=progress_cb
    )

    assert os.path.exists(resolved)
    assert os.path.exists(os.path.join(resolved, "config.json"))
    assert len(progress_updates) > 0
    stages = [u.stage for u in progress_updates if getattr(u, "stage", None)]
    assert any("Verifying sidecar model package" in s for s in stages)
    assert any("Sidecar model hydration complete" in s for s in stages)


def test_sidecar_hydration_tampered_weights_error(tmp_path, monkeypatch):
    """Verify that sidecar hydration aborts if extracted weights fail integrity verification."""
    import zipfile

    sidecar_zip = tmp_path / "smart-autosorter-models.zip"
    target_home = tmp_path / "user_home"
    target_home.mkdir()

    monkeypatch.setenv("HOME", str(target_home))
    monkeypatch.setenv("USERPROFILE", str(target_home))
    monkeypatch.setenv("SMART_AUTOSORTER_MODELS_ZIP", str(sidecar_zip))

    with zipfile.ZipFile(sidecar_zip, "w") as zf:
        zf.writestr("easyocr/craft_mlt_25k.pth", "TAMPERED_WEIGHT_DATA")

    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "easyocr",
        {
            "craft_mlt_25k.pth": "0000000000000000000000000000000000000000000000000000000000000000"
        },
    )

    with pytest.raises(OfflineModelLoadError, match="(?i)integrity check"):
        OfflineModelLoader.hydrate_sidecar_models(
            sidecar_zip_path=str(sidecar_zip),
            target_dir=str(target_home / ".smart-autosorter" / "offline_bundle"),
        )


def _write_sidecar(zip_path, files):
    import zipfile

    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("SMART_AUTOSORTER_MODELS_ZIP", raising=False)
    monkeypatch.delenv("MODELS_ZIP_PATH", raising=False)
    OfflineModelLoader._registered_models.clear()
    SharedModelRegistry._instance = None
    yield home
    SharedModelRegistry._instance = None


def test_tampered_sidecar_weights_never_become_resolvable(
    tmp_path, monkeypatch, isolated_home
):
    """Weights that fail verification are not left on disk for the next lookup."""
    sidecar = tmp_path / "smart-autosorter-models.zip"
    _write_sidecar(sidecar, {"mymodel/model.onnx": b"TAMPERED WEIGHTS"})
    monkeypatch.setenv("SMART_AUTOSORTER_MODELS_ZIP", str(sidecar))
    SharedModelRegistry.get_instance().register_expected_hashes(
        "mymodel", {"model.onnx": "0" * 64}
    )
    OfflineModelLoader.register_model("mymodel", expected_files=["model.onnx"])

    with pytest.raises(ModelWeightsNotFoundError):
        OfflineModelLoader.resolve_model_path("mymodel")
    sidecar.unlink()

    bundle = isolated_home / ".smart-autosorter" / "offline_bundle"
    assert not (bundle / "mymodel").exists()
    assert not any(p.name.startswith(".hydrate-") for p in bundle.parent.iterdir())
    with pytest.raises(ModelWeightsNotFoundError):
        OfflineModelLoader.resolve_model_path("mymodel")


def test_tampered_folder_already_on_disk_is_refused(isolated_home):
    """A model folder whose files don't match the registered hashes is skipped."""
    folder = isolated_home / ".smart-autosorter" / "offline_bundle" / "mymodel"
    folder.mkdir(parents=True)
    (folder / "model.onnx").write_bytes(b"TAMPERED WEIGHTS")
    SharedModelRegistry.get_instance().register_expected_hashes(
        "mymodel", {"model.onnx": _sha256(b"GOOD WEIGHTS")}
    )

    with pytest.raises(ModelWeightsNotFoundError):
        OfflineModelLoader.resolve_model_path("mymodel")

    (folder / "model.onnx").write_bytes(b"GOOD WEIGHTS")
    assert OfflineModelLoader.resolve_model_path("mymodel") == str(folder)


def test_model_folder_without_registered_hashes_is_refused(isolated_home):
    """There is nothing to verify an unregistered model against, so it is refused."""
    folder = isolated_home / ".smart-autosorter" / "offline_bundle" / "unknown"
    folder.mkdir(parents=True)
    (folder / "weights.bin").write_bytes(b"weights")

    with pytest.raises(ModelWeightsNotFoundError):
        OfflineModelLoader.resolve_model_path("unknown")


def test_working_directory_is_not_searched(tmp_path, monkeypatch, isolated_home):
    """Neither a sidecar zip nor an offline_bundle in the CWD is used."""
    cwd = tmp_path / "downloads"
    (cwd / "offline_bundle" / "mymodel").mkdir(parents=True)
    (cwd / "offline_bundle" / "mymodel" / "model.onnx").write_bytes(b"W")
    _write_sidecar(cwd / "smart-autosorter-models.zip", {"mymodel/model.onnx": b"W"})
    monkeypatch.chdir(cwd)
    SharedModelRegistry.get_instance().register_expected_hashes(
        "mymodel", {"model.onnx": _sha256(b"W")}
    )

    candidates = OfflineModelLoader._find_sidecar_zip_candidates()
    assert not any(c.startswith(str(cwd)) for c in candidates)
    with pytest.raises(ModelWeightsNotFoundError):
        OfflineModelLoader.resolve_model_path("mymodel")


def test_verify_integrity_fails_on_any_missing_registered_file(tmp_path):
    """A missing registered file fails verification instead of skipping the rest."""
    from app.core.exceptions import HashVerificationError  # noqa: F401

    (tmp_path / "a.bin").write_bytes(b"a")
    registry = SharedModelRegistry()
    registry.register_expected_hashes(
        "two-files", {"missing.bin": "0" * 64, "a.bin": "1" * 64}
    )

    with pytest.raises(FileNotFoundError):
        registry.verify_integrity("two-files", str(tmp_path))
