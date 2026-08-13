"""Model downloader module with sandbox bypass, proxy support, and real-time tracking."""

import hashlib
import json
import logging
import os
import shutil
import threading
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_MODEL_URL = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"


class DownloadError(Exception):
    """Base class for download exceptions."""

    pass


class NetworkError(DownloadError):
    """Raised when a network/connection error occurs."""

    pass


class DiskSpaceError(DownloadError):
    """Raised when there is insufficient disk space."""

    pass


class DownloadCancelledError(DownloadError):
    """Raised when the download is cancelled by the user."""

    pass


class ModelVerificationError(DownloadError):
    """Raised when downloaded model integrity or cryptographic validation fails."""

    pass


def verify_temp_file_hash(temp_path: str, target_path: str) -> bool:
    """Calculate SHA-256 hash of the temp file and verify it against the central registry.

    Raises ModelVerificationError if verification fails.
    """
    if not os.path.exists(temp_path):
        raise ModelVerificationError("Temporary download file does not exist.")

    # Requirement 1: Calculate the SHA-256 hash using low-memory chunked streaming
    # Keeping memory footprint under 100MB of RAM even for large files.
    hasher = hashlib.sha256()
    try:
        with open(temp_path, "rb") as f:
            # Use 64KB chunk size (65536 bytes) to keep memory footprint minimal
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
    except OSError as e:
        raise ModelVerificationError(
            f"Failed to read temporary file during hash calculation: {e}"
        )

    actual_hash = hasher.hexdigest()

    # Requirement 2: Validate computed hash against central registry
    from app.core.shared_registry import SharedModelRegistry

    registry = SharedModelRegistry.get_instance()

    filename = os.path.basename(target_path)
    expected_hash = None

    if "model_download" in registry._expected_hashes:
        expected_hash = registry._expected_hashes["model_download"].get(filename)
    if not expected_hash and "generative_naming" in registry._expected_hashes:
        expected_hash = registry._expected_hashes["generative_naming"].get(filename)

    if not expected_hash:
        raise ModelVerificationError(
            f"No cryptographic hash registered in the central registry for {filename}. "
            "Verification cannot proceed."
        )

    if actual_hash != expected_hash:
        # Requirement 3: Block finalization and immediately discard the temporary file
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception as e:
                logger.error(
                    f"Failed to immediately delete temporary file {temp_path}: {e}"
                )

        raise ModelVerificationError(
            f"Cryptographic signature verification failed for {filename}.\n"
            f"Expected: {expected_hash}\n"
            f"Actual: {actual_hash}\n"
            "This file may be corrupted, incomplete, or tampered with."
        )

    return True


def verify_downloaded_model(model_dir: str) -> bool:
    """Verify integrity of the completed download.

    Checks if model.onnx and config.json exist, are non-empty, and valid.
    """
    onnx_file = os.path.join(model_dir, "model.onnx")
    config_file = os.path.join(model_dir, "config.json")
    if not os.path.exists(onnx_file) or os.path.getsize(onnx_file) == 0:
        return False
    if not os.path.exists(config_file):
        return False
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            json.load(f)
    except Exception:
        return False
    return True


def run_background_download(
    url: str,
    model_dir: str,
    proxy: str = "",
    progress_callback=None,
    on_success=None,
    on_failure=None,
    cancel_event=None,
    notification_queue=None,
    base_delay: float = 1.0,
):
    """Run model download in a dedicated background thread that bypasses sandboxing."""
    if cancel_event is None:
        cancel_event = threading.Event()

    def thread_target():
        # Requirement 5: Bypass application sandboxing on this dedicated thread
        try:
            from app.core.shared_registry import _thread_local

            _thread_local.sandboxed = False
            _thread_local.reason = "model download execution"
        except Exception as e:
            logger.debug(f"Failed to clear sandboxed flag on downloader thread: {e}")

        try:
            # Create model directory
            os.makedirs(model_dir, exist_ok=True)
            target_path = os.path.join(model_dir, "model.onnx")
            temp_path = target_path + ".tmp"

            max_attempts = 5
            success = False

            for attempt in range(1, max_attempts + 1):
                try:
                    # Clear out any previous partial download file to start from scratch (0%)
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    # Setup urllib opener with proxy support if specified
                    handlers = []
                    if proxy and proxy.strip():
                        p_str = proxy.strip()
                        handlers.append(
                            urllib.request.ProxyHandler({"http": p_str, "https": p_str})
                        )
                    opener = urllib.request.build_opener(*handlers)

                    req = urllib.request.Request(
                        url, headers={"User-Agent": "Smart-AutoSorter/1.0"}
                    )

                    with opener.open(req, timeout=15) as response:
                        total_size = int(response.info().get("Content-Length", 0))

                        # Proactive disk space check
                        if total_size > 0:
                            try:
                                _, _, free = shutil.disk_usage(model_dir)
                                if free < total_size:
                                    raise DiskSpaceError(
                                        f"Insufficient disk space. Required: {total_size} bytes, Free: {free} bytes."
                                    )
                            except OSError as e:
                                # If disk_usage fails (e.g. on custom mount), we log and proceed
                                logger.warning(f"Could not retrieve disk usage: {e}")

                        bytes_downloaded = 0
                        chunk_size = 1024 * 64  # 64KB chunks

                        with open(temp_path, "wb") as f:
                            while True:
                                if cancel_event.is_set():
                                    raise DownloadCancelledError(
                                        "Download was cancelled by the user."
                                    )

                                try:
                                    chunk = response.read(chunk_size)
                                except Exception as e:
                                    if cancel_event.is_set():
                                        raise DownloadCancelledError(
                                            "Download was cancelled by the user."
                                        )
                                    raise NetworkError(f"Network error during read: {e}") from e

                                if not chunk:
                                    break

                                try:
                                    f.write(chunk)
                                except OSError as e:
                                    if e.errno == 28 or "No space" in str(e):
                                        raise DiskSpaceError(
                                            "Insufficient disk space on the target drive."
                                        ) from e
                                    raise DiskSpaceError(f"Local file write error: {e}") from e

                                bytes_downloaded += len(chunk)
                                if progress_callback:
                                    try:
                                        progress_callback(bytes_downloaded, total_size)
                                    except Exception:
                                        pass

                        # If we reached here, the download of the file is complete
                        success = True
                        break

                except (DownloadCancelledError, DiskSpaceError) as e:
                    # Do not retry on explicit cancellation or disk space exhaustion
                    raise e
                except Exception as e:
                    if attempt == max_attempts:
                        # Retries exhausted, raise final error
                        raise e
                    else:
                        # Exponential backoff retry with randomized jitter
                        import random
                        import time

                        delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.1, 0.5)
                        msg = f"Network drop detected. Retrying download (attempt {attempt}/{max_attempts}) in {delay:.2f}s..."
                        logger.warning(msg)
                        if notification_queue is not None:
                            try:
                                notification_queue.put(msg)
                            except Exception:
                                pass
                        time.sleep(delay)

            # Requirement 3 & Zero-Trust File Finalization: Calculate and verify cryptographic hash before moving to final destination
            verify_temp_file_hash(temp_path, target_path)

            # If verification passes, finalize and rename it and write config
            if os.path.exists(temp_path):
                if os.path.exists(target_path):
                    os.remove(target_path)
                os.rename(temp_path, target_path)

            # Write a placeholder config.json next to it
            config_path = os.path.join(model_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as cf:
                json.dump({"model_type": "onnx", "dimensions": 384}, cf)

            # Requirement 6: Run integrity verification on completed download
            if not verify_downloaded_model(model_dir):
                raise DownloadError(
                    "Integrity verification failed for the downloaded model."
                )

            if on_success:
                on_success()

        except DownloadCancelledError as e:
            # Clean up temp files
            temp_path = os.path.join(model_dir, "model.onnx.tmp")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            if on_failure:
                on_failure(e)

        except DiskSpaceError as e:
            # Clean up temp files
            temp_path = os.path.join(model_dir, "model.onnx.tmp")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            if on_failure:
                on_failure(e)

        except Exception as e:
            # Clean up temp files
            temp_path = os.path.join(model_dir, "model.onnx.tmp")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            # Wrap as NetworkError if not already a subclass of DownloadError
            err = e if isinstance(e, DownloadError) else NetworkError(str(e))
            if on_failure:
                on_failure(err)

    from app.core.shared_registry import ContextPropagatingThread

    thread = ContextPropagatingThread(target=thread_target, daemon=True)
    thread.start()
    return thread
