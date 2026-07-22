import os
import socket
import sys
import urllib.error
from unittest.mock import MagicMock, patch

# Add scripts to path so we can import it
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.validate_links import URL_REGEX, validate_url


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
    assert is_critical is False


def test_bypass_domain():
    success, msg, is_critical = validate_url(
        "http://bypassed.com/some/path", {"bypassed.com"}
    )
    assert success is True
    assert "Bypassed (bypassed.com)" in msg
    assert is_critical is False
