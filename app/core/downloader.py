"""Model downloader module with sandbox bypass, proxy support, and real-time tracking."""

import json
import logging
import os
import shutil
import threading
import urllib.error
import urllib.request

from app.core.exceptions import DownloaderError
from app.core.progress import ProgressUpdate, emit_progress

logger = logging.getLogger(__name__)

DEFAULT_MODEL_URL = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"


class DownloadError(DownloaderError):
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
            else:
                try:
                    from app.config import AppSettings

                    AppSettings.add_observer("PROXY", cls._instance._on_proxy_changed)
                except Exception:
                    pass
            return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance of DownloadManager."""
        with cls._lock:
            if cls._instance is not None:
                try:
                    from app.config import AppSettings

                    AppSettings.remove_observer("PROXY", cls._instance._on_proxy_changed)
                except Exception:
                    pass
                cls._instance = None

    def __init__(self, settings=None):
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
        self._opener_lock = threading.Lock()

        # Dynamic Proxy & Opener State
        self._settings = settings
        self._proxy = ""
        self._proxy_error = None
        self._opener = None

        self._initialize_proxy_observer()

    @property
    def _current_proxy(self):
        return self._proxy

    @_current_proxy.setter
    def _current_proxy(self, val):
        self._proxy = val or ""

    @property
    def _active_proxy_str(self):
        return self._proxy

    def _initialize_proxy_observer(self):
        """Subscribe DownloadManager to AppSettings proxy configuration changes."""
        try:
            from app.config import AppSettings

            if self._settings is None:
                self._settings = AppSettings()

            initial_proxy = getattr(self._settings, "PROXY", "")
            self.update_proxy(initial_proxy)

            # Register setting change listener during initialization
            self._settings.add_observer("PROXY", self._on_proxy_changed)
        except Exception as e:
            logger.warning(
                f"Failed to initialize proxy setting observer in DownloadManager: {e}"
            )

    def _on_proxy_changed(self, new_proxy: str):
        """Observer callback executed synchronously when PROXY configuration mutates."""
        logger.info(
            f"DownloadManager notified of proxy setting modification: '{new_proxy}'"
        )
        self.update_proxy(new_proxy)

    def update_proxy(self, proxy_str: str):
        """Invalidate cached network handlers and reconstruct opener using updated proxy URL."""
        with self._opener_lock:
            self._proxy = proxy_str or ""
            self._proxy_error = None
            handlers = []
            if self._proxy and self._proxy.strip():
                p_str = self._proxy.strip()
                if "<DECRYPTION_FAILED>" in p_str:
                    self._proxy_error = "Invalid proxy configuration: decryption failed."
                    self._opener = None
                else:
                    handlers.append(
                        urllib.request.ProxyHandler({"http": p_str, "https": p_str})
                    )
                    self._opener = urllib.request.build_opener(*handlers)
            else:
                handlers.append(urllib.request.ProxyHandler({}))
                self._opener = urllib.request.build_opener(*handlers)

    def get_opener(self, proxy: str = None) -> urllib.request.OpenerDirector:
        """Retrieve the active cached opener or raise NetworkError if proxy configuration is invalid."""
        with self._opener_lock:
            if proxy is not None and proxy != "":
                p_str = str(proxy).strip()
                if "<DECRYPTION_FAILED>" in p_str:
                    raise NetworkError("Invalid proxy configuration: decryption failed.")
                handlers = [urllib.request.ProxyHandler({"http": p_str, "https": p_str})]
                return urllib.request.build_opener(*handlers)

            if self._proxy_error or "<DECRYPTION_FAILED>" in self._proxy:
                raise NetworkError(
                    self._proxy_error or "Invalid proxy configuration: decryption failed."
                )

            handlers = []
            if self._proxy and self._proxy.strip():
                p_str = self._proxy.strip()
                handlers.append(
                    urllib.request.ProxyHandler({"http": p_str, "https": p_str})
                )
            else:
                handlers.append(urllib.request.ProxyHandler({}))
            self._opener = urllib.request.build_opener(*handlers)
            return self._opener

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

            # Sync proxy parameter if explicitly provided and different
            if proxy and proxy.strip() and proxy != self._proxy:
                self.update_proxy(proxy)

            effective_proxy = self._proxy if not proxy else proxy

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

            def progress_callback_wrapper(update: ProgressUpdate):
                self.state["progress"] = update.progress
                if update.stage:
                    self.state["status_text"] = update.stage
                else:
                    dl_mb = (update.unit_count or 0) / (1024 * 1024)
                    self.state["status_text"] = f"Downloaded {dl_mb:.2f}MB..."

            self.current_thread = run_background_download(
                url=url,
                model_dir=model_dir,
                proxy=effective_proxy,
                progress_callback=progress_callback_wrapper,
                on_success=on_success_wrapper,
                on_failure=on_failure_wrapper,
                cancel_event=self.cancel_event,
                download_manager=self,
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

                try:
                    from app.core.shared_registry import SharedModelRegistry
                    SharedModelRegistry.get_instance().unload_all_models()
                except Exception as e:
                    logger.warning(f"Failed to unload in-memory model instances during deletion: {e}")

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

    from app.core.resilient_file_ops import resilient_file_hash

    # Requirement 1: Calculate the SHA-256 hash using low-memory chunked streaming
    # Keeping memory footprint under 100MB of RAM even for large files.
    try:
        actual_hash = resilient_file_hash(temp_path, chunk_size=65536)
    except OSError as e:
        raise ModelVerificationError(
            f"Failed to read temporary file during hash calculation: {e}"
        ) from e

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
    download_manager=None,
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

            dm = download_manager
            if dm is None:
                try:
                    dm = DownloadManager.get_instance()
                except Exception:
                    dm = None

            def get_current_opener():
                if dm is not None:
                    return dm.get_opener(proxy)
                p_str = (proxy or "").strip()
                if p_str and "<DECRYPTION_FAILED>" in p_str:
                    raise NetworkError(
                        "Invalid proxy configuration: decryption failed."
                    )
                handlers = []
                if p_str:
                    handlers.append(
                        urllib.request.ProxyHandler({"http": p_str, "https": p_str})
                    )
                else:
                    handlers.append(urllib.request.ProxyHandler({}))
                return urllib.request.build_opener(*handlers)

            bytes_downloaded = 0
            total_size = 0
            chunk_size = 1024 * 64
            max_retries = 3
            retries = 0

            while True:
                try:
                    opener = get_current_opener()
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "Smart-AutoSorter/1.0"}
                    )
                    if bytes_downloaded > 0:
                        req.add_header("Range", f"bytes={bytes_downloaded}-")

                    mode = "ab" if bytes_downloaded > 0 else "wb"
                    with opener.open(req, timeout=15) as response:
                        if response.status == 206:
                            content_range = response.info().get("Content-Range")
                            if content_range and "/" in content_range:
                                total_size = int(content_range.split("/")[-1])
                        else:
                            if mode == "ab":
                                mode = "wb"
                                bytes_downloaded = 0
                            total_size = int(response.info().get("Content-Length", 0))

                        # Proactive disk space check
                        if total_size > 0 and bytes_downloaded == 0:
                            try:
                                _, _, free = shutil.disk_usage(model_dir)
                                if free < total_size:
                                    raise DiskSpaceError(
                                        f"Insufficient disk space. Required: {total_size} bytes, Free: {free} bytes."
                                    )
                            except OSError as e:
                                logger.warning(f"Could not retrieve disk usage: {e}")

                        with open(temp_path, mode) as f:
                            while True:
                                if cancel_event.is_set():
                                    raise DownloadCancelledError(
                                        "Download was cancelled by the user."
                                    )

                                try:
                                    chunk = response.read(chunk_size)
                                except Exception as e:
                                    raise NetworkError(
                                        f"Network error during read: {e}"
                                    ) from e

                                if not chunk:
                                    break

                                try:
                                    f.write(chunk)
                                except OSError as e:
                                    if e.errno == 28 or "No space" in str(e):
                                        raise DiskSpaceError(
                                            "Insufficient disk space on the target drive."
                                        ) from e
                                    raise DiskSpaceError(
                                        f"Local file write error: {e}"
                                    ) from e

                                bytes_downloaded += len(chunk)
                                ratio = (bytes_downloaded / total_size) if total_size > 0 else 0.0
                                msg = (
                                    f"Downloaded {bytes_downloaded / (1024 * 1024):.2f}MB of {total_size / (1024 * 1024):.2f}MB ({ratio * 100:.1f}%)"
                                    if total_size > 0
                                    else f"Downloaded {bytes_downloaded / (1024 * 1024):.2f}MB..."
                                )
                                emit_progress(
                                    progress_callback,
                                    progress_or_update=ratio,
                                    stage=msg,
                                    unit_count=bytes_downloaded,
                                    unit_type="bytes",
                                )
                    # Download succeeded
                    break

                except (DownloadCancelledError, DiskSpaceError):
                    raise
                except Exception as net_err:
                    if cancel_event.is_set():
                        raise DownloadCancelledError(
                            "Download was cancelled by the user."
                        )
                    if isinstance(net_err, DownloadError) and not isinstance(
                        net_err, NetworkError
                    ):
                        raise
                    retries += 1
                    if retries > max_retries:
                        if isinstance(net_err, DownloadError):
                            raise net_err
                        raise NetworkError(str(net_err)) from net_err
                    logger.info(
                        f"Retrying download operation ({retries}/{max_retries}) using updated proxy opener: {net_err}"
                    )

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
                except Exception as cleanup_err:
                    logger.warning(
                        f"Failed to clean up temporary file {temp_path}: {cleanup_err}"
                    )
            # Wrap as NetworkError if not already a subclass of DownloadError
            if isinstance(e, DownloadError):
                err = e
            else:
                err = NetworkError(str(e))
                err.__cause__ = e
            if on_failure:
                on_failure(err)

    from app.core.shared_registry import ContextPropagatingThread

    thread = ContextPropagatingThread(target=thread_target, daemon=True)
    thread.start()
    return thread
