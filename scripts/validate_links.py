#!/usr/bin/env python3
"""Script to validate external Markdown links in user-facing documentation and policy files."""

import argparse
import concurrent.futures
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

# Keep URL_REGEX to avoid breaking imports in other scripts (e.g., generate_docs.py)
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
            # Fallback to GET if method not allowed, forbidden, bad request, unauthorized, or too many requests
            if e.code in (405, 403, 400, 401, 301, 302, 308, 307, 429):
                try:
                    req.method = "GET"
                    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                        return True, f"OK (GET {response.status})", False
                except urllib.error.HTTPError as e_get:
                    # Treat 429 (Too Many Requests), 401, or 403 as non-critical to prevent false positives from rate-limiting/scraping protection
                    is_critical = e_get.code not in (429, 401, 403)
                    return (
                        False,
                        f"HTTP Error {e_get.code}: {e_get.reason}",
                        is_critical,
                    )
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


def get_all_markdown_files(base_dir: str = ".") -> list:
    """Get all user-facing and policy markdown files in the repository."""
    md_files = []
    excluded_dirs = {
        ".git",
        "venv",
        ".venv",
        "env",
        "__pycache__",
        "node_modules",
        "site-packages",
        "build",
        "dist",
        ".github",
        ".pytest_cache",
    }

    for root, dirs, files in os.walk(base_dir):
        # Exclude directories in-place to prevent walking into them
        dirs[:] = [d for d in dirs if d not in excluded_dirs]

        rel_root = os.path.relpath(root, base_dir)
        is_root = rel_root == "."
        parts = rel_root.split(os.sep)
        is_docs = parts[0] == "docs"

        if is_root or is_docs:
            for file in files:
                if file.endswith(".md"):
                    file_path = os.path.normpath(os.path.join(root, file))
                    # Defensive check to make absolutely sure no excluded part is in path
                    if any(part in excluded_dirs for part in file_path.split(os.sep)):
                        continue
                    md_files.append(file_path)

    return md_files


def extract_markdown_links(content: str) -> list:
    """Extract explicit Markdown-formatted HTTP/HTTPS links, ignoring code blocks and relative links."""
    # 1. Strip Code Blocks
    # Block Code
    content = re.sub(r"```[\s\S]*?```", "", content)
    content = re.sub(r"~~~[\s\S]*?~~~", "", content)
    # Inline Code (Double backticks first, then single)
    content = re.sub(r"``[\s\S]*?``", "", content)
    content = re.sub(r"`[^`]*?`", "", content)

    urls = []

    # 2. Extract Inline Links: [text](url)
    inline_pattern = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
    for match in inline_pattern.finditer(content):
        link_content = match.group(2).strip()
        # Split by whitespace to separate the URL from optional titles
        parts = link_content.split()
        if parts:
            url = parts[0].strip()
            # Strip potential surrounding quotes or parenthesis
            url = url.strip("'\"()")
            url = url.rstrip(".,;)'\"")
            if url.startswith("http://") or url.startswith("https://"):
                urls.append(url)

    # 3. Extract Reference Links: [label]: url
    ref_pattern = re.compile(r"\[([^\]]+)\]:\s*([^\n]+)")
    for match in ref_pattern.finditer(content):
        link_content = match.group(2).strip()
        # Split by whitespace to separate the URL from optional titles
        parts = link_content.split()
        if parts:
            url = parts[0].strip()
            url = url.strip("'\"()")
            url = url.rstrip(".,;)'\"")
            if url.startswith("http://") or url.startswith("https://"):
                urls.append(url)

    return urls


def main():
    """Parse arguments and run concurrent URL validation for markdown files."""
    parser = argparse.ArgumentParser(
        description="Comprehensive Markdown Link Validator"
    )
    parser.add_argument("--bypass", nargs="*", default=[], help="Domains to bypass")
    args = parser.parse_args()

    bypass_domains = set(args.bypass)
    url_to_files = {}

    markdown_files = get_all_markdown_files()
    for file_path in markdown_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, FileNotFoundError):
            continue

        extracted_urls = extract_markdown_links(content)
        for url in extracted_urls:
            url_to_files.setdefault(url, set()).add(file_path)

    if not url_to_files:
        print("No external Markdown links found to validate.")
        sys.exit(0)

    print(
        f"Found {len(url_to_files)} unique external Markdown links to validate across {len(markdown_files)} files."
    )

    has_critical_error = False
    failed_links_report = []

    # Concurrent execution with a maximum of 10 workers
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_url = {
            executor.submit(validate_url, url, bypass_domains): url
            for url in url_to_files
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
                        failed_links_report.append((url, msg))
                    else:
                        print(f"[\033[93mWARN\033[0m] {url} - {msg}")
            except Exception as exc:
                print(f"[\033[93mWARN\033[0m] {url} generated an exception: {exc}")

    if has_critical_error:
        print("\n\033[91m========================================\033[0m")
        print("\033[91mCRITICAL LINK VALIDATION FAILURES\033[0m")
        print("\033[91m========================================\033[0m")
        for url, msg in sorted(failed_links_report):
            files = sorted(list(url_to_files[url]))
            print(f"\n\033[91mBroken URL:\033[0m {url}")
            print(f"  \033[93mStatus:\033[0m {msg}")
            print("  \033[90mFiles containing this link:\033[0m")
            for file_path in files:
                print(f"    - {file_path}")
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
