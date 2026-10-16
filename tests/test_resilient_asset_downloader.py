"""Unit tests for standalone resilient asset download helper in scripts/build.py."""

import io
import socket
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.build import download_file


def test_download_file_success(tmp_path):
    """Test that download_file successfully fetches an asset and writes it to disk using stdlib."""
    dest_path = tmp_path / "model" / "weights.bin"
    mock_response = MagicMock()
    mock_response.__enter__.return_value = io.BytesIO(b"model-weights-binary-data")
    mock_response.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        download_file("https://example.com/weights.bin", dest_path, timeout=12.5)

        assert dest_path.exists()
        assert dest_path.read_bytes() == b"model-weights-binary-data"
        assert mock_urlopen.call_count == 1
        req_arg = mock_urlopen.call_args[0][0]
        assert req_arg.full_url == "https://example.com/weights.bin"
        assert mock_urlopen.call_args[1].get("timeout") == 12.5


def test_download_file_enforces_socket_timeout(tmp_path):
    """Test that download_file passes explicit socket timeout to urllib.request.urlopen."""
    dest_path = tmp_path / "asset.json"
    mock_response = MagicMock()
    mock_response.__enter__.return_value = io.BytesIO(b'{"key": "value"}')
    mock_response.__exit__.return_value = False

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        download_file("https://example.com/asset.json", dest_path, timeout=45.0)
        assert mock_urlopen.call_args[1].get("timeout") == 45.0


def test_download_file_retries_transient_failures(tmp_path):
    """Test that transient socket errors trigger retry attempts with exponential backoff and succeed."""
    dest_path = tmp_path / "model.onnx"
    mock_response = MagicMock()
    mock_response.__enter__.return_value = io.BytesIO(b"onnx-model-data")
    mock_response.__exit__.return_value = False

    # First attempt fails with socket.timeout, second attempt succeeds
    side_effects = [socket.timeout("Socket read timed out"), mock_response]

    with (
        patch("urllib.request.urlopen", side_effect=side_effects) as mock_urlopen,
        patch("time.sleep") as mock_sleep,
    ):
        download_file(
            "https://example.com/model.onnx",
            dest_path,
            timeout=10.0,
            max_retries=3,
            backoff_factor=1.5,
        )

        assert dest_path.exists()
        assert dest_path.read_bytes() == b"onnx-model-data"
        assert mock_urlopen.call_count == 2
        # Backoff delay for attempt 2: backoff_factor * (2 ** (2 - 2)) = 1.5 * 1 = 1.5s
        mock_sleep.assert_called_once_with(1.5)


def test_download_file_cleans_up_partial_file_on_failure(tmp_path):
    """Test that interrupted asset downloads leave no partial or zero-byte files on disk."""
    dest_path = tmp_path / "corrupted_asset.bin"

    def faulty_copy(src, dst):
        # Write partial data before failing
        dst.write(b"partial byte data")
        dst.flush()
        raise socket.timeout("Connection reset mid-stream")

    mock_response = MagicMock()
    mock_response.__enter__.return_value = io.BytesIO(b"full content")
    mock_response.__exit__.return_value = False

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        patch("shutil.copyfileobj", side_effect=faulty_copy),
        patch("time.sleep"),
    ):
        with pytest.raises(Exception) as exc_info:
            download_file(
                "https://example.com/corrupted_asset.bin",
                dest_path,
                max_retries=2,
                backoff_factor=0.01,
            )

        assert "Connection reset mid-stream" in str(exc_info.value)
        # Verify NO partial file remains on disk after interruption
        assert not dest_path.exists()


def test_download_file_exhausts_retries_logs_failure_and_raises(tmp_path, capsys):
    """Test that failure details are logged to console output when all retries are exhausted."""
    dest_path = tmp_path / "failed_download.bin"
    error = urllib.error.URLError("503 Service Unavailable")

    with (
        patch("urllib.request.urlopen", side_effect=error) as mock_urlopen,
        patch("time.sleep") as mock_sleep,
    ):
        with pytest.raises(urllib.error.URLError):
            download_file(
                "https://example.com/failed_download.bin",
                dest_path,
                timeout=5.0,
                max_retries=3,
                backoff_factor=2.0,
            )

        assert mock_urlopen.call_count == 3
        # Sleep called for attempts 2 and 3
        assert mock_sleep.call_args_list == [call(2.0), call(4.0)]
        assert not dest_path.exists()

        captured = capsys.readouterr()
        assert "Download attempt 1/3 failed" in captured.out
        assert "Download attempt 2/3 failed" in captured.out
        assert "Download attempt 3/3 failed" in captured.out
        assert "Failed to download asset" in captured.out


def test_download_file_stdlib_only():
    """Verify download_file relies strictly on standard library modules without core app dependencies."""
    import inspect

    import scripts.build as build_module

    source = inspect.getsource(build_module.download_file)

    # Must not import app.core or external HTTP libs
    forbidden_terms = [
        "app.core",
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
    ]
    for term in forbidden_terms:
        assert term not in source, (
            f"Found forbidden import/term '{term}' in download_file"
        )
