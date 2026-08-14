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


class ThreadSafeState:
    """A thread-safe state container.

    Provides synchronized dictionary-like access to internal state keys.
    """

    def __init__(self, **kwargs):
        self._lock = threading.Lock()
        self._state = kwargs

    def __getitem__(self, key):
        """Retrieve a value thread-safely."""
        with self._lock:
            return self._state[key]

    def __setitem__(self, key, value):
        """Store a value thread-safely."""
        with self._lock:
            self._state[key] = value

    def get(self, key, default=None):
        """Get a value safely with fallback."""
        with self._lock:
            return self._state.get(key, default)


class DownloadManager:
    """Centralized manager coordinating model downloads and progress sharing."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        """Retrieve the singleton instance of DownloadManager."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.state = ThreadSafeState(
            progress=0.0,
            status_text="Idle",
            error=None,
            success=False,
            is_downloading=False,
        )
        self.cancel_event = threading.Event()
        self.current_thread = None
        self._manager_lock = threading.Lock()

    def start_download(self, url: str, model_dir: str, proxy: str = ""):
        """Initiate model download thread-safely if not already downloading."""
        with self._manager_lock:
            if self.state["is_downloading"]:
                raise DownloadError("An installation is already underway.")

            self.state["progress"] = 0.0
            self.state["status_text"] = "Starting background download..."
            self.state["error"] = None
            self.state["success"] = False
            self.state["is_downloading"] = True
            self.cancel_event.clear()

            def on_success_wrapper():
                with self._manager_lock:
                    self.state["success"] = True
                    self.state["is_downloading"] = False
                    self.current_thread = None

            def on_failure_wrapper(err):
                with self._manager_lock:
                    self.state["error"] = err
                    self.state["is_downloading"] = False
                    self.current_thread = None

            def progress_callback_wrapper(downloaded, total):
                if total > 0:
                    pct = (downloaded / total) * 100
                    self.state["progress"] = downloaded / total
                    self.state["status_text"] = (
                        f"Downloaded {downloaded / (1024 * 1024):.2f}MB of {total / (1024 * 1024):.2f}MB ({pct:.1f}%)"
                    )
                else:
                    self.state["progress"] = 0.0
                    self.state["status_text"] = (
                        f"Downloaded {downloaded / (1024 * 1024):.2f}MB..."
                    )

            self.current_thread = run_background_download(
                url=url,
                model_dir=model_dir,
                proxy=proxy,
                progress_callback=progress_callback_wrapper,
                on_success=on_success_wrapper,
                on_failure=on_failure_wrapper,
                cancel_event=self.cancel_event,
            )
            return self.current_thread

    def cancel_download(self):
        """Cancel the active background download process."""
        with self._manager_lock:
            if self.state["is_downloading"]:
                self.cancel_event.set()
                self.state["is_downloading"] = False
                self.state["status_text"] = "Download cancelled."
                self.current_thread = None

    def delete_model_async(self, model_dir: str, on_done=None):
        """Asynchronously delete model files securely in a separate thread."""

        def delete_target():
            try:
                from app.core.shared_registry import _thread_local

                _thread_local.sandboxed = False
                _thread_local.reason = "model deletion execution"
            except Exception:
                pass

            try:
                self.cancel_download()

                import shutil

                if os.path.exists(model_dir):
                    shutil.rmtree(model_dir, ignore_errors=True)

                with self._manager_lock:
                    self.state["progress"] = 0.0
                    self.state["status_text"] = "Model deleted."
                    self.state["success"] = False
                    self.state["error"] = None
                    self.state["is_downloading"] = False

                if on_done:
                    on_done(True, None)
            except Exception as e:
                if on_done:
                    on_done(False, e)

        from app.core.shared_registry import ContextPropagatingThread

        t = ContextPropagatingThread(target=delete_target, daemon=True)
        t.start()
        return t


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
                if "<DECRYPTION_FAILED>" in p_str:
                    raise NetworkError(
                        "Invalid proxy configuration: decryption failed."
                    )
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
