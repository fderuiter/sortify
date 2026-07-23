import os
import socket
import sys
import urllib.error
from unittest.mock import MagicMock, patch

# Add scripts to path so we can import it
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.validate_links import (
    URL_REGEX,
    extract_markdown_links,
    get_all_markdown_files,
    validate_url,
)


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


# --- New Comprehensive Tests for Markdown Scanning ---


def test_extract_markdown_links_basic():
    content = """
    Check out [Uv Installation Guide](https://github.com/astral-sh/uv) and [Python](https://www.python.org/ "with title").
    We also have reference links:
    [reference]: http://example.com/ref 'Optional Title'
    """
    urls = extract_markdown_links(content)
    assert "https://github.com/astral-sh/uv" in urls
    assert "https://www.python.org/" in urls
    assert "http://example.com/ref" in urls
    assert len(urls) == 3


def test_extract_markdown_links_ignores_code_blocks():
    content = """
    Here is an active link: [Active](https://active.com).
    
    Here is a block code segment:
    ```markdown
    [Ignored 1](https://ignored1.com)
    ```
    
    Here is another block code:
    ~~~
    [Ignored 2](https://ignored2.com)
    ~~~
    
    And inline code: `[Ignored 3](https://ignored3.com)` or ``[Ignored 4](https://ignored4.com)``.
    """
    urls = extract_markdown_links(content)
    assert "https://active.com" in urls
    assert "https://ignored1.com" not in urls
    assert "https://ignored2.com" not in urls
    assert "https://ignored3.com" not in urls
    assert "https://ignored4.com" not in urls
    assert len(urls) == 1


def test_extract_markdown_links_ignores_relative_and_non_http():
    content = """
    Relative files:
    [Admin Guide](../admin_guide.md)
    [Architecture Section](architecture.md#concurrency)
    
    Non-HTTP:
    [Mail](mailto:info@example.com)
    [FTP](ftp://example.com/file)
    """
    urls = extract_markdown_links(content)
    assert len(urls) == 0


def test_get_all_markdown_files():
    # Verify that we can retrieve markdown files and that standard exclusions apply
    files = get_all_markdown_files()
    assert len(files) > 0
    for f in files:
        assert f.endswith(".md")
        # Ensure exclusions are respected
        parts = f.split(os.sep)
        assert ".git" not in parts
        assert ".github" not in parts
        assert "venv" not in parts
        assert ".venv" not in parts


@patch("urllib.request.urlopen")
def test_validate_url_429_warning(mock_urlopen):
    # If both HEAD and GET fail with 429, it should be treated as non-critical
    error_429 = urllib.error.HTTPError(
        "http://example.com", 429, "Too Many Requests", {}, None
    )
    mock_urlopen.side_effect = [error_429, error_429]

    success, msg, is_critical = validate_url("http://example.com", set())
    assert success is False
    assert "HTTP Error 429" in msg
    assert is_critical is False
