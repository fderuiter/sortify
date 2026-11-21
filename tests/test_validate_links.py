import os
import socket
import sys
import urllib.error
from unittest.mock import MagicMock, patch

# Add scripts to path so we can import it
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.validate_links import (
    URL_REGEX,
    get_all_python_files,
    get_all_target_files,
    main,
    validate_url,
)


def test_get_all_target_files():
    files = get_all_target_files(extensions=(".py", ".md"))
    assert len(files) > 0
    assert any(f.endswith(".py") for f in files)
    assert any(f.endswith(".md") for f in files)
    assert not any("tests/" in f or f.startswith("tests" + os.sep) for f in files)


def test_get_all_python_files():
    py_files = get_all_python_files()
    assert len(py_files) > 0
    assert all(f.endswith(".py") for f in py_files)


def test_url_extraction():
    content = """
    Check out https://astral.sh/uv/install.sh.
    Also http://example.com/
    (See https://docs.smartautosorter.com/user_guide/#system-limits)
    """
    found = URL_REGEX.findall(content)
    # The regex itself captures trailing punctuation, which we strip later in the script
    urls = [url.rstrip(".,;)") for url in found]

    assert "https://astral.sh/uv/install.sh" in urls
    assert "http://example.com/" in urls
    assert "https://docs.smartautosorter.com/user_guide/#system-limits" in urls


@patch("urllib.request.urlopen")
def test_validate_url_success_head(mock_urlopen):
    mock_response = MagicMock()
    mock_response.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_response

    success, msg, is_critical = validate_url("http://example.com", set())
    assert success is True
    assert "OK (HEAD 200)" in msg
    assert is_critical is False


@patch("urllib.request.urlopen")
def test_validate_url_success_get_fallback(mock_urlopen):
    # First HEAD request fails with 403
    error_403 = urllib.error.HTTPError("http://example.com", 403, "Forbidden", {}, None)

    # Second GET request succeeds
    mock_response_get = MagicMock()
    mock_response_get.status = 200

    mock_urlopen.side_effect = [
        error_403,
        MagicMock(__enter__=MagicMock(return_value=mock_response_get)),
    ]

    success, msg, is_critical = validate_url("http://example.com", set())
    assert success is True
    assert "OK (GET 200)" in msg
    assert is_critical is False


@patch("urllib.request.urlopen")
def test_validate_url_critical_error(mock_urlopen):
    # Return 404
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "http://example.com", 404, "Not Found", {}, None
    )

    success, msg, is_critical = validate_url("http://example.com", set())
    assert success is False
    assert "HTTP Error 404: Not Found" in msg
    assert is_critical is True


@patch("urllib.request.urlopen")
def test_validate_url_timeout_warning(mock_urlopen):
    mock_urlopen.side_effect = socket.timeout("timed out")

    success, msg, is_critical = validate_url("http://example.com", set())
    assert success is False
    assert "timed out" in msg
    assert is_critical is True


@patch("urllib.request.urlopen")
def test_validate_url_timeout_allow_offline(mock_urlopen):
    mock_urlopen.side_effect = socket.timeout("timed out")

    success, msg, is_critical = validate_url(
        "http://example.com", set(), allow_offline=True
    )
    assert success is False
    assert "timed out" in msg
    assert is_critical is False


@patch("urllib.request.urlopen")
def test_validate_url_url_error_dns_failure(mock_urlopen):
    mock_urlopen.side_effect = urllib.error.URLError("Name or service not known")

    success, msg, is_critical = validate_url("http://nonexistent.domain", set())
    assert success is False
    assert "Name or service not known" in msg
    assert is_critical is True

    success, msg, is_critical = validate_url(
        "http://nonexistent.domain", set(), allow_offline=True
    )
    assert success is False
    assert is_critical is False


def test_bypass_domain():
    success, msg, is_critical = validate_url(
        "http://bypassed.com/some/path", {"bypassed.com"}
    )
    assert success is True
    assert "Bypassed (bypassed.com)" in msg
    assert is_critical is False


@patch("urllib.request.urlopen")
def test_validate_url_local_only_valid(mock_urlopen):
    success, msg, is_critical = validate_url(
        "https://nonexistent-external-domain-12345.com/path", set(), local_only=True
    )
    assert success is True
    assert "Local-only" in msg
    assert is_critical is False
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_validate_url_local_only_invalid(mock_urlopen):
    success, msg, is_critical = validate_url(
        "invalid_url_scheme_without_netloc", set(), local_only=True
    )
    assert success is False
    assert "Invalid URL structure" in msg
    assert is_critical is True
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_main_local_only(mock_urlopen, monkeypatch, tmp_path):
    test_file = tmp_path / "test.md"
    test_file.write_text(
        "Check https://external-domain-test-xyz.org/doc for info.", encoding="utf-8"
    )

    monkeypatch.setattr(
        "scripts.validate_links.get_all_target_files",
        lambda extensions=(".py", ".md"): [str(test_file)],
    )
    monkeypatch.setattr(sys, "argv", ["validate_links.py", "--local-only"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0

    mock_urlopen.assert_not_called()


def test_validate_links_incremental_file(monkeypatch, tmp_path):
    doc1 = tmp_path / "doc1.md"
    doc1.write_text("See https://example.com", encoding="utf-8")
    doc2 = tmp_path / "doc2.md"
    doc2.write_text("See http://invalid-url-here", encoding="utf-8")

    # Pass only doc1.md
    monkeypatch.setattr(sys, "argv", ["validate_links.py", "--local-only", str(doc1)])
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0


def test_validate_links_global_config_fallback(monkeypatch, tmp_path):
    # Passing pyproject.toml forces fallback to get_all_target_files
    mock_files = [str(tmp_path / "nonexistent.md")]
    monkeypatch.setattr(
        "scripts.validate_links.get_all_target_files",
        lambda extensions=(".py", ".md"): mock_files,
    )
    monkeypatch.setattr(
        sys, "argv", ["validate_links.py", "--local-only", "pyproject.toml"]
    )
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0


@patch("urllib.request.urlopen")
def test_main_allow_offline(mock_urlopen, monkeypatch, tmp_path):
    test_file = tmp_path / "test.md"
    test_file.write_text(
        "Check https://nonexistent-domain-xyz123.org/doc for info.", encoding="utf-8"
    )

    mock_urlopen.side_effect = socket.timeout("timed out")

    monkeypatch.setattr(
        "scripts.validate_links.get_all_target_files",
        lambda extensions=(".py", ".md"): [str(test_file)],
    )
    monkeypatch.setattr(sys, "argv", ["validate_links.py", "--allow-offline"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0


@patch("urllib.request.urlopen")
def test_main_network_failure_fails(mock_urlopen, monkeypatch, tmp_path):
    test_file = tmp_path / "test.md"
    test_file.write_text(
        "Check https://nonexistent-domain-xyz123.org/doc for info.", encoding="utf-8"
    )

    mock_urlopen.side_effect = urllib.error.URLError("DNS resolution failed")

    monkeypatch.setattr(
        "scripts.validate_links.get_all_target_files",
        lambda extensions=(".py", ".md"): [str(test_file)],
    )
    monkeypatch.setattr(sys, "argv", ["validate_links.py"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 1


@patch("urllib.request.urlopen")
def test_validate_url_malformed_ipv6_fails_in_local_and_live_modes(
    mock_urlopen, monkeypatch, tmp_path
):
    bad_url = "https://[invalid-ipv6-host/docs/guide"
    for local_only in (True, False):
        ok, msg, is_crit = validate_url(
            bad_url, {"example.com", "invalid-ipv6-host"}, local_only=local_only
        )
        assert ok is False
        assert "Invalid URL structure" in msg
        assert is_crit is True
    mock_urlopen.assert_not_called()

    for ext in (".md", ".py"):
        target = tmp_path / f"sample{ext}"
        target.write_text(f'URL = "{bad_url}"\n', encoding="utf-8")
        for args in (
            ["validate_links.py", "--local-only", str(target)],
            ["validate_links.py", str(target)],
        ):
            monkeypatch.setattr(sys, "argv", args)
            try:
                main()
                raise AssertionError("Expected SystemExit(1)")
            except SystemExit as exc:
                assert exc.code == 1


def test_worker_exception_marks_critical_failure(monkeypatch, tmp_path):
    doc = tmp_path / "crash.md"
    doc.write_text("Visit https://live-test.invalid/page\n", encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated worker crash")

    monkeypatch.setattr("scripts.validate_links.validate_url", boom)
    monkeypatch.setattr(sys, "argv", ["validate_links.py", "--local-only", str(doc)])
    try:
        main()
        raise AssertionError("Expected SystemExit(1)")
    except SystemExit as exc:
        assert exc.code == 1


def test_default_bypass_domains_restricted_and_templates_skipped():
    from scripts.validate_links import (
        DEFAULT_BYPASS_DOMAINS,
        is_template_or_redacted_url,
    )

    for live_host in (
        "github.com",
        "pypi.org",
        "astral.sh",
        "docs.astral.sh",
        "osv.dev",
        "api.osv.dev",
        "download.pytorch.org",
        "huggingface.co",
        "security.huggingface.co",
        "keepachangelog.com",
        "w3.org",
        "www.w3.org",
    ):
        assert live_host not in DEFAULT_BYPASS_DOMAINS

    assert is_template_or_redacted_url("https://api.osv.dev/v1/vulns/{vuln_id.strip()}")
    assert is_template_or_redacted_url("https://[REDACTED]:[REDACTED]@")
    assert not is_template_or_redacted_url("https://[invalid-ipv6-host/docs/guide")
    assert not is_template_or_redacted_url("https://[invalid-ipv6-host/{slug}")


@patch("urllib.request.urlopen")
def test_validate_url_rate_limited_403_and_429_are_non_critical(mock_urlopen):
    """Verify HTTP 429 and rate-limited 403 responses warn without failing critically or retrying GET."""
    rate_limit_403 = urllib.error.HTTPError(
        "https://api.github.com/advisories",
        403,
        "rate limit exceeded",
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "60"},
        None,
    )
    mock_urlopen.side_effect = rate_limit_403

    success, msg, is_critical = validate_url("https://api.github.com/advisories", set())
    assert success is False
    assert "403" in msg and "rate limited" in msg
    assert is_critical is False
    assert mock_urlopen.call_count == 1

    mock_urlopen.reset_mock()
    header_only_rate_limit = urllib.error.HTTPError(
        "https://api.github.com/advisories",
        403,
        "Forbidden",
        {"X-RateLimit-Remaining": "0"},
        None,
    )
    mock_urlopen.side_effect = header_only_rate_limit

    success, msg, is_critical = validate_url("https://api.github.com/advisories", set())
    assert success is False
    assert "rate limited" in msg
    assert is_critical is False
    assert mock_urlopen.call_count == 1

    mock_urlopen.reset_mock()
    too_many_429 = urllib.error.HTTPError(
        "https://pypi.org", 429, "Too Many Requests", {"Retry-After": "30"}, None
    )
    mock_urlopen.side_effect = too_many_429

    success, msg, is_critical = validate_url("https://pypi.org", set())
    assert success is False
    assert "429" in msg and "rate limited" in msg
    assert is_critical is False
    assert mock_urlopen.call_count == 1


@patch("urllib.request.urlopen")
def test_validate_url_non_rate_limited_403_remains_critical(mock_urlopen):
    """Verify genuine 403 Forbidden without rate-limit signals still falls back to GET and fails critically."""
    err_head = urllib.error.HTTPError(
        "https://pypi.org/secret", 403, "Forbidden", {}, None
    )
    err_get = urllib.error.HTTPError(
        "https://pypi.org/secret", 403, "Forbidden", {}, None
    )
    mock_urlopen.side_effect = [err_head, err_get]

    success, msg, is_critical = validate_url("https://pypi.org/secret", set())
    assert success is False
    assert msg == "HTTP Error 403: Forbidden"
    assert is_critical is True
    assert mock_urlopen.call_count == 2


@patch("urllib.request.urlopen")
def test_main_rate_limited_url_exits_zero(mock_urlopen, monkeypatch, tmp_path):
    """Verify validate_links.py main() exits 0 with warning when an upstream API rate-limits probes."""
    test_file = tmp_path / "doc.md"
    test_file.write_text(
        "Advisory feed: https://api.github.com/advisories\n", encoding="utf-8"
    )
    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://api.github.com/advisories",
        403,
        "rate limit exceeded",
        {"X-RateLimit-Remaining": "0"},
        None,
    )
    monkeypatch.setattr(sys, "argv", ["validate_links.py", str(test_file)])
    try:
        main()
        raise AssertionError("Expected SystemExit(0)")
    except SystemExit as exc:
        assert exc.code == 0


@patch("urllib.request.urlopen")
def test_github_token_authenticates_only_https_api_github_com(
    mock_urlopen, monkeypatch
):
    """Verify GITHUB_TOKEN adds Bearer auth only to https://api.github.com requests."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_response

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_token_123")
    ok, _, _ = validate_url("https://api.github.com/advisories", set())
    assert ok is True
    req = mock_urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer ghs_test_token_123"
    assert req.get_header("Accept") == "application/vnd.github+json"

    for untrusted_url in (
        "http://api.github.com/advisories",
        "https://github.com/fderuiter/sortify",
        "https://pypi.org/project/ruff/",
    ):
        mock_urlopen.reset_mock()
        validate_url(untrusted_url, set())
        other_req = mock_urlopen.call_args[0][0]
        assert other_req.get_header("Authorization") is None, (
            f"Leaked Authorization header to {untrusted_url}"
        )

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mock_urlopen.reset_mock()
    validate_url("https://api.github.com/advisories", set())
    unauth_req = mock_urlopen.call_args[0][0]
    assert unauth_req.get_header("Authorization") is None


def test_safe_redirect_handler_strips_authorization_on_cross_origin():
    """Verify _SafeRedirectHandler strips Authorization on cross-origin redirects only."""
    import urllib.request

    from scripts.validate_links import _SafeRedirectHandler

    handler = _SafeRedirectHandler()
    origin_req = urllib.request.Request(
        "https://api.github.com/advisories",
        headers={
            "Authorization": "Bearer ghs_secret",
            "User-Agent": "TestAgent",
        },
        method="GET",
    )

    same_origin = handler.redirect_request(
        origin_req,
        None,
        302,
        "Found",
        {},
        "https://api.github.com/advisories?per_page=1",
    )
    assert same_origin is not None
    assert same_origin.get_header("Authorization") == "Bearer ghs_secret"

    for cross_origin_target in (
        "https://objects.githubusercontent.com/feed.json",
        "http://api.github.com/advisories",
        "https://api.github.com:8443/advisories",
    ):
        redirected = handler.redirect_request(
            origin_req,
            None,
            302,
            "Found",
            {},
            cross_origin_target,
        )
        assert redirected is not None
        assert redirected.get_header("Authorization") is None
        assert "Authorization" not in redirected.headers
        assert "Authorization" not in redirected.unredirected_hdrs


@patch("urllib.request.urlopen")
def test_validate_url_redirect_fallback_strips_cross_origin_authorization(
    mock_urlopen, monkeypatch
):
    """Verify manual 302 GET fallback in validate_url strips Authorization on cross-origin Location."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret_fallback")
    redirect_302 = urllib.error.HTTPError(
        "https://api.github.com/advisories",
        302,
        "Found",
        {"Location": "https://codeload.github.com/archive.zip"},
        None,
    )
    ok_response = MagicMock()
    ok_response.status = 200
    ok_cm = MagicMock()
    ok_cm.__enter__.return_value = ok_response
    mock_urlopen.side_effect = [redirect_302, ok_cm]

    ok, _, is_crit = validate_url("https://api.github.com/advisories", set())
    assert ok is True
    assert is_crit is False
    assert mock_urlopen.call_count == 2

    head_req = mock_urlopen.call_args_list[0][0][0]
    get_req = mock_urlopen.call_args_list[1][0][0]
    assert head_req.get_header("Authorization") == "Bearer ghs_secret_fallback"
    assert get_req.get_header("Authorization") is None


def test_urlopen_cross_origin_redirect_end_to_end_strips_auth():
    """Verify end-to-end opener strips Authorization across loopback port redirects."""
    import http.server
    import threading
    import urllib.request

    from scripts.validate_links import _urlopen

    received_auth: list[str | None] = []

    class TargetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received_auth.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            return

    target_server = http.server.HTTPServer(("127.0.0.1", 0), TargetHandler)
    target_port = target_server.server_address[1]

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target_port}/dest")
            self.end_headers()

        def log_message(self, *_args):
            return

    redirect_server = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_port = redirect_server.server_address[1]

    t1 = threading.Thread(target=target_server.serve_forever, daemon=True)
    t2 = threading.Thread(target=redirect_server.serve_forever, daemon=True)
    t1.start()
    t2.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{redirect_port}/start",
            headers={"Authorization": "Bearer super_secret_token"},
            method="GET",
        )
        with _urlopen(req) as resp:
            assert resp.status == 200
        assert received_auth == [None]
    finally:
        redirect_server.shutdown()
        target_server.shutdown()
        redirect_server.server_close()
        target_server.server_close()
