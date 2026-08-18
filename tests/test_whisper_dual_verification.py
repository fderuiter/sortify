"""Tests for Comprehensive Whisper Management and Dual Verification."""

import hashlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings, Settings
from app.core.downloader import DownloadManager, ModelVerificationError
from app.core.extractor_strategies import AudioExtractor
from app.core.hashes_registry import HASHES
from app.core.shared_registry import SharedModelRegistry
from app.core.verifier import (
    get_whisper_model_path,
    verify_whisper_binary,
    verify_whisper_dual,
    verify_whisper_model_weight,
)


def test_whisper_settings_configuration():
    """Test selecting Whisper model sizes and setting custom binary paths in settings."""
    settings = Settings(WHISPER_MODEL_SIZE="medium", WHISPER_CMD="/usr/local/bin/whisper")
    assert settings.WHISPER_MODEL_SIZE == "medium"
    assert settings.WHISPER_CMD == "/usr/local/bin/whisper"

    # Test validator for invalid model size
    with pytest.raises(ValueError):
        Settings(WHISPER_MODEL_SIZE="ultra_large")

    # Test list support for WHISPER_CMD
    settings_list = Settings(WHISPER_CMD=["python", "-m", "whisper"])
    assert settings_list.WHISPER_CMD == ["python", "-m", "whisper"]


def test_central_hash_registry_includes_whisper():
    """Test that the central registry stores SHA-256 checksums for Whisper executables and weights."""
    assert "whisper_binaries" in HASHES
    assert "whisper_models" in HASHES
    assert "whisper" in HASHES

    assert "base" in HASHES["whisper_models"]
    assert "tiny" in HASHES["whisper_models"]
    assert "whisper" in HASHES["whisper_binaries"]

    registry = SharedModelRegistry.get_instance()
    assert "whisper_binaries" in registry._expected_hashes
    assert "whisper_models" in registry._expected_hashes


def test_verify_whisper_binary_integrity(tmp_path):
    """Test cryptographic SHA-256 verification for Whisper binary executables."""
    fake_binary = tmp_path / "fake_whisper"
    fake_content = b"fake whisper binary content"
    fake_binary.write_bytes(fake_content)

    actual_hash = hashlib.sha256(fake_content).hexdigest()

    registry = SharedModelRegistry.get_instance()
    # Register the valid hash for fake_whisper
    registry.register_expected_hashes("whisper_binaries", {"fake_whisper": actual_hash})

    # Test valid binary verification
    ok, msg = verify_whisper_binary(str(fake_binary))
    assert ok is True
    assert "verified" in msg.lower()

    # Tamper with the binary content
    fake_binary.write_bytes(b"tampered whisper binary content")

    # Test modified binary verification fails
    ok, msg = verify_whisper_binary(str(fake_binary))
    assert ok is False
    assert "verification failed" in msg.lower() or "expected" in msg.lower()


def test_verify_whisper_model_weight_integrity(tmp_path):
    """Test cryptographic SHA-256 verification for Whisper model weight files."""
    fake_weight = tmp_path / "custom.pt"
    fake_content = b"fake model weight weights content"
    fake_weight.write_bytes(fake_content)

    actual_hash = hashlib.sha256(fake_content).hexdigest()

    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("whisper_models", {"custom": actual_hash})

    # Test valid model weight verification
    ok, msg = verify_whisper_model_weight("custom", custom_path=str(fake_weight))
    assert ok is True

    # Tamper with the weight file
    fake_weight.write_bytes(b"corrupted weights")
    ok, msg = verify_whisper_model_weight("custom", custom_path=str(fake_weight))
    assert ok is False
    assert "cryptographic verification failed" in msg.lower()


def test_audio_extractor_halts_on_unverified_binary_or_weight(tmp_path):
    """Test that audio extraction halts and yields an error if dual verification fails."""
    dummy_audio = tmp_path / "test.wav"
    dummy_audio.touch()

    extractor = AudioExtractor()
    settings = MagicMock()
    settings.WHISPER_CMD = str(tmp_path / "non_existent_whisper")
    settings.WHISPER_MODEL_SIZE = "base"

    with patch("app.core.verifier.verify_whisper_dual", return_value=(False, "Binary hash mismatch detected")):
        mock_spawn = MagicMock()
        with patch("app.core.env_helper.spawn_background_process", mock_spawn):
            result = extractor.extract(str(dummy_audio), settings=settings)

            # Execution must halt immediately with error status alert
            assert "[STATUS:ERROR: Whisper verification failed" in result
            # Zero execution attempts permitted when verification fails
            assert mock_spawn.call_count == 0


def test_audio_extractor_passes_configured_model_size_parameter(tmp_path):
    """Test that transcription jobs pass the configured model size parameter to the subprocess."""
    dummy_audio = tmp_path / "test.wav"
    dummy_audio.touch()

    extractor = AudioExtractor()
    settings = MagicMock()
    settings.WHISPER_CMD = "whisper"
    settings.WHISPER_MODEL_SIZE = "small"
    settings.AUDIO_GPU_ENABLED = False

    mock_process = MagicMock()
    mock_process.stdout.readline.return_value = ""
    mock_process.returncode = 0

    mock_spawn = MagicMock(return_value=mock_process)

    with (
        patch("app.core.verifier.verify_whisper_dual", return_value=(True, "Dual verification passed")),
        patch("app.core.env_helper.spawn_background_process", mock_spawn),
    ):
        extractor.extract(str(dummy_audio), settings=settings)

        assert mock_spawn.call_count == 1
        cmd_args = mock_spawn.call_args[0][0]

        # Verify --model parameter is passed with user-selected model size ("small")
        assert "--model" in cmd_args
        model_idx = cmd_args.index("--model")
        assert cmd_args[model_idx + 1] == "small"


def test_whisper_download_manager_background_and_verification(tmp_path):
    """Test background download of Whisper model weights with SHA-256 verification."""
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    dm = DownloadManager.get_instance()
    dm.state["is_downloading"] = False
    dm.state["error"] = None
    dm.state["success"] = False

    model_dir = str(tmp_path / "whisper")
    os.makedirs(model_dir, exist_ok=True)

    fake_content = b"valid whisper model weight binary data"
    valid_hash = hashlib.sha256(fake_content).hexdigest()

    registry.register_expected_hashes(
        "whisper_models", {"tiny": valid_hash, "tiny.pt": valid_hash}
    )

    # Mock response for urlopen
    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = str(len(fake_content))
    mock_response.read.side_effect = [fake_content, b""]
    mock_response.__enter__.return_value = mock_response

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_response

    with patch("urllib.request.build_opener", return_value=mock_opener):
        thread = dm.start_whisper_download(
            model_size="tiny",
            model_dir=model_dir,
            url="http://fake.url/tiny.pt",
        )
        thread.join(timeout=5.0)

        assert dm.state["success"] is True
        assert os.path.exists(os.path.join(model_dir, "tiny.pt"))
