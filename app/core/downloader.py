"""Model downloader module with sandbox bypass, proxy support, and real-time tracking."""

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
                temp_path = target_path + ".tmp"

                with open(temp_path, "wb") as f:
                    while True:
                        if cancel_event.is_set():
                            raise DownloadCancelledError(
                                "Download was cancelled by the user."
                            )

                        try:
                            chunk = response.read(chunk_size)
                        except Exception as e:
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

                # If we successfully wrote the full file, rename it and write config
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
