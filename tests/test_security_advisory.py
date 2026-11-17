"""Unit and integration test suite for Pre-Download Security Advisory Client."""

import os
import tempfile
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from app.core.downloader import run_background_download
from app.core.exceptions import ModelVerificationError
from app.core.offline_loader import OfflineModelLoader
from app.core.security_advisory import (
    ALLOWED_ADVISORY_HOSTS,
    SecurityAdvisoryClient,
    SecurityAdvisoryResult,
    _is_allowed_advisory_host,
    _validate_advisory_url,
)


@pytest.fixture(autouse=True)
def reset_advisory_client():
    """Ensure clean SecurityAdvisoryClient and sandbox thread-local state for every test."""
    from app.core.shared_registry import _thread_local

    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    was_reason = getattr(_thread_local, "reason", "worker execution")
    SecurityAdvisoryClient.reset_instance()
    try:
        yield
    finally:
        SecurityAdvisoryClient.reset_instance()
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = was_reason


def test_advisory_client_singleton_and_cache():
    """Test singleton pattern, cache storage, and TTL expiration."""
    client1 = SecurityAdvisoryClient.get_instance()
    client2 = SecurityAdvisoryClient.get_instance()
    assert client1 is client2

    # Test URL repo ID extraction
    url = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"
    extracted = SecurityAdvisoryClient.extract_repo_id(url)
    assert extracted == "sentence-transformers/all-MiniLM-L6-v2"

    # Test cache insertion and retrieval
    res = SecurityAdvisoryResult(
        has_advisory=False, details="No security advisories registered."
    )
    client1.cache_advisory("sentence-transformers/all-MiniLM-L6-v2", None, res)

    cached = client1.get_cached_advisory("sentence-transformers/all-MiniLM-L6-v2", None)
    assert cached is not None
    assert cached.has_advisory is False

    # Test clear_cache
    client1.clear_cache()
    assert (
        client1.get_cached_advisory("sentence-transformers/all-MiniLM-L6-v2", None)
        is None
    )


def test_cached_lookup_latency_under_50ms():
    """Verify that in-memory cached advisory lookups complete in under 50 milliseconds."""
    client = SecurityAdvisoryClient.get_instance()
    repo_id = "test-owner/test-model"
    res = SecurityAdvisoryResult(has_advisory=False, details="Clean")
    client.cache_advisory(repo_id, None, res)

    start_time = time.perf_counter()
    result = client.check_advisory(repo_id=repo_id, raise_on_advisory=False)
    elapsed = time.perf_counter() - start_time

    assert result.has_advisory is False
    assert elapsed < 0.050, (
        f"Cached lookup took {elapsed * 1000:.2f}ms, exceeding 50ms SLA limit!"
    )


def test_active_security_advisory_blocks_and_raises_verification_error():
    """Test that active security advisories raise ModelVerificationError."""
    client = SecurityAdvisoryClient.get_instance()
    repo_id = "flagged-vendor/vulnerable-model"

    flagged_res = SecurityAdvisoryResult(
        has_advisory=True,
        advisory_id="GHSA-xxxx-yyyy",
        severity="CRITICAL",
        details="Remote code execution vulnerability in model weights.",
    )
    client.cache_advisory(repo_id, None, flagged_res)

    with pytest.raises(ModelVerificationError) as exc_info:
        client.check_advisory(repo_id=repo_id)

    assert "Active security advisory 'GHSA-xxxx-yyyy'" in str(exc_info.value)
    assert "CRITICAL" in str(exc_info.value)


def test_app_settings_default_policy_is_strict():
    """Verify that AppSettings.SECURITY_ADVISORY_FALLBACK_POLICY defaults to 'strict'."""
    from app.config import AppSettings

    settings = AppSettings()
    assert settings.SECURITY_ADVISORY_FALLBACK_POLICY == "strict"

    client = SecurityAdvisoryClient.get_instance()
    assert client.fallback_policy == "strict"


def test_fallback_policy_strict_vs_permissive():
    """Test fallback policy behavior during network query errors or timeouts."""
    client_strict = SecurityAdvisoryClient(fallback_policy="strict")
    client_permissive = SecurityAdvisoryClient(fallback_policy="permissive")

    with patch("urllib.request.build_opener") as mock_opener_builder:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("Network unreachable")
        mock_opener_builder.return_value = mock_opener

        # Strict policy must raise ModelVerificationError
        with pytest.raises(ModelVerificationError) as exc_info:
            client_strict.check_advisory(repo_id="owner/model-a", timeout=1.0)
        assert "under strict security policy" in str(exc_info.value)

        # Permissive policy must log warning and return clean result without caching it in _cache
        res = client_permissive.check_advisory(
            repo_id="owner/model-b", timeout=1.0, raise_on_advisory=True
        )
        assert res.has_advisory is False
        assert "permissive fallback policy" in res.details

        # Ensure unverified fallback result is NOT stored in long-term cache
        assert client_permissive.get_cached_advisory("owner/model-b", None) is None
        assert len(client_permissive._cache) == 0


def test_background_downloader_sandboxing_maintained_on_advisory_failure():
    """Verify that downloader thread sandboxing remains active and un-sandboxing is skipped on advisory check failure."""
    from app.core.shared_registry import _thread_local

    test_url = "https://huggingface.co/unreachable-org/model-x/resolve/main/model.onnx"

    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    old_reason = getattr(_thread_local, "reason", "")
    _thread_local.sandboxed = True

    try:
        # Mock opener open to fail with network error
        with patch("urllib.request.build_opener") as mock_opener_builder:
            mock_opener = MagicMock()
            mock_opener.open.side_effect = urllib.error.URLError("Network unreachable")
            mock_opener_builder.return_value = mock_opener

            with tempfile.TemporaryDirectory() as temp_dir:
                model_dir = os.path.join(temp_dir, "test_model_target")

                failed_event = pytest.importorskip("threading").Event()
                captured_error = []
                sandboxed_state = []

                def on_failure_cb(err):
                    captured_error.append(err)
                    sandboxed_state.append(getattr(_thread_local, "sandboxed", False))
                    failed_event.set()

                t = run_background_download(
                    url=test_url,
                    model_dir=model_dir,
                    on_failure=on_failure_cb,
                )
                t.join(timeout=5.0)

                assert failed_event.is_set()
                assert len(captured_error) == 1
                assert isinstance(captured_error[0], ModelVerificationError)
                # Verify sandboxing was active (True) when failure callback executed
                assert sandboxed_state[0] is True
    finally:
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = old_reason


def test_offline_loader_aborts_on_network_failure_under_strict_policy():
    """Verify offline model loader aborts cleanly with ModelVerificationError on network failure under default strict policy."""
    client = SecurityAdvisoryClient.get_instance()
    model_id = "strict_offline_model"

    mock_loader = MagicMock()

    with patch("urllib.request.build_opener") as mock_opener_builder:
        mock_opener = MagicMock()
        mock_opener.open.side_effect = urllib.error.URLError("Network unreachable")
        mock_opener_builder.return_value = mock_opener

        with tempfile.TemporaryDirectory() as temp_dir:
            model_folder = os.path.join(temp_dir, "offline_bundle", model_id)
            os.makedirs(model_folder, exist_ok=True)
            with open(os.path.join(model_folder, "model.onnx"), "w") as f:
                f.write("dummy model content")

            with patch.object(
                OfflineModelLoader, "resolve_model_path", return_value=model_folder
            ):
                with pytest.raises(ModelVerificationError) as exc_info:
                    OfflineModelLoader.load_model(model_id, mock_loader)

                assert "under strict security policy" in str(exc_info.value)
                mock_loader.assert_not_called()


def test_background_downloader_pre_download_advisory_check_blocks_download():
    """Verify background downloader checks advisories and halts before stream opening or disk allocation."""
    client = SecurityAdvisoryClient.get_instance()
    test_url = (
        "https://huggingface.co/vulnerable-org/malicious-model/resolve/main/model.onnx"
    )
    repo_id = SecurityAdvisoryClient.extract_repo_id(test_url)

    # Flag repository as vulnerable in advisory client
    client.cache_advisory(
        repo_id,
        None,
        SecurityAdvisoryResult(
            has_advisory=True,
            advisory_id="GHSA-MALICIOUS-01",
            severity="HIGH",
            details="Backdoored model weights.",
        ),
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "test_model_target")

        failed_event = pytest.importorskip("threading").Event()
        captured_error = []

        def on_failure_cb(err):
            captured_error.append(err)
            failed_event.set()

        t = run_background_download(
            url=test_url,
            model_dir=model_dir,
            on_failure=on_failure_cb,
        )
        t.join(timeout=5.0)

        assert failed_event.is_set()
        assert len(captured_error) == 1
        assert isinstance(captured_error[0], ModelVerificationError)
        assert "GHSA-MALICIOUS-01" in str(captured_error[0])

        # Verify no files or directories were created for the download
        assert not os.path.exists(os.path.join(model_dir, "model.onnx.tmp"))
        assert not os.path.exists(os.path.join(model_dir, "model.onnx"))


def test_offline_loader_blocks_loading_flagged_model():
    """Verify offline model loader checks cached advisory records and blocks deserialization."""
    client = SecurityAdvisoryClient.get_instance()
    model_id = "flagged_offline_model"

    # Flag model in advisory client
    client.cache_advisory(
        model_id,
        None,
        SecurityAdvisoryResult(
            has_advisory=True,
            advisory_id="GHSA-OFFLINE-BLOCK",
            severity="CRITICAL",
            details="Known compromised offline asset.",
        ),
    )

    mock_loader = MagicMock()

    with tempfile.TemporaryDirectory() as temp_dir:
        model_folder = os.path.join(temp_dir, "offline_bundle", model_id)
        os.makedirs(model_folder, exist_ok=True)
        # Touch dummy file
        with open(os.path.join(model_folder, "model.onnx"), "w") as f:
            f.write("dummy model content")

        with patch.object(
            OfflineModelLoader, "resolve_model_path", return_value=model_folder
        ):
            with pytest.raises(ModelVerificationError) as exc_info:
                OfflineModelLoader.load_model(model_id, mock_loader)

            assert "GHSA-OFFLINE-BLOCK" in str(exc_info.value)
            # Ensure model loader function was NEVER invoked!
            mock_loader.assert_not_called()


def test_concurrent_advisory_cache_thread_safety():
    """Verify in-memory advisory caching supports thread safety across concurrent threads."""
    client = SecurityAdvisoryClient.get_instance()
    import threading

    errors = []

    def worker(idx):
        try:
            repo = f"org/model-{idx % 5}"
            res = SecurityAdvisoryResult(has_advisory=False, details=f"Thread {idx}")
            client.cache_advisory(repo, None, res)

            retrieved = client.get_cached_advisory(repo, None)
            assert retrieved is not None
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Thread safety errors encountered: {errors}"


def test_non_json_response_handling_permissive_and_strict():
    """Verify non-JSON responses from advisory endpoint return fallback result in permissive mode and raise in strict mode."""
    client_permissive = SecurityAdvisoryClient(fallback_policy="permissive")
    client_strict = SecurityAdvisoryClient(fallback_policy="strict")

    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html>502 Bad Gateway</html>"
    mock_opener = MagicMock()
    mock_opener.open.return_value.__enter__.return_value = mock_resp

    with patch.object(client_permissive, "_build_opener", return_value=mock_opener):
        res = client_permissive.check_advisory(repo_id="test-org/non-json-model")
        assert res.has_advisory is False
        assert "Query failed" in res.details
        assert "permissive fallback policy" in res.details

    with patch.object(client_strict, "_build_opener", return_value=mock_opener):
        with pytest.raises(ModelVerificationError) as exc_info:
            client_strict.check_advisory(repo_id="test-org/non-json-model-strict")
        assert "Pre-download security advisory lookup failed" in str(exc_info.value)


def test_sandbox_state_restoration_during_advisory_check():
    """Verify thread-local sandbox state is cleanly restored after check_advisory under success and failure."""
    from app.core.shared_registry import _thread_local

    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    was_reason = getattr(_thread_local, "reason", "")

    try:
        _thread_local.sandboxed = True
        _thread_local.reason = "pre-existing restricted sandbox"

        client_permissive = SecurityAdvisoryClient(fallback_policy="permissive")
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"has_advisory": false}'
        mock_opener = MagicMock()
        mock_opener.open.return_value.__enter__.return_value = mock_resp

        with patch.object(client_permissive, "_build_opener", return_value=mock_opener):
            res = client_permissive.check_advisory(
                repo_id="test-org/sandbox-restoration-test"
            )
            assert res.has_advisory is False

        # Assert flags were restored
        assert getattr(_thread_local, "sandboxed", False) is True
        assert getattr(_thread_local, "reason", "") == "pre-existing restricted sandbox"

        client_strict = SecurityAdvisoryClient(fallback_policy="strict")
        mock_err_opener = MagicMock()
        mock_err_opener.open.side_effect = urllib.error.URLError("Connection refused")

        with patch.object(client_strict, "_build_opener", return_value=mock_err_opener):
            with pytest.raises(ModelVerificationError):
                client_strict.check_advisory(
                    repo_id="test-org/sandbox-restoration-error-test"
                )

        # Assert flags were restored even after exception
        assert getattr(_thread_local, "sandboxed", False) is True
        assert getattr(_thread_local, "reason", "") == "pre-existing restricted sandbox"

    finally:
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = was_reason


def test_background_downloader_strict_advisory_failure_triggers_on_failure():
    """Verify background downloader receives ModelVerificationError when advisory check fails under strict policy."""
    client_strict = SecurityAdvisoryClient.get_instance()
    client_strict._fallback_policy = "strict"

    mock_err_opener = MagicMock()
    mock_err_opener.open.side_effect = urllib.error.URLError("Advisory service down")

    failed_event = pytest.importorskip("threading").Event()
    captured_error = []

    def on_failure_cb(err):
        captured_error.append(err)
        failed_event.set()

    with tempfile.TemporaryDirectory() as temp_dir:
        model_dir = os.path.join(temp_dir, "strict_test_model")
        with patch.object(client_strict, "_build_opener", return_value=mock_err_opener):
            t = run_background_download(
                url="https://huggingface.co/org/strict-model/resolve/main/model.onnx",
                model_dir=model_dir,
                on_failure=on_failure_cb,
            )
            t.join(timeout=5.0)

    assert failed_event.is_set()
    assert len(captured_error) == 1
    assert isinstance(captured_error[0], ModelVerificationError)
    assert "strict security policy" in str(captured_error[0])


def test_allowed_advisory_hosts_contains_defaults():
    """Verify ALLOWED_ADVISORY_HOSTS constant contains expected domains."""
    assert "huggingface.co" in ALLOWED_ADVISORY_HOSTS
    assert "hf.co" in ALLOWED_ADVISORY_HOSTS


@pytest.mark.parametrize(
    "hostname, expected",
    [
        ("huggingface.co", True),
        ("hf.co", True),
        ("security.huggingface.co", True),
        ("api.hf.co", True),
        ("sub.domain.huggingface.co", True),
        ("security.huggingface.co.", True),
        ("example.com", False),
        ("evil-huggingface.co", False),
        ("huggingface.co.attacker.com", False),
        ("", False),
        (".huggingface.co", False),
        ("127.0.0.1", False),
    ],
)
def test_is_allowed_advisory_host(hostname, expected):
    """Verify allowed advisory host checking for domains and subdomains."""
    assert _is_allowed_advisory_host(hostname) is expected


@pytest.mark.parametrize(
    "invalid_url, expected_match",
    [
        ("file:///etc/passwd", "Unsupported URL scheme 'file'"),
        ("file:///advisories.json", "Unsupported URL scheme 'file'"),
        (
            "http://security.huggingface.co/api/advisories",
            "Unsupported URL scheme 'http'",
        ),
        ("http://hf.co/api/advisories", "Unsupported URL scheme 'http'"),
        ("ftp://security.huggingface.co/api", "Unsupported URL scheme 'ftp'"),
        ("gopher://security.huggingface.co/", "Unsupported URL scheme 'gopher'"),
    ],
)
def test_advisory_rejects_non_https_schemes(invalid_url, expected_match):
    """Verify that file, http, and other non-HTTPS schemes are rejected."""
    with pytest.raises(ModelVerificationError, match=expected_match):
        _validate_advisory_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_url, expected_match",
    [
        (
            "https://127.0.0.1/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://127.0.0.2/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://[::1]/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://169.254.169.254/latest/meta-data",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://10.0.0.1/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://192.168.1.1/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://172.16.0.1/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://0.0.0.0/api/advisories",
            "Private or local IP addresses are not permitted",
        ),
        (
            "https://93.184.216.34/api/advisories",
            "IP address literals are not permitted as security advisory hosts",
        ),
        (
            "https://localhost/api/advisories",
            "Local or loopback hostnames are not permitted",
        ),
        (
            "https://myhost.local/api/advisories",
            "Local or loopback hostnames are not permitted",
        ),
        (
            "https://app.localhost/api/advisories",
            "Local or loopback hostnames are not permitted",
        ),
    ],
)
def test_advisory_rejects_loopback_and_private_ips(invalid_url, expected_match):
    """Verify that loopback, link-local metadata, and private IP addresses are rejected."""
    with pytest.raises(ModelVerificationError, match=expected_match):
        _validate_advisory_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_url",
    [
        "https://example.com/api/advisories",
        "https://evil-huggingface.co/api/advisories",
        "https://huggingface.co.attacker.com/api/advisories",
        "https://malicious.org/api/advisories",
        "https://github.com/api/advisories",
    ],
)
def test_advisory_rejects_untrusted_hostnames(invalid_url):
    """Verify that untrusted domains outside the advisory host allowlist are rejected."""
    with pytest.raises(
        ModelVerificationError, match="Untrusted security advisory host"
    ):
        _validate_advisory_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_url, expected_match",
    [
        ("", "must be a non-empty string"),
        ("   ", "must be a non-empty string"),
        ("not-a-valid-url", "Unsupported URL scheme"),
        ("https:///api/advisories", "URL hostname is missing or empty"),
        ("security.huggingface.co/api", "Unsupported URL scheme"),
    ],
)
def test_advisory_rejects_malformed_urls(invalid_url, expected_match):
    """Verify that empty, missing scheme, or malformed advisory URLs are rejected."""
    with pytest.raises(ModelVerificationError, match=expected_match):
        _validate_advisory_url(invalid_url)


@pytest.mark.parametrize(
    "invalid_endpoint",
    [
        "file:///etc/passwd",
        "http://security.huggingface.co/api",
        "https://127.0.0.1/api",
        "https://evil.com/advisories",
        "",
    ],
)
def test_advisory_client_init_fails_fast_on_invalid_endpoint(invalid_endpoint):
    """Verify SecurityAdvisoryClient.__init__ fails fast with ModelVerificationError."""
    with pytest.raises(ModelVerificationError):
        SecurityAdvisoryClient(endpoint=invalid_endpoint)


@pytest.mark.parametrize(
    "invalid_endpoint",
    [
        "file:///etc/passwd",
        "http://security.huggingface.co/api",
        "https://127.0.0.1/api",
        "https://169.254.169.254/api",
        "https://evil.com/api",
    ],
)
def test_check_advisory_rejects_before_socket_unblocking(invalid_endpoint):
    """Verify check_advisory rejects invalid endpoints before socket sandboxing is unblocked."""
    from app.core.shared_registry import _thread_local

    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    was_reason = getattr(_thread_local, "reason", "")
    _thread_local.sandboxed = True
    _thread_local.reason = "sandbox safety test"

    try:
        # Construct client bypassing __init__ check to test check_advisory defense-in-depth
        client = SecurityAdvisoryClient.get_instance()
        client._endpoint = invalid_endpoint

        with patch("app.core.network_guard.unblock_external_network") as mock_unblock:
            with pytest.raises(ModelVerificationError):
                client.check_advisory(repo_id="test-org/test-model")

            # Crucially, unblock_external_network was NEVER invoked
            mock_unblock.assert_not_called()

        # Sandboxing state remained completely active throughout
        assert getattr(_thread_local, "sandboxed", False) is True
        assert getattr(_thread_local, "reason", "") == "sandbox safety test"
    finally:
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = was_reason


def test_check_advisory_cached_result_bypasses_url_validation():
    """Verify queries with cached results return immediately without redundant URL parsing."""
    client = SecurityAdvisoryClient.get_instance()
    # Set an invalid endpoint that would fail validation if parsed
    client._endpoint = "https://invalid.untrusted.host.com/api"

    repo_id = "cached-org/cached-model"
    res = SecurityAdvisoryResult(has_advisory=False, details="Cached valid result")
    client.cache_advisory(repo_id, None, res)

    # Calling check_advisory with a cache hit returns immediately without failing
    retrieved = client.check_advisory(repo_id=repo_id)
    assert retrieved is res
    assert retrieved.details == "Cached valid result"
