#!/usr/bin/env python3
"""Script to validate external links in repository files."""

import argparse
import concurrent.futures
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

URL_REGEX = re.compile(r'https?://[^\s\'"<>]+')
_TEMPLATE_EXPR_RE = re.compile(r"\{[^{}]+\}")
TIMEOUT = 3.0
DEFAULT_BYPASS_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "proxy.example.com",
    "test-proxy",
    "initial-proxy",
    "bypassed.com",
    "smartautosorter.com",
    "docs.smartautosorter.com",
    "sortify.ai",
    "docs.sortify.ai",
    "localhost",
    "127.0.0.1",
    "host",
    "dummy",
}
_OSV_QUERY_PROBE_BODY = (
    b'{"package":{"name":"pip","ecosystem":"PyPI"},"version":"24.0"}'
)


def is_template_or_redacted_url(url: str) -> bool:
    """Return True for valid f-string URL templates or redacted credential literals."""
    if "[REDACTED]" in url:
        return True
    if "{" in url or "}" in url:
        substituted = _TEMPLATE_EXPR_RE.sub("placeholder", url)
        if "{" in substituted or "}" in substituted:
            return False
        try:
            parsed = urlparse(substituted)
            _ = parsed.port
            return parsed.scheme in ("http", "https") and bool(
                parsed.netloc and parsed.hostname
            )
        except ValueError:
            return False
    return False


def _effective_port(parsed) -> int | None:
    """Return explicit or scheme-default port for origin comparison."""
    try:
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _is_same_origin(source_url: str, target_url: str) -> bool:
    """Return True only when source_url and target_url share scheme, hostname, and port."""
    try:
        src = urlparse(source_url)
        dst = urlparse(urljoin(source_url, target_url))
        return (
            bool(src.scheme and src.hostname and dst.scheme and dst.hostname)
            and src.scheme.lower() == dst.scheme.lower()
            and src.hostname.lower() == dst.hostname.lower()
            and _effective_port(src) == _effective_port(dst)
        )
    except ValueError:
        return False


def _strip_authorization_headers(req: urllib.request.Request) -> None:
    """Remove Authorization headers from both redirected and unredirected header maps."""
    for hdr_dict in (req.headers, req.unredirected_hdrs):
        for key in list(hdr_dict.keys()):
            if key.lower() == "authorization":
                del hdr_dict[key]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler that strips Authorization headers on cross-origin redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and not _is_same_origin(
            req.full_url, redirected.full_url
        ):
            _strip_authorization_headers(redirected)
        return redirected


_OPENER_LOCK = threading.Lock()
_LAST_OPENER_PROXIES: dict[str, str] | None = None


def _urlopen(req: urllib.request.Request, timeout: float = TIMEOUT):
    """Open URL with _SafeRedirectHandler installed while delegating via urllib.request.urlopen."""
    global _LAST_OPENER_PROXIES
    current_proxies = urllib.request.getproxies()
    opener = getattr(urllib.request, "_opener", None)
    has_safe_handler = opener is not None and any(
        isinstance(h, _SafeRedirectHandler) for h in getattr(opener, "handlers", ())
    )
    if not has_safe_handler or current_proxies != _LAST_OPENER_PROXIES:
        with _OPENER_LOCK:
            opener = getattr(urllib.request, "_opener", None)
            has_safe_handler = opener is not None and any(
                isinstance(h, _SafeRedirectHandler)
                for h in getattr(opener, "handlers", ())
            )
            if not has_safe_handler or current_proxies != _LAST_OPENER_PROXIES:
                urllib.request.install_opener(
                    urllib.request.build_opener(_SafeRedirectHandler())
                )
                _LAST_OPENER_PROXIES = dict(current_proxies)
    return urllib.request.urlopen(req, timeout=timeout)


def _is_rate_limited_http_error(err: urllib.error.HTTPError) -> bool:
    """Return True when an HTTPError indicates upstream HTTP rate limiting rather than a broken link."""
    if err.code == 429:
        return True
    if err.code == 403:
        reason = str(getattr(err, "reason", "") or "").lower()
        if "rate limit" in reason:
            return True
        headers = getattr(err, "headers", None) or getattr(err, "hdrs", None)
        if headers and hasattr(headers, "items"):
            lower_headers = {str(k).lower(): str(v).strip() for k, v in headers.items()}
            if lower_headers.get("x-ratelimit-remaining") == "0":
                return True
            if lower_headers.get("ratelimit-remaining") == "0":
                return True
            if "retry-after" in lower_headers:
                return True
    return False


def validate_url(
    url: str,
    bypass_domains: set | None = None,
    local_only: bool = False,
    allow_offline: bool = False,
):
    """Validate a single URL using HEAD with a fallback to GET."""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        netloc = parsed.netloc
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as e:
        return False, f"Invalid URL structure: {e}", True

    if scheme not in ("http", "https") or not (netloc and hostname):
        return False, "Invalid URL structure", True

    bypass_set = bypass_domains or set()
    if (
        netloc in bypass_set
        or hostname in bypass_set
        or any(
            hostname == domain or hostname.endswith("." + domain)
            for domain in bypass_set
        )
    ):
        return True, f"Bypassed ({netloc})", False

    if local_only:
        return True, "OK (Local-only)", False

    if scheme == "https" and hostname == "api.osv.dev" and parsed.path in ("", "/"):
        probe_req = urllib.request.Request(
            f"{url.rstrip('/')}/v1/query",
            data=_OSV_QUERY_PROBE_BODY,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            },
        )
        try:
            with _urlopen(probe_req, timeout=TIMEOUT) as response:
                return True, f"OK (POST {response.status})", False
        except urllib.error.HTTPError as e:
            if _is_rate_limited_http_error(e):
                return False, f"HTTP Error {e.code}: {e.reason} (rate limited)", False
            return False, f"HTTP Error {e.code}: {e.reason}", True
        except urllib.error.URLError as e:
            return (
                False,
                f"Connection/Timeout Error: {e.reason}",
                not allow_offline,
            )
        except socket.timeout:
            return (
                False,
                "Connection/Timeout Error: timed out",
                not allow_offline,
            )
        except Exception as e:
            return False, f"Unexpected Error: {str(e)}", not allow_offline

    # Using a common user agent to avoid being blocked immediately
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if scheme == "https" and hostname.lower() == "api.github.com":
        req_headers["Accept"] = "application/vnd.github+json"
        req_headers["X-GitHub-Api-Version"] = "2022-11-28"
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if github_token:
            req_headers["Authorization"] = f"Bearer {github_token}"

    req = urllib.request.Request(
        url,
        method="HEAD",
        headers=req_headers,
    )

    try:
        try:
            with _urlopen(req, timeout=TIMEOUT) as response:
                return True, f"OK (HEAD {response.status})", False
        except urllib.error.HTTPError as e:
            if _is_rate_limited_http_error(e):
                return False, f"HTTP Error {e.code}: {e.reason} (rate limited)", False
            # Fallback to GET if method not allowed, forbidden, bad request, or unauthorized
            if e.code in (405, 403, 400, 401, 301, 302, 308, 307):
                try:
                    get_url = req.full_url
                    get_headers = dict(req.header_items())
                    if e.code in (301, 302, 308, 307):
                        err_headers = getattr(e, "headers", None) or getattr(
                            e, "hdrs", None
                        )
                        location = None
                        if err_headers and hasattr(err_headers, "items"):
                            for hk, hv in err_headers.items():
                                if str(hk).lower() == "location":
                                    location = str(hv).strip()
                                    break
                        if location:
                            get_url = urljoin(req.full_url, location)
                        if not location or not _is_same_origin(req.full_url, get_url):
                            get_headers = {
                                k: v
                                for k, v in get_headers.items()
                                if k.lower() != "authorization"
                            }
                    get_req = urllib.request.Request(
                        get_url,
                        method="GET",
                        headers=get_headers,
                    )
                    with _urlopen(get_req, timeout=TIMEOUT) as response:
                        return True, f"OK (GET {response.status})", False
                except urllib.error.HTTPError as e_get:
                    if _is_rate_limited_http_error(e_get):
                        return (
                            False,
                            f"HTTP Error {e_get.code}: {e_get.reason} (rate limited)",
                            False,
                        )
                    return False, f"HTTP Error {e_get.code}: {e_get.reason}", True
                except urllib.error.URLError as e_get:
                    return (
                        False,
                        f"Connection/Timeout Error: {e_get.reason}",
                        not allow_offline,
                    )
                except socket.timeout:
                    return (
                        False,
                        "Connection/Timeout Error: timed out",
                        not allow_offline,
                    )
                except Exception as e_get:
                    return False, f"Unexpected Error: {str(e_get)}", not allow_offline
            else:
                return False, f"HTTP Error {e.code}: {e.reason}", True

    except urllib.error.URLError as e:
        return False, f"Connection/Timeout Error: {e.reason}", not allow_offline
    except socket.timeout:
        return False, "Connection/Timeout Error: timed out", not allow_offline
    except Exception as e:
        return False, f"Unexpected Error: {str(e)}", not allow_offline


def get_all_target_files(extensions=(".py", ".md")):
    """Get all files with specified extensions in the repository."""
    import os

    target_files = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d
            not in (
                "venv",
                "env",
                "__pycache__",
                "node_modules",
                "site-packages",
                "build",
                "dist",
                "htmlcov",
                "tests",
            )
        ]
        for file in files:
            if any(file.endswith(ext) for ext in extensions):
                target_files.append(os.path.join(root, file))
    return target_files


def get_all_python_files():
    """Get all python files in the repository (backwards compatibility wrapper)."""
    return get_all_target_files(extensions=(".py",))


def main():
    """Parse arguments and run concurrent URL validation."""
    parser = argparse.ArgumentParser(description="Local-First Link Validator")
    parser.add_argument("--bypass", nargs="*", default=[], help="Domains to bypass")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Validate URL syntax locally without network requests",
    )
    parser.add_argument(
        "--allow-offline",
        action="store_true",
        help="Treat network connection timeouts and DNS errors as non-critical warnings",
    )
    parser.add_argument(
        "files",
        nargs="*",
        default=[],
        help="Optional staged file path(s) to validate links in.",
    )
    args = parser.parse_args()

    bypass_domains = set(args.bypass) | DEFAULT_BYPASS_DOMAINS
    urls = set()

    is_full_scan = not args.files

    if is_full_scan:
        target_files = get_all_target_files(extensions=(".py", ".md"))
    else:
        target_files = [
            f for f in args.files if f.endswith((".py", ".md")) and os.path.isfile(f)
        ]

    for file_path in target_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                try:
                    file_content = f.read()
                except UnicodeDecodeError:
                    continue
                found = URL_REGEX.findall(file_content)
                for url in found:
                    url = url.strip().rstrip(".,;)'\"`<> \n\r\t\\")
                    if url and not is_template_or_redacted_url(url):
                        urls.add(url)
        except FileNotFoundError:
            continue

    if not urls:
        print("No external links found.")
        sys.exit(0)

    print(f"Found {len(urls)} unique external links to validate.")

    has_critical_error = False

    # We use ThreadPoolExecutor to run validations concurrently for speed
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {
            executor.submit(
                validate_url, url, bypass_domains, args.local_only, args.allow_offline
            ): url
            for url in urls
        }
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                success, msg, is_critical = future.result()
                if success:
                    print(f"[\033[92mPASS\033[0m] {url} - {msg}")
                else:
                    if is_critical:
                        print(f"[\033[91mFAIL\033[0m] {url} - {msg}")
                        has_critical_error = True
                    else:
                        print(f"[\033[93mWARN\033[0m] {url} - {msg}")
            except Exception as exc:
                print(f"[\033[91mFAIL\033[0m] {url} generated an exception: {exc}")
                has_critical_error = True

    if has_critical_error:
        print(
            "\n\033[91mValidation failed due to critical link or network errors.\033[0m"
        )
        sys.exit(1)
    else:
        print(
            "\n\033[92mValidation passed successfully (any offline/timeout issues were treated as warnings).\033[0m"
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
