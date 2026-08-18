"""Unit tests for the Inline Offline Bundle Importer."""

import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import zipfile
from unittest.mock import patch

import pytest

from app.config import AppSettings
from app.core.offline_importer import OfflineBundleImporter
from app.core.shared_registry import SharedModelRegistry


@pytest.fixture
def temp_dirs():
    work_dir = tempfile.mkdtemp(prefix="test_offline_import_")
    target_dir = os.path.join(work_dir, "target_model")
    source_dir = os.path.join(work_dir, "source_model")
    os.makedirs(source_dir, exist_ok=True)

    yield {
        "work_dir": work_dir,
        "target_dir": target_dir,
        "source_dir": source_dir,
    }

    shutil.rmtree(work_dir, ignore_errors=True)


def create_mock_model_bundle(source_dir, config_content=None, extra_files=None):
    if config_content is None:
        config_content = {"model_type": "generative", "version": "1.0"}
    
    config_path = os.path.join(source_dir, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_content, f)

    created_files = {"config.json": config_path}
    if extra_files:
        for name, content in extra_files.items():
            file_path = os.path.join(source_dir, name)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            if isinstance(content, bytes):
                with open(file_path, "wb") as f:
                    f.write(content)
            else:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
            created_files[name] = file_path

    return created_files


def create_zip_archive(zip_path, file_map):
    with zipfile.ZipFile(zip_path, "w") as zf:
        for arcname, filepath in file_map.items():
            zf.write(filepath, arcname)


def test_import_valid_zip_archive(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]
    work_dir = temp_dirs["work_dir"]

    # Create model files
    files = create_mock_model_bundle(
        source_dir,
        extra_files={"model.onnx": b"dummy_model_weights_data_123"},
    )
    zip_path = os.path.join(work_dir, "bundle.zip")
    create_zip_archive(zip_path, {"config.json": files["config.json"], "model.onnx": files["model.onnx"]})

    # Register expected hashes
    with open(files["config.json"], "rb") as f:
        config_hash = hashlib.sha256(f.read()).hexdigest()
    model_hash = hashlib.sha256(b"dummy_model_weights_data_123").hexdigest()
    
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("generative_naming", {"config.json": config_hash, "model.onnx": model_hash})
    registry.register_expected_hashes("model_download", {"model.onnx": model_hash})

    settings = AppSettings()
    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_archive_async(
        zip_path=zip_path,
        target_model_dir=target_dir,
        settings=settings,
        on_done=on_done,
    )

    assert done_event.wait(timeout=5)
    assert results["success"] is True
    assert results["err"] is None
    assert os.path.exists(os.path.join(target_dir, "config.json"))
    assert os.path.exists(os.path.join(target_dir, "model.onnx"))
    assert getattr(settings, "AI_CONSENT_GRANTED", False) is True


def test_import_zip_nested_structure(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]
    work_dir = temp_dirs["work_dir"]

    files = create_mock_model_bundle(source_dir)
    zip_path = os.path.join(work_dir, "nested_bundle.zip")
    create_zip_archive(zip_path, {"subfolder/config.json": files["config.json"]})

    with open(files["config.json"], "rb") as f:
        config_hash = hashlib.sha256(f.read()).hexdigest()
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("generative_naming", {"config.json": config_hash})

    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_archive_async(
        zip_path=zip_path,
        target_model_dir=target_dir,
        on_done=on_done,
    )

    assert done_event.wait(timeout=5)
    assert results["success"] is True
    assert os.path.exists(os.path.join(target_dir, "config.json"))


def test_import_zip_missing_config_fails(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    work_dir = temp_dirs["work_dir"]
    target_dir = temp_dirs["target_dir"]

    # Create zip without config.json
    dummy_txt = os.path.join(work_dir, "readme.txt")
    with open(dummy_txt, "w") as f:
        f.write("hello")

    zip_path = os.path.join(work_dir, "no_config.zip")
    create_zip_archive(zip_path, {"readme.txt": dummy_txt})

    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_archive_async(
        zip_path=zip_path,
        target_model_dir=target_dir,
        on_done=on_done,
    )

    assert done_event.wait(timeout=5)
    assert results["success"] is False
    assert "config.json" in results["err"]
    assert not os.path.exists(target_dir)


def test_import_zip_corrupt_config_fails(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]
    work_dir = temp_dirs["work_dir"]

    # Corrupt config.json
    config_path = os.path.join(source_dir, "config.json")
    with open(config_path, "w") as f:
        f.write("{invalid json content:")

    zip_path = os.path.join(work_dir, "corrupt_config.zip")
    create_zip_archive(zip_path, {"config.json": config_path})

    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_archive_async(
        zip_path=zip_path,
        target_model_dir=target_dir,
        on_done=on_done,
    )

    assert done_event.wait(timeout=5)
    assert results["success"] is False
    assert "config.json" in results["err"] or "Corrupt" in results["err"]


def test_import_checksum_mismatch_fails(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]
    work_dir = temp_dirs["work_dir"]

    files = create_mock_model_bundle(
        source_dir,
        extra_files={"model.onnx": b"tampered_weights"},
    )
    zip_path = os.path.join(work_dir, "tampered.zip")
    create_zip_archive(zip_path, {"config.json": files["config.json"], "model.onnx": files["model.onnx"]})

    # Register expected hash for model.onnx that doesn't match
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", {"model.onnx": "expected_valid_sha256_hash_value"}
    )

    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_archive_async(
        zip_path=zip_path,
        target_model_dir=target_dir,
        on_done=on_done,
    )

    assert done_event.wait(timeout=5)
    assert results["success"] is False
    assert "checksum mismatch" in results["err"].lower()
    assert not os.path.exists(target_dir)


def test_import_uncompressed_directory(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]

    files = create_mock_model_bundle(source_dir, extra_files={"vocab.txt": "abc"})

    with open(files["config.json"], "rb") as f:
        config_hash = hashlib.sha256(f.read()).hexdigest()
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("generative_naming", {"config.json": config_hash})

    settings = AppSettings()
    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_directory_async(
        dir_path=source_dir,
        target_model_dir=target_dir,
        settings=settings,
        on_done=on_done,
    )

    assert done_event.wait(timeout=5)
    assert results["success"] is True
    assert os.path.exists(os.path.join(target_dir, "config.json"))
    assert os.path.exists(os.path.join(target_dir, "vocab.txt"))
    assert getattr(settings, "AI_CONSENT_GRANTED", False) is True


def test_import_cancellation(temp_dirs):
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]
    work_dir = temp_dirs["work_dir"]

    # Create many files to give enough time to cancel
    extra_files = {f"weight_{i}.bin": b"x" * 10000 for i in range(50)}
    files = create_mock_model_bundle(source_dir, extra_files=extra_files)

    zip_path = os.path.join(work_dir, "large_bundle.zip")
    archive_files = {"config.json": files["config.json"]}
    for i in range(50):
        archive_files[f"weight_{i}.bin"] = files[f"weight_{i}.bin"]
    create_zip_archive(zip_path, archive_files)

    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    importer.import_archive_async(
        zip_path=zip_path,
        target_model_dir=target_dir,
        on_done=on_done,
    )

    # Cancel immediately
    importer.cancel_import()

    assert done_event.wait(timeout=5)
    assert results["success"] is False
    assert "canceled" in results["err"].lower() or "cancelled" in results["err"].lower()


def test_import_network_isolation(temp_dirs):
    """Verify that network isolation blocks external socket creation during import."""
    importer = OfflineBundleImporter.get_instance()
    importer.reset_state()

    source_dir = temp_dirs["source_dir"]
    target_dir = temp_dirs["target_dir"]

    create_mock_model_bundle(source_dir)

    network_attempt_blocked = threading.Event()

    original_verify = importer._validate_config_file

    def mock_validate_with_network_attempt(model_root):
        original_verify(model_root)
        # Attempt an external network connection during import
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("8.8.8.8", 80))
        except PermissionError:
            network_attempt_blocked.set()
            raise

    done_event = threading.Event()
    results = {}

    def on_done(success, err):
        results["success"] = success
        results["err"] = err
        done_event.set()

    with patch.object(importer, "_validate_config_file", side_effect=mock_validate_with_network_attempt):
        importer.import_directory_async(
            dir_path=source_dir,
            target_model_dir=target_dir,
            on_done=on_done,
        )

        assert done_event.wait(timeout=5)
        assert network_attempt_blocked.is_set()
        assert results["success"] is False
        assert "blocked" in results["err"].lower() or "permission" in results["err"].lower()
