"""Centralized Standard Library Download Manager.

Provides robust, memory-optimized download capability using only the Python standard library.
Supports chunked streaming, transactional .part file writing, HTTP Range resumes,
graceful fallback, and SHA-256 integrity checks.
"""

import hashlib
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Base exception for downloader errors."""

    pass


class DownloadValidationError(DownloadError):
    """Exception raised when integrity verification fails."""

    pass


def download_file(
    url: str,
    dest_path: str | Path,
    expected_sha256: str = None,
    chunk_size: int = 8192,
    progress_callback=None,
    headers: dict = None,
) -> Path:
    """Download a remote file using only python standard library.

    Args:
        url: The URL to download.
        dest_path: The target file path.
        expected_sha256: Expected SHA-256 signature for integrity validation.
        chunk_size: Small chunk size for writing (helps optimize RAM usage).
        progress_callback: A callable taking (bytes_downloaded, total_bytes).
        headers: Additional HTTP headers to merge with default browser headers.

    Returns
    -------
        Path: The resolved absolute path of the completed file.

    Raises
    ------
        DownloadValidationError: If SHA-256 integrity check fails.
        DownloadError: If any network, file, or other operational error occurs.
    """
    dest_path = Path(dest_path).resolve()
    # Ensure target directory has verified write permissions
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    part_path = dest_path.with_name(dest_path.name + ".part")

    # Standard browser headers to avoid getting blocked
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    if headers:
        req_headers.update(headers)

    existing_size = 0
    if part_path.exists():
        existing_size = part_path.stat().st_size

    response = None
    opened_mode = "wb"
    bytes_downloaded = 0
    total_bytes = None

    if existing_size > 0:
        resume_headers = dict(req_headers)
        resume_headers["Range"] = f"bytes={existing_size}-"
        req = urllib.request.Request(url, headers=resume_headers)
        try:
            response = urllib.request.urlopen(req)
            status = response.status if hasattr(response, "status") else response.getcode()
            if status == 206:
                opened_mode = "ab"
                bytes_downloaded = existing_size
                content_range = response.headers.get("Content-Range")
                if content_range:
                    try:
                        # Extract the total length from "bytes X-Y/Z" format
                        total_bytes = int(content_range.split("/")[-1])
                    except Exception:
                        pass
                if total_bytes is None:
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        total_bytes = existing_size + int(content_length)
                logger.info(f"Resuming download from byte {existing_size}. Total bytes: {total_bytes}")
            else:
                logger.info("Server did not return status 206. Restarting download from beginning.")
                opened_mode = "wb"
                bytes_downloaded = 0
                content_length = response.headers.get("Content-Length")
                if content_length:
                    total_bytes = int(content_length)
        except Exception as e:
            logger.info(f"Range request failed ({e}). Falling back to full chunked download from scratch.")
            response = None

    if response is None:
        req = urllib.request.Request(url, headers=req_headers)
        try:
            response = urllib.request.urlopen(req)
        except Exception as e:
            raise DownloadError(f"Failed to start download from {url}: {e}") from e
        opened_mode = "wb"
        bytes_downloaded = 0
        content_length = response.headers.get("Content-Length")
        if content_length:
            total_bytes = int(content_length)

    # Perform chunked stream read and write directly to disk to minimize RAM
    try:
        with open(part_path, opened_mode) as f:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                bytes_downloaded += len(chunk)
                if progress_callback:
                    try:
                        progress_callback(bytes_downloaded, total_bytes)
                    except Exception as p_err:
                        logger.warning(f"Error in progress callback: {p_err}")
    except Exception as e:
        raise DownloadError(f"Error downloading {url} to {part_path}: {e}") from e
    finally:
        try:
            response.close()
        except Exception:
            pass

    # Verify SHA-256 integrity hash
    if expected_sha256:
        hasher = hashlib.sha256()
        try:
            with open(part_path, "rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
            actual_sha256 = hasher.hexdigest()
        except Exception as e:
            raise DownloadError(f"Failed to calculate SHA-256 signature for {part_path}: {e}") from e

        if actual_sha256 != expected_sha256:
            # Delete corrupted partial file so retry can start clean
            try:
                part_path.unlink()
            except Exception:
                pass
            raise DownloadValidationError(
                f"Integrity check failed for {dest_path}. Expected {expected_sha256}, got {actual_sha256}"
            )

    # Atomic swap renaming .part to final target path
    try:
        if dest_path.exists():
            dest_path.unlink()
        part_path.rename(dest_path)
    except Exception as e:
        raise DownloadError(f"Failed to rename completed file from {part_path} to {dest_path}: {e}") from e

    return dest_path


def download_ai_models(settings, progress_callback=None) -> bool:
    """Download missing/corrupt AI models (Generative naming and OCR models) if enabled/missing.

    Returns True if everything was downloaded successfully, False otherwise.
    """
    from app.config import get_app_dir
    from app.core.shared_registry import SharedModelRegistry
    from app.core.user_space_bootstrap import is_file_valid

    # Resolve generative naming model directory
    try:
        user_bundle_path = get_app_dir() / "model"
    except Exception:
        user_bundle_path = Path(os.path.expanduser("~/.smart-autosorter/model"))

    # Resolve OCR models directory
    easyocr_path = os.environ.get("EASYOCR_MODULE_PATH")
    if easyocr_path:
        easyocr_dir = Path(easyocr_path) / "model"
    else:
        easyocr_dir = Path(os.path.expanduser("~/.EasyOCR/model"))

    # MODEL_BASE_URL can be configured
    MODEL_BASE_URL = os.environ.get("AUTOSORTER_MODEL_BASE_URL", "https://example.com/models")

    downloads_todo = []

    # Get SharedModelRegistry instance to check registered hashes
    registry = SharedModelRegistry.get_instance()

    # 1. EasyOCR models
    craft_model_path = easyocr_dir / "craft_mlt_25k.pth"
    lang_model_path = easyocr_dir / "english_g2.pth"

    easyocr_hashes = registry._expected_hashes.get("easyocr", {})
    craft_expected = easyocr_hashes.get("craft_mlt_25k.pth")
    lang_expected = easyocr_hashes.get("english_g2.pth")

    if not is_file_valid(craft_model_path, craft_expected):
        downloads_todo.append({
            "url": f"{MODEL_BASE_URL}/craft_mlt_25k.pth",
            "dest": craft_model_path,
            "hash": craft_expected
        })

    if not is_file_valid(lang_model_path, lang_expected):
        downloads_todo.append({
            "url": f"{MODEL_BASE_URL}/english_g2.pth",
            "dest": lang_model_path,
            "hash": lang_expected
        })

    # 2. Generative model (if AI_ASSISTED_NAMING is enabled)
    if getattr(settings, "AI_ASSISTED_NAMING", False):
        gen_hashes = registry._expected_hashes.get("generative_naming", {})
        if not gen_hashes:
            # Fallback default
            gen_hashes = {"config.json": None}

        for filename, expected_hash in gen_hashes.items():
            file_path = user_bundle_path / filename
            if not is_file_valid(file_path, expected_hash):
                downloads_todo.append({
                    "url": f"{MODEL_BASE_URL}/{filename}",
                    "dest": file_path,
                    "hash": expected_hash
                })

    if not downloads_todo:
        logger.info("All enabled AI models are healthy and present.")
        return True

    # Download everything sequentially
    total_files = len(downloads_todo)
    for i, item in enumerate(downloads_todo):
        logger.info(f"Downloading model {i+1}/{total_files}: {item['url']} to {item['dest']}")

        def sub_progress(bytes_dl, total_bytes):
            if progress_callback:
                try:
                    progress_callback(bytes_dl, total_bytes, i, total_files, item['dest'].name)
                except Exception as cb_err:
                    logger.warning(f"Error in model progress callback: {cb_err}")

        try:
            download_file(
                item["url"],
                item["dest"],
                expected_sha256=item["hash"],
                progress_callback=sub_progress
            )
        except Exception as e:
            logger.error(f"Failed to download model file {item['dest'].name}: {e}")
            raise e

    return True

