#!/usr/bin/env python3
"""Script to validate external links in repository files."""

import argparse
import concurrent.futures
import re
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

URL_REGEX = re.compile(r'https?://[^\s\'"<>]+')
TIMEOUT = 3.0


def validate_url(url: str, bypass_domains: set):
    """Validate a single URL using HEAD with a fallback to GET."""
    parsed = urlparse(url)
    if parsed.netloc in bypass_domains:
        return True, f"Bypassed ({parsed.netloc})", False

    # Using a common user agent to avoid being blocked immediately
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )

    try:
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                return True, f"OK (HEAD {response.status})", False
        except urllib.error.HTTPError as e:
            # Fallback to GET if method not allowed, forbidden, bad request, or unauthorized
            if e.code in (405, 403, 400, 401, 301, 302, 308, 307):
                try:
                    req.method = "GET"
                    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                        return True, f"OK (GET {response.status})", False
                except urllib.error.HTTPError as e_get:
                    return False, f"HTTP Error {e_get.code}: {e_get.reason}", True
                except urllib.error.URLError as e_get:
                    return False, f"Connection/Timeout Error: {e_get.reason}", False
                except socket.timeout:
                    return False, "Connection/Timeout Error: timed out", False
                except Exception as e_get:
                    return False, f"Unexpected Error: {str(e_get)}", False
            else:
                return False, f"HTTP Error {e.code}: {e.reason}", True

    except urllib.error.URLError as e:
        return False, f"Connection/Timeout Error: {e.reason}", False
    except socket.timeout:
        return False, "Connection/Timeout Error: timed out", False
    except Exception as e:
        return False, f"Unexpected Error: {str(e)}", False


def get_all_python_files():
    """Get all python files in the repository."""
    import os

    py_files = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d not in ("venv", "env", "__pycache__", "node_modules", "site-packages")
        ]
        for file in files:
            if file.endswith(".py"):
                py_files.append(os.path.join(root, file))
    return py_files


def main():
    """Parse arguments and run concurrent URL validation."""
    parser = argparse.ArgumentParser(description="Local-First Python Link Validator")
    parser.add_argument("--bypass", nargs="*", default=[], help="Domains to bypass")
    args = parser.parse_args()

    bypass_domains = set(args.bypass)
    urls = set()

    for file_path in get_all_python_files():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                try:
                    file_content = f.read()
                except UnicodeDecodeError:
                    continue
                found = URL_REGEX.findall(file_content)
                for url in found:
                    url = url.rstrip(".,;)'\"")
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
            executor.submit(validate_url, url, bypass_domains): url for url in urls
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
                print(f"[\033[93mWARN\033[0m] {url} generated an exception: {exc}")

    if has_critical_error:
        print(
            "\n\033[91mValidation failed due to critical HTTP errors (e.g. 404, 500).\033[0m"
        )
        sys.exit(1)
    else:
        print(
            "\n\033[92mValidation passed successfully (any offline/timeout issues were treated as warnings).\033[0m"
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
