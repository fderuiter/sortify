import hashlib
import os
import shutil
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import (
    DEFAULT_MODEL_URL,
    DiskSpaceError,
    DownloadCancelledError,
    DownloadError,
    DownloadManager,
    DownloadState,
    InvalidStateTransitionError,
    ModelVerificationError,
    _validate_url,
    run_background_download,
    verify_downloaded_model,
    verify_temp_file_hash,
)
from app.core.shared_registry import SharedModelRegistry


@pytest.fixture
def temp_model_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


@pytest.fixture(autouse=True)
def mock_security_advisory():
    with patch(
        "app.core.security_advisory.SecurityAdvisoryClient.check_advisory"
    ) as mock_check:
        from app.core.security_advisory import SecurityAdvisoryResult

        mock_check.return_value = SecurityAdvisoryResult(
            has_advisory=False, details="Clean mock advisory for downloader tests"
        )
        yield mock_check


def test_verify_downloaded_model(temp_model_dir):
    bundle_payloads = {
        "model.onnx": b"dummy onnx content",
        "config.json": b'{"model_type": "bert", "hidden_size": 384}',
        "tokenizer.json": b'{"version": "1.0"}',
        "tokenizer_config.json": b'{"do_lower_case": true}',
        "special_tokens_map.json": b'{"cls_token": "[CLS]"}',
        "vocab.txt": b"[PAD]\n[UNK]\n[CLS]\n[SEP]\n",
        "version.txt": b"1.0.0",
    }
    expected_hashes = {
        fname: hashlib.sha256(content).hexdigest()
        for fname, content in bundle_payloads.items()
    }
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", expected_hashes
    )

    # Empty dir -> False
    assert not verify_downloaded_model(temp_model_dir)

    # Only model.onnx + config.json without tokenizer files -> False
    with open(os.path.join(temp_model_dir, "model.onnx"), "wb") as f:
        f.write(bundle_payloads["model.onnx"])
    with open(os.path.join(temp_model_dir, "config.json"), "wb") as f:
        f.write(bundle_payloads["config.json"])
    assert not verify_downloaded_model(temp_model_dir)

    # Full bundle with matching SHA-256 digests -> True
    for fname, content in bundle_payloads.items():
        with open(os.path.join(temp_model_dir, fname), "wb") as f:
            f.write(content)
    assert verify_downloaded_model(temp_model_dir)

    # Missing vocab.txt -> False
    os.remove(os.path.join(temp_model_dir, "vocab.txt"))
    assert not verify_downloaded_model(temp_model_dir)

    # Restore vocab.txt, remove tokenizer_config.json -> False
    with open(os.path.join(temp_model_dir, "vocab.txt"), "wb") as f:
        f.write(bundle_payloads["vocab.txt"])
    os.remove(os.path.join(temp_model_dir, "tokenizer_config.json"))
    assert not verify_downloaded_model(temp_model_dir)

    # Restore tokenizer_config.json with tampered bytes -> False
    with open(os.path.join(temp_model_dir, "tokenizer_config.json"), "wb") as f:
        f.write(b'{"tampered": true}')
    assert not verify_downloaded_model(temp_model_dir)


@pytest.mark.parametrize(
    "invalid_url, expected_match",
    [
        ("file:///etc/passwd", "Unsupported URL scheme 'file'"),
        ("file:///model.onnx", "Unsupported URL scheme 'file'"),
        (
            "http://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
            "Unsupported URL scheme 'http'",
        ),
        ("http://example.com/model.onnx", "Unsupported URL scheme 'http'"),
        ("ftp://huggingface.co/model.onnx", "Unsupported URL scheme 'ftp'"),
    ],
)
def test_downloader_rejects_non_https_schemes(invalid_url, expected_match):
    """Verify that file, http, and other non-HTTPS schemes are rejected."""
    with pytest.raises(DownloadError, match=expected_match):
        _validate_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_url, expected_match",
    [
        (
            "https://127.0.0.1/model.onnx",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://127.0.0.2/model.onnx",
            "Private or local IP addresses are not permitted",
        ),
        ("https://[::1]/model.onnx", "Private or local IP addresses are not permitted"),
        (
            "https://169.254.169.254/latest/meta-data",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://10.0.0.1/model.onnx",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://192.168.1.1/model.onnx",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://172.16.0.1/model.onnx",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://0.0.0.0/model.onnx",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://localhost/model.onnx",
            "Local or loopback hostnames are not permitted",
        ),
        (
            "https://myhost.local/model.onnx",
            "Local or loopback hostnames are not permitted",
        ),
        (
            "https://app.localhost/model.onnx",
            "Local or loopback hostnames are not permitted",
        ),
    ],
)
def test_downloader_rejects_loopback_and_private_ips(invalid_url, expected_match):
    """Verify that loopback, link-local metadata, and private IP addresses are rejected."""
    with pytest.raises(DownloadError, match=expected_match):
        _validate_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://example.com/model.onnx",
        "https://evil-huggingface.co/model.onnx",
        "https://huggingface.co.attacker.com/model.onnx",
        "https://malicious.org/model.onnx",
        "https://github.com/model.onnx",
    ],
)
def test_downloader_rejects_untrusted_hostnames(invalid_url):
    """Verify that untrusted domains outside the model host allowlist are rejected."""
    with pytest.raises(DownloadError, match="Untrusted model download host"):
        _validate_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_url, expected_match",
    [
        ("", "must be a non-empty string"),
        ("   ", "must be a non-empty string"),
        ("not-a-valid-url", "Unsupported URL scheme"),
        ("https" + ":///model.onnx", "URL hostname is missing or empty"),
        ("huggingface.co/model.onnx", "Unsupported URL scheme"),
    ],
)
def test_downloader_rejects_malformed_urls(invalid_url, expected_match):
    """Verify that empty, missing scheme, or malformed URLs are rejected."""
    with pytest.raises(DownloadError, match=expected_match):
        _validate_url(invalid_url)


def test_start_download_validation_prevents_downloading_state(temp_model_dir):
    """Verify that start_download rejects invalid URLs without mutating state to DOWNLOADING."""
    dm = DownloadManager()
    dm.state["is_downloading"] = False
    dm.transition_to(DownloadState.IDLE)

    invalid_urls = [
        "file:///etc/passwd",
        "http://huggingface.co/model.onnx",
        "https://127.0.0.1/model.onnx",
        "https://169.254.169.254/latest/meta-data",
        "https://example.com/model.onnx",
    ]

    for url in invalid_urls:
        with pytest.raises(DownloadError):
            dm.start_download(url, temp_model_dir)

        assert dm.current_state == DownloadState.IDLE
        assert dm.state["is_downloading"] is False
        assert dm.current_thread is None


def test_run_background_download_validation_prevents_network_unblock(temp_model_dir):
    """Verify that run_background_download rejects invalid URLs before network unblocking."""
    from app.core.shared_registry import _thread_local

    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    _thread_local.sandboxed = True

    try:
        invalid_urls = [
            "file:///etc/passwd",
            "http://huggingface.co/model.onnx",
            "https://127.0.0.1/model.onnx",
            "https://169.254.169.254/latest/meta-data",
            "https://example.com/model.onnx",
        ]

        for url in invalid_urls:
            failure_called = threading.Event()
            success_called = threading.Event()
            captured = []

            thread = run_background_download(
                url,
                temp_model_dir,
                on_success=lambda: success_called.set(),
                on_failure=lambda err: (captured.append(err), failure_called.set()),
            )
            assert failure_called.wait(timeout=5)
            assert not success_called.is_set()
            assert len(captured) == 1
            assert isinstance(captured[0], DownloadError)
            thread.join(timeout=5)

            # Ensure no temporary or destination files were created
            assert not os.path.exists(os.path.join(temp_model_dir, "model.onnx"))
            assert not os.path.exists(os.path.join(temp_model_dir, "model.onnx.tmp"))
    finally:
        _thread_local.sandboxed = was_sandboxed


@pytest.mark.parametrize(
    "valid_url",
    [
        DEFAULT_MODEL_URL,
        "https://hf.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
        "https://cdn-lfs.huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
        "https://www.huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx",
    ],
)
def test_downloader_allowed_urls_pass_validation(valid_url):
    """Verify that trusted Hugging Face HTTPS endpoints pass validation."""
    _validate_url(valid_url)


def test_downloader_sandboxing_bypass(temp_model_dir):
    # Set thread local sandboxed to True globally to simulate sandbox mode
    from app.core.shared_registry import _thread_local

    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    _thread_local.sandboxed = True

    try:
        success_called = threading.Event()
        failure_called = threading.Event()
        captured_error = []

        def on_success():
            success_called.set()

        def on_failure(err):
            captured_error.append(err)
            failure_called.set()

        # Compute and register mock hash for the expected download to pass verification
        mock_data = b"chunk1chunk2"
        mock_hash = hashlib.sha256(mock_data).hexdigest()
        SharedModelRegistry.get_instance().register_expected_hashes(
            "model_download", {"model.onnx": mock_hash}
        )

        # We mock urllib.request.build_opener and open to return a mock response
        mock_response = MagicMock()
        mock_response.info.return_value.get.return_value = "100"
        mock_response.read.side_effect = [b"chunk1", b"chunk2", b""]

        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_response

        with (
            patch("urllib.request.build_opener", return_value=mock_opener),
            patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
        ):
            run_background_download(
                DEFAULT_MODEL_URL,
                temp_model_dir,
                progress_callback=None,
                on_success=on_success,
                on_failure=on_failure,
            )

            assert success_called.wait(timeout=15)
            assert verify_downloaded_model(temp_model_dir)
    finally:
        _thread_local.sandboxed = was_sandboxed


def test_downloader_cancellation(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    cancel_event = threading.Event()

    # Mock response to stream forever so we can cancel it
    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "100"
    mock_response.read.side_effect = lambda size: b"data"

    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_response

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
    ):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            cancel_event=cancel_event,
        )

        # Trigger cancellation immediately
        cancel_event.set()

        assert failure_called.wait(timeout=15)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], DownloadCancelledError)


def test_downloader_insufficient_disk_space(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "100000000"  # 100MB
    mock_response.read.return_value = b""

    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_response

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        # Return 10MB free space
        patch("shutil.disk_usage", return_value=(10000000, 1000000, 1000000)),
    ):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
        )

        assert failure_called.wait(timeout=15)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], DiskSpaceError)


def test_verify_temp_file_hash_success(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
    target_path = os.path.join(temp_model_dir, "model.onnx")

    # Write some dummy model content
    dummy_content = b"valid onnx content data stream"
    with open(temp_path, "wb") as f:
        f.write(dummy_content)

    # Calculate mock expected hash and register it
    expected_hash = hashlib.sha256(dummy_content).hexdigest()
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("model_download", {"model.onnx": expected_hash})

    # Call verification -> should succeed
    assert verify_temp_file_hash(temp_path, target_path) is True
    # The temp file should still exist since it passed
    assert os.path.exists(temp_path)


def test_verify_temp_file_hash_mismatch_raises_error(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
    target_path = os.path.join(temp_model_dir, "model.onnx")

    # Write some dummy model content
    dummy_content = b"corrupted content data stream"
    with open(temp_path, "wb") as f:
        f.write(dummy_content)

    # Register an incorrect hash in registry
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "model_download", {"model.onnx": "incorrect_hash_value"}
    )

    # Call verification -> should raise ModelVerificationError
    with pytest.raises(
        ModelVerificationError, match="Cryptographic signature verification failed"
    ):
        verify_temp_file_hash(temp_path, target_path)

    # Temp file must be immediately deleted/discarded from disk
    assert not os.path.exists(temp_path)


def test_verify_temp_file_hash_no_hash_registered(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "unregistered_model.bin.tmp")
    target_path = os.path.join(temp_model_dir, "unregistered_model.bin")

    with open(temp_path, "wb") as f:
        f.write(b"some model content")

    with pytest.raises(
        ModelVerificationError, match="No cryptographic hash registered"
    ):
        verify_temp_file_hash(temp_path, target_path)


def test_verify_temp_file_hash_easyocr_category(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "english_g2.pth.tmp")
    target_path = os.path.join(temp_model_dir, "english_g2.pth")

    content = b"easyocr weights content data"
    with open(temp_path, "wb") as f:
        f.write(content)

    expected_hash = hashlib.sha256(content).hexdigest()
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes("easyocr", {"english_g2.pth": expected_hash})

    assert verify_temp_file_hash(temp_path, target_path) is True
    assert os.path.exists(temp_path)


def test_verify_temp_file_hash_florence2_category(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "model.safetensors.tmp")
    target_path = os.path.join(temp_model_dir, "model.safetensors")

    content = b"florence2 safetensors content stream"
    with open(temp_path, "wb") as f:
        f.write(content)

    expected_hash = hashlib.sha256(content).hexdigest()
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "florence-2", {"model.safetensors": expected_hash}
    )

    assert verify_temp_file_hash(temp_path, target_path) is True
    assert os.path.exists(temp_path)


def test_verify_temp_file_hash_corrupted_non_generative_purged(temp_model_dir):
    temp_path = os.path.join(temp_model_dir, "craft_mlt_25k.pth.tmp")
    target_path = os.path.join(temp_model_dir, "craft_mlt_25k.pth")

    corrupted_content = b"corrupted craft model content"
    with open(temp_path, "wb") as f:
        f.write(corrupted_content)

    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "easyocr", {"craft_mlt_25k.pth": "expected_valid_sha256_hash_value"}
    )

    with pytest.raises(
        ModelVerificationError, match="Cryptographic signature verification failed"
    ):
        verify_temp_file_hash(temp_path, target_path)

    # Temporary download file MUST be purged on verification failure
    assert not os.path.exists(temp_path)


def test_downloader_fails_on_hash_mismatch(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    # Register an incorrect hash in registry to force mismatch
    registry = SharedModelRegistry.get_instance()
    registry.register_expected_hashes(
        "model_download", {"model.onnx": "some_mismatched_hash"}
    )

    # We mock urllib.request.build_opener and open to return a mock response
    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = "100"
    mock_response.read.side_effect = [b"data1", b"data2", b""]

    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_response

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
    ):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
        )

        assert failure_called.wait(timeout=15)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], ModelVerificationError)
        assert "Cryptographic signature verification failed" in str(captured_error[0])
        # Assert temp file is gone
        temp_path = os.path.join(temp_model_dir, "model.onnx.tmp")
        assert not os.path.exists(temp_path)


def test_downloader_aborts_on_placeholder_proxy(temp_model_dir):
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    from app.core.downloader import NetworkError

    run_background_download(
        DEFAULT_MODEL_URL,
        temp_model_dir,
        proxy="<DECRYPTION_FAILED>",
        progress_callback=None,
        on_success=on_success,
        on_failure=on_failure,
    )

    assert failure_called.wait(timeout=15)
    assert len(captured_error) == 1
    assert isinstance(captured_error[0], NetworkError)
    assert "Invalid proxy configuration" in str(captured_error[0])


def test_downloader_exponential_backoff_retry_success(temp_model_dir):
    import urllib.error

    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    mock_data = b"chunk1chunk2"
    mock_hash = hashlib.sha256(mock_data).hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", {"model.onnx": mock_hash}
    )

    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = str(len(mock_data))
    mock_response.read.side_effect = [b"chunk1", b"chunk2", b""]

    attempt_count = 0

    def open_side_effect(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise urllib.error.URLError("Transient network glitch")
        cm = MagicMock()
        cm.__enter__.return_value = mock_response
        return cm

    mock_opener = MagicMock()
    mock_opener.open.side_effect = open_side_effect

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
    ):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            initial_delay=0.01,
            backoff_factor=2.0,
            max_retries=3,
        )

        assert success_called.wait(timeout=15)
        assert attempt_count == 3
        assert verify_downloaded_model(temp_model_dir)


def test_downloader_exponential_backoff_retry_exhausted(temp_model_dir):
    import urllib.error

    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    attempt_count = 0

    def open_side_effect(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        raise urllib.error.URLError("Persistent connection failure")

    mock_opener = MagicMock()
    mock_opener.open.side_effect = open_side_effect

    from app.core.downloader import NetworkError

    with patch("urllib.request.build_opener", return_value=mock_opener):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            initial_delay=0.01,
            backoff_factor=2.0,
            max_retries=3,
        )

        assert failure_called.wait(timeout=15)
        # Total attempts = 1 initial + 3 retries = 4
        assert attempt_count == 4
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], NetworkError)


def test_downloader_cancellation_during_backoff_wait(temp_model_dir):
    import urllib.error

    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    cancel_event = threading.Event()

    mock_opener = MagicMock()
    mock_opener.open.side_effect = urllib.error.URLError("Transient failure")

    with patch("urllib.request.build_opener", return_value=mock_opener):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            cancel_event=cancel_event,
            initial_delay=10.0,
            max_retries=3,
        )

        # Trigger cancellation while thread is in retry wait
        cancel_event.set()

        assert failure_called.wait(timeout=5)
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], DownloadCancelledError)


def test_downloader_proxy_maintained_across_retries(temp_model_dir):
    import urllib.error

    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    mock_data = b"model_data"
    mock_hash = hashlib.sha256(mock_data).hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", {"model.onnx": mock_hash}
    )

    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = str(len(mock_data))
    mock_response.read.side_effect = [mock_data, b""]

    opener_calls = []

    def mock_get_opener(proxy=None):
        opener_calls.append(proxy)
        m_op = MagicMock()
        if len(opener_calls) == 1:
            m_op.open.side_effect = urllib.error.URLError("Network timeout")
        else:
            cm = MagicMock()
            cm.__enter__.return_value = mock_response
            m_op.open.return_value = cm
        return m_op

    mock_dm = MagicMock()
    mock_dm.get_opener.side_effect = mock_get_opener

    with patch("shutil.disk_usage", return_value=(10000, 5000, 5000)):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            proxy="http://proxy.test:8080",
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            download_manager=mock_dm,
            initial_delay=0.01,
        )

        assert success_called.wait(timeout=15)
        assert len(opener_calls) == 2
        assert opener_calls[0] == "http://proxy.test:8080"
        assert opener_calls[1] == "http://proxy.test:8080"


def test_download_state_enum_and_valid_transitions():
    """Verify DownloadState enum values and valid transitions."""
    dm = DownloadManager()
    assert dm.current_state == DownloadState.IDLE
    assert dm.state["status"] == DownloadState.IDLE
    assert dm.state["is_downloading"] is False

    # Valid path: IDLE -> DOWNLOADING -> COMPLETED -> IDLE
    dm.transition_to(DownloadState.DOWNLOADING)
    assert dm.current_state == DownloadState.DOWNLOADING
    assert dm.state["is_downloading"] is True

    dm.transition_to(DownloadState.COMPLETED)
    assert dm.current_state == DownloadState.COMPLETED
    assert dm.state["success"] is True
    assert dm.state["is_downloading"] is False

    dm.transition_to(DownloadState.IDLE)
    assert dm.current_state == DownloadState.IDLE

    # Valid path: IDLE -> DOWNLOADING -> FAILED -> DOWNLOADING -> CANCELLED -> IDLE
    dm.transition_to(DownloadState.DOWNLOADING)
    dm.transition_to(DownloadState.FAILED)
    assert dm.current_state == DownloadState.FAILED

    dm.transition_to(DownloadState.DOWNLOADING)
    dm.transition_to(DownloadState.CANCELLED)
    assert dm.current_state == DownloadState.CANCELLED

    dm.transition_to(DownloadState.IDLE)
    assert dm.current_state == DownloadState.IDLE


def test_download_state_invalid_transitions():
    """Verify that invalid transitions raise InvalidStateTransitionError / DownloadError."""
    dm = DownloadManager()
    assert dm.current_state == DownloadState.IDLE

    # From IDLE: cannot go directly to COMPLETED, FAILED, or CANCELLED
    for invalid in (
        DownloadState.COMPLETED,
        DownloadState.FAILED,
        DownloadState.CANCELLED,
    ):
        with pytest.raises((InvalidStateTransitionError, DownloadError)):
            dm.transition_to(invalid)
    assert dm.current_state == DownloadState.IDLE

    # Transition to DOWNLOADING
    dm.transition_to(DownloadState.DOWNLOADING)

    # From DOWNLOADING: cannot transition to DOWNLOADING again
    with pytest.raises((InvalidStateTransitionError, DownloadError)):
        dm.transition_to(DownloadState.DOWNLOADING)

    # Transition to CANCELLED
    dm.transition_to(DownloadState.CANCELLED)
    assert dm.current_state == DownloadState.CANCELLED

    # From CANCELLED: cannot transition to COMPLETED or FAILED
    for invalid in (DownloadState.COMPLETED, DownloadState.FAILED):
        with pytest.raises((InvalidStateTransitionError, DownloadError)):
            dm.transition_to(invalid)
    assert dm.current_state == DownloadState.CANCELLED

    # Reset to IDLE then DOWNLOADING -> COMPLETED
    dm.transition_to(DownloadState.IDLE)
    dm.transition_to(DownloadState.DOWNLOADING)
    dm.transition_to(DownloadState.COMPLETED)
    assert dm.current_state == DownloadState.COMPLETED

    # From COMPLETED: cannot transition to CANCELLED or FAILED
    for invalid in (DownloadState.CANCELLED, DownloadState.FAILED):
        with pytest.raises((InvalidStateTransitionError, DownloadError)):
            dm.transition_to(invalid)
    assert dm.current_state == DownloadState.COMPLETED


def test_download_manager_concurrent_cancellation_guards(temp_model_dir):
    """Verify that cancelled downloads ignore stale worker completion and progress events."""
    dm = DownloadManager()

    # We mock run_background_download so we can capture callbacks
    captured_callbacks = {}

    def mock_run_background_download(**kwargs):
        captured_callbacks["progress_callback"] = kwargs.get("progress_callback")
        captured_callbacks["on_success"] = kwargs.get("on_success")
        captured_callbacks["on_failure"] = kwargs.get("on_failure")
        return MagicMock()

    with patch(
        "app.core.downloader.run_background_download",
        side_effect=mock_run_background_download,
    ):
        dm.start_download(DEFAULT_MODEL_URL, temp_model_dir)

        assert dm.current_state == DownloadState.DOWNLOADING
        assert dm.state["is_downloading"] is True

        # User cancels the download
        dm.cancel_download()
        assert dm.current_state == DownloadState.CANCELLED
        assert dm.state["is_downloading"] is False
        assert dm.state["status_text"] == "Download cancelled."
        assert dm.state["success"] is False

        # Stale progress update arrives from worker thread
        from app.core.progress import ProgressUpdate

        captured_callbacks["progress_callback"](
            ProgressUpdate(
                progress=0.99, stage="Downloaded 99MB...", unit_count=99000000
            )
        )
        # Progress callback must be rejected; status_text must NOT be overwritten
        assert dm.state["status_text"] == "Download cancelled."

        # Stale success callback arrives from worker thread
        captured_callbacks["on_success"]()

        # Success must NOT overwrite cancellation!
        assert dm.current_state == DownloadState.CANCELLED
        assert dm.state["success"] is False
        assert dm.state["is_downloading"] is False
        assert dm.state["status_text"] == "Download cancelled."

        # Stale failure callback arrives from worker thread
        captured_callbacks["on_failure"](Exception("Late failure"))
        # Failure must not overwrite CANCELLED with FAILED
        assert dm.current_state == DownloadState.CANCELLED
        assert dm.state["status_text"] == "Download cancelled."


def test_run_background_download_cancellation_before_completion(temp_model_dir):
    """Verify cancellation arriving right before completion triggers cancellation, not success."""
    cancel_event = threading.Event()
    success_called = threading.Event()
    failure_called = threading.Event()
    captured_error = []

    def on_success():
        success_called.set()

    def on_failure(err):
        captured_error.append(err)
        failure_called.set()

    mock_data = b"chunk1chunk2"
    mock_hash = hashlib.sha256(mock_data).hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", {"model.onnx": mock_hash}
    )

    mock_response = MagicMock()
    mock_response.info.return_value.get.return_value = str(len(mock_data))
    mock_response.read.side_effect = [b"chunk1", b"chunk2", b""]

    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_response

    # Intercept verify_downloaded_model so that cancel_event is set right before completion check
    from app.core import downloader as dm_mod

    orig_verify = dm_mod.verify_downloaded_model

    def cancel_during_verification(model_dir):
        cancel_event.set()
        return orig_verify(model_dir)

    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("shutil.disk_usage", return_value=(10000, 5000, 5000)),
        patch(
            "app.core.downloader.verify_downloaded_model",
            side_effect=cancel_during_verification,
        ),
    ):
        run_background_download(
            DEFAULT_MODEL_URL,
            temp_model_dir,
            progress_callback=None,
            on_success=on_success,
            on_failure=on_failure,
            cancel_event=cancel_event,
        )

        assert failure_called.wait(timeout=15)
        assert not success_called.is_set()
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], DownloadCancelledError)


def test_delete_model_async_clears_cancellation_state(temp_model_dir):
    """Verify that delete_model_async clears cancellation event and resets state to IDLE."""
    dm = DownloadManager()
    dm.transition_to(DownloadState.DOWNLOADING)
    dm.cancel_download()
    assert dm.current_state == DownloadState.CANCELLED
    assert dm.cancel_event.is_set()

    done_event = threading.Event()
    callback_results = []

    def on_done(success, err):
        callback_results.append((success, err))
        done_event.set()

    dm.delete_model_async(temp_model_dir, on_done=on_done)
    assert done_event.wait(timeout=15)
    assert callback_results == [(True, None)]

    # Cancellation state must be cleared and manager must be IDLE
    assert dm.current_state == DownloadState.IDLE
    assert dm.cancel_event.is_set() is False
    assert dm.state["is_downloading"] is False
    assert dm.state["success"] is False
    assert dm.state["status_text"] == "Model deleted."


def test_default_model_url_points_to_xenova_bundle():
    """Verify DEFAULT_MODEL_URL targets Xenova/all-MiniLM-L6-v2 ONNX bundle."""
    assert (
        DEFAULT_MODEL_URL
        == "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"
    )


def test_full_xenova_bundle_staging_verification_and_atomic_activation():
    """Verify downloader fetches all Xenova bundle files into staging, verifies hashes, and atomically activates."""
    parent_dir = tempfile.mkdtemp()
    target_model_dir = os.path.join(parent_dir, "model")

    remote_payloads = {
        "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx": b"onnx-binary-weights",
        "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/config.json": b'{"model_type": "bert", "hidden_size": 384}',
        "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer.json": b'{"version": "1.0"}',
        "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer_config.json": b'{"do_lower_case": true}',
        "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/special_tokens_map.json": b'{"cls_token": "[CLS]"}',
        "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/vocab.txt": b"[PAD]\n[UNK]\n[CLS]\n[SEP]\n",
    }
    expected_hashes = {
        "model.onnx": hashlib.sha256(
            remote_payloads[
                "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"
            ]
        ).hexdigest(),
        "config.json": hashlib.sha256(
            remote_payloads[
                "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/config.json"
            ]
        ).hexdigest(),
        "tokenizer.json": hashlib.sha256(
            remote_payloads[
                "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer.json"
            ]
        ).hexdigest(),
        "tokenizer_config.json": hashlib.sha256(
            remote_payloads[
                "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer_config.json"
            ]
        ).hexdigest(),
        "special_tokens_map.json": hashlib.sha256(
            remote_payloads[
                "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/special_tokens_map.json"
            ]
        ).hexdigest(),
        "vocab.txt": hashlib.sha256(
            remote_payloads[
                "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/vocab.txt"
            ]
        ).hexdigest(),
        "version.txt": hashlib.sha256(b"1.0.0").hexdigest(),
    }
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", expected_hashes
    )

    fetched_urls: list[str] = []

    def fake_open(req, timeout=30.0):
        req_url = req.full_url if hasattr(req, "full_url") else str(req)
        fetched_urls.append(req_url)
        body = remote_payloads[req_url]
        resp = MagicMock()
        resp.info.return_value.get.return_value = str(len(body))
        resp.read.side_effect = [body, b""]
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    mock_opener = MagicMock()
    mock_opener.open.side_effect = fake_open

    success_event = threading.Event()
    failure_errors: list[Exception] = []

    try:
        with patch("urllib.request.build_opener", return_value=mock_opener):
            run_background_download(
                DEFAULT_MODEL_URL,
                target_model_dir,
                on_success=success_event.set,
                on_failure=lambda err: failure_errors.append(err),
            )
            assert success_event.wait(timeout=15), (
                f"Unexpected errors: {failure_errors}"
            )

        assert set(fetched_urls) == set(remote_payloads.keys())
        assert verify_downloaded_model(target_model_dir) is True
        for fname in expected_hashes:
            assert os.path.isfile(os.path.join(target_model_dir, fname))
        leftover = [
            entry
            for entry in os.listdir(parent_dir)
            if entry.startswith(".model_staging_")
        ]
        assert leftover == []
    finally:
        shutil.rmtree(parent_dir, ignore_errors=True)


def test_bundle_staging_cleanup_on_corrupted_tokenizer_and_cancellation():
    """Verify failed or cancelled bundle downloads delete staging files immediately and never leave partial model folders."""
    parent_dir = tempfile.mkdtemp()
    target_model_dir = os.path.join(parent_dir, "model")

    good_payload = b"good-content"
    good_hash = hashlib.sha256(good_payload).hexdigest()
    expected_hashes = {
        fname: good_hash
        for fname in (
            "model.onnx",
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.txt",
        )
    }
    expected_hashes["version.txt"] = hashlib.sha256(b"1.0.0").hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", expected_hashes
    )

    # Case 1: Tampered vocab.txt triggers ModelVerificationError and leaves no partial model_dir or staging dir
    def fake_open_corrupt_vocab(req, timeout=30.0):
        req_url = req.full_url if hasattr(req, "full_url") else str(req)
        body = b"TAMPERED-VOCAB" if req_url.endswith("/vocab.txt") else good_payload
        resp = MagicMock()
        resp.info.return_value.get.return_value = str(len(body))
        resp.read.side_effect = [body, b""]
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    mock_opener = MagicMock()
    mock_opener.open.side_effect = fake_open_corrupt_vocab
    fail_event = threading.Event()
    errors: list[Exception] = []

    try:
        with patch("urllib.request.build_opener", return_value=mock_opener):
            run_background_download(
                DEFAULT_MODEL_URL,
                target_model_dir,
                on_failure=lambda err: (errors.append(err), fail_event.set()),
            )
            assert fail_event.wait(timeout=15)
        assert isinstance(errors[0], ModelVerificationError)
        assert not os.path.exists(target_model_dir)
        assert [
            e for e in os.listdir(parent_dir) if e.startswith(".model_staging_")
        ] == []

        # Case 2: Mid-bundle cancellation deletes staging dir and never creates target_model_dir
        cancel_event = threading.Event()

        def fake_open_cancel_midway(req, timeout=30.0):
            req_url = req.full_url if hasattr(req, "full_url") else str(req)
            if req_url.endswith("/tokenizer.json"):
                cancel_event.set()
            resp = MagicMock()
            resp.info.return_value.get.return_value = str(len(good_payload))
            resp.read.side_effect = [good_payload, b""]
            cm = MagicMock()
            cm.__enter__.return_value = resp
            cm.__exit__.return_value = False
            return cm

        mock_opener_cancel = MagicMock()
        mock_opener_cancel.open.side_effect = fake_open_cancel_midway
        cancel_done = threading.Event()
        cancel_errors: list[Exception] = []

        with patch("urllib.request.build_opener", return_value=mock_opener_cancel):
            run_background_download(
                DEFAULT_MODEL_URL,
                target_model_dir,
                cancel_event=cancel_event,
                on_failure=lambda err: (cancel_errors.append(err), cancel_done.set()),
            )
            assert cancel_done.wait(timeout=15)
        assert isinstance(cancel_errors[0], DownloadCancelledError)
        assert not os.path.exists(target_model_dir)
        assert [
            e for e in os.listdir(parent_dir) if e.startswith(".model_staging_")
        ] == []
    finally:
        shutil.rmtree(parent_dir, ignore_errors=True)
