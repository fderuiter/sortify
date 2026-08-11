import hashlib
import urllib.request
from unittest.mock import patch

import pytest

from app.core.downloader import (
    DownloadValidationError,
    download_ai_models,
    download_file,
)


class MockHTTPResponse:
    """Mock HTTP Response compatible with urllib.request."""

    def __init__(self, data: bytes, status: int = 200, headers: dict = None):
        self.data = data
        self.status = status
        self.headers = headers or {}
        self.position = 0

    def read(self, amt: int = -1):
        if self.position >= len(self.data):
            return b""
        if amt < 0:
            chunk = self.data[self.position:]
            self.position = len(self.data)
            return chunk
        else:
            chunk = self.data[self.position : self.position + amt]
            self.position += len(chunk)
            return chunk

    def getcode(self):
        return self.status

    def close(self):
        pass


def test_standard_chunked_download(tmp_path):
    """Verify standard chunked download with correct hash and progress updates."""
    dest_path = tmp_path / "target.txt"
    data = b"Hello world, standard chunked download test payload!"
    expected_hash = hashlib.sha256(data).hexdigest()

    mock_resp = MockHTTPResponse(
        data, status=200, headers={"Content-Length": str(len(data))}
    )

    progress_calls = []

    def progress_callback(dl, total):
        progress_calls.append((dl, total))

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result_path = download_file(
            url="http://127.0.0.1:12345/file.txt",
            dest_path=dest_path,
            expected_sha256=expected_hash,
            chunk_size=8,
            progress_callback=progress_callback,
        )

        assert result_path == dest_path.resolve()
        assert dest_path.exists()
        assert dest_path.read_bytes() == data
        assert not dest_path.with_name("target.txt.part").exists()

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://127.0.0.1:12345/file.txt"
        assert req.get_header("User-agent") is not None

        # Verify chunked progress callback was invoked sequentially
        assert len(progress_calls) > 1
        assert progress_calls[-1] == (len(data), len(data))


def test_resumable_download_with_range(tmp_path):
    """Verify that interrupted downloads are resumed with the correct Range header."""
    dest_path = tmp_path / "resume.bin"
    part_path = tmp_path / "resume.bin.part"

    # Pre-write partial data
    partial_data = b"Hello "
    part_path.write_bytes(partial_data)

    remaining_data = b"world!"
    full_data = partial_data + remaining_data
    expected_hash = hashlib.sha256(full_data).hexdigest()

    mock_resp = MockHTTPResponse(
        remaining_data,
        status=206,
        headers={
            "Content-Range": "bytes 6-11/12",
            "Content-Length": str(len(remaining_data)),
        },
    )

    progress_calls = []

    def progress_callback(dl, total):
        progress_calls.append((dl, total))

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result_path = download_file(
            url="http://127.0.0.1:12345/resume.bin",
            dest_path=dest_path,
            expected_sha256=expected_hash,
            chunk_size=4,
            progress_callback=progress_callback,
        )

        assert result_path == dest_path.resolve()
        assert dest_path.read_bytes() == full_data
        assert not part_path.exists()

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Range") == "bytes=6-"

        assert progress_calls[-1] == (12, 12)


def test_graceful_fallback_when_range_unsupported(tmp_path):
    """Verify graceful fallback (discarding partial data) if the server does not support Range requests."""
    dest_path = tmp_path / "fallback.bin"
    part_path = tmp_path / "fallback.bin.part"

    # Stale partial data
    part_path.write_bytes(b"Stale data")

    full_payload = b"Completely fresh payload from scratch"
    expected_hash = hashlib.sha256(full_payload).hexdigest()

    # Server returns status 200 instead of 206
    mock_resp = MockHTTPResponse(
        full_payload, status=200, headers={"Content-Length": str(len(full_payload))}
    )

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result_path = download_file(
            url="http://127.0.0.1:12345/fallback.bin",
            dest_path=dest_path,
            expected_sha256=expected_hash,
        )

        assert result_path == dest_path.resolve()
        assert dest_path.read_bytes() == full_payload
        assert not part_path.exists()

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Range") == "bytes=10-"


def test_graceful_fallback_when_range_raises_error(tmp_path):
    """Verify fallback to scratch download if the range request triggers an exception."""
    dest_path = tmp_path / "error_fallback.bin"
    part_path = tmp_path / "error_fallback.bin.part"

    part_path.write_bytes(b"Stale")

    full_payload = b"Fully fresh download"
    expected_hash = hashlib.sha256(full_payload).hexdigest()

    mock_resp_full = MockHTTPResponse(
        full_payload, status=200, headers={"Content-Length": str(len(full_payload))}
    )

    # First call with Range raises URLError, second call succeeds from scratch
    call_count = 0

    def mock_urlopen_handler(req, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise urllib.error.URLError("Range not supported")
        return mock_resp_full

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        result_path = download_file(
            url="http://127.0.0.1:12345/error_fallback.bin",
            dest_path=dest_path,
            expected_sha256=expected_hash,
        )

        assert result_path == dest_path.resolve()
        assert dest_path.read_bytes() == full_payload
        assert not part_path.exists()
        assert call_count == 2


def test_invalid_checksum_raises_error(tmp_path):
    """Verify that a validation error is raised if the SHA-256 hash does not match, and partial files are removed."""
    dest_path = tmp_path / "bad_file.txt"
    part_path = tmp_path / "bad_file.txt.part"
    data = b"Corrupted data payload"

    mock_resp = MockHTTPResponse(data, status=200)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        with pytest.raises(DownloadValidationError, match="Integrity check failed"):
            download_file(
                url="http://127.0.0.1:12345/bad_file.txt",
                dest_path=dest_path,
                expected_sha256="incorrect_sha256_hash_here_for_sure",
            )

        assert not dest_path.exists()
        assert not part_path.exists()


def test_download_ai_models_flow(tmp_path):
    """Verify that download_ai_models coordinates downloading of missing models correctly."""
    from app.config import Settings

    settings = Settings()
    settings.AI_ASSISTED_NAMING = True

    # Setup mocks
    with (
        patch("app.config.get_app_dir", return_value=tmp_path / "app_dir"),
        patch("os.path.expanduser", return_value=str(tmp_path / "home")),
        patch("app.core.user_space_bootstrap.is_file_valid", return_value=False),
        patch("app.core.downloader.download_file") as mock_download_file,
    ):
        # Trigger model acquisition
        success = download_ai_models(settings)
        assert success is True

        # Expect calls to download EasyOCR and Generative naming files
        assert mock_download_file.call_count >= 3
