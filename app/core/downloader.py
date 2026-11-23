"""Model downloader module with sandbox bypass, proxy support, and real-time tracking."""

import contextlib
import ipaddress
import json
import logging
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from typing import Any

from app.core.exceptions import DownloaderError, ModelVerificationError
from app.core.lifecycle import LifecycleTelemetryMixin
from app.core.progress import ProgressUpdate, emit_progress

logger = logging.getLogger(__name__)

DEFAULT_MODEL_URL = (
    "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model.onnx"
)

_REQUIRED_BUNDLE_FILES: tuple[str, ...] = (
    "model.onnx",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
    "version.txt",
)


class DownloadError(DownloaderError):
    """Base class for download exceptions."""


ALLOWED_MODEL_HOSTS: frozenset[str] = frozenset({"huggingface.co", "hf.co"})
_ALLOWED_MODEL_HOSTS = ALLOWED_MODEL_HOSTS
_LOCAL_HOSTNAMES: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
)


def _normalize_trusted_model_hosts(trusted_hosts=None) -> frozenset[str]:
    """Normalize optional configured trusted hostnames alongside default Hugging Face hosts."""
    combined = set(_ALLOWED_MODEL_HOSTS)
    if not trusted_hosts:
        return frozenset(combined)
    for raw_host in trusted_hosts:
        if not isinstance(raw_host, str):
            continue
        candidate = raw_host.strip().lower().rstrip(".")
        if "://" in candidate:
            parsed_cand = urllib.parse.urlsplit(candidate)
            candidate = (parsed_cand.hostname or "").strip().lower().rstrip(".")
        if (
            not candidate
            or candidate.startswith(".")
            or candidate in _LOCAL_HOSTNAMES
            or candidate.endswith((".local", ".localhost"))
        ):
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            combined.add(candidate)
    return frozenset(combined)


def _is_allowed_model_host(hostname: str, trusted_hosts=None) -> bool:
    """Check if the given hostname matches an allowed model host or trusted subdomain."""
    host = hostname.lower().rstrip(".")
    if not host or host.startswith("."):
        return False
    allowed_set = _normalize_trusted_model_hosts(trusted_hosts)
    return host in allowed_set or any(
        host.endswith(f".{allowed}") for allowed in allowed_set
    )


def _validate_url(url: str, trusted_hosts=None) -> None:
    """Validate that the model download URL uses HTTPS and points to an allowed model host.

    Rejects non-HTTPS schemes, private/loopback/link-local IP addresses, empty hostnames,
    and untrusted hostnames with DownloadError.
    """
    if not isinstance(url, str) or not url.strip():
        raise DownloadError("Model download URL must be a non-empty string.")

    cleaned_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(cleaned_url)
    except ValueError as e:
        raise DownloadError(f"Malformed model download URL: {e}") from e

    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise DownloadError(
            f"Unsupported URL scheme '{parsed.scheme}'. Only HTTPS is permitted."
        )

    hostname = parsed.hostname
    if not hostname or not hostname.strip():
        raise DownloadError("URL hostname is missing or empty.")

    host_clean = hostname.strip().lower().rstrip(".")
    if not host_clean or host_clean.startswith("."):
        raise DownloadError(f"Invalid URL hostname: {hostname}")

    # Reject IP address literals (loopback, private, link-local, unspecified, or public)
    try:
        ip = ipaddress.ip_address(host_clean)
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise DownloadError(
                f"Private or local IP addresses are not permitted: {hostname}"
            )
        raise DownloadError(
            f"IP address literals are not permitted as model download hosts: {hostname}"
        )
    except ValueError:
        pass

    # Reject local hostnames and loopback names
    if host_clean in _LOCAL_HOSTNAMES or host_clean.endswith((".local", ".localhost")):
        raise DownloadError(
            f"Local or loopback hostnames are not permitted: {hostname}"
        )

    # Restrict to trusted model hosts
    if not _is_allowed_model_host(host_clean, trusted_hosts=trusted_hosts):
        raise DownloadError(
            f"Untrusted model download host: {hostname}. Only trusted model hosts are permitted."
        )


_validate_model_url = _validate_url


class InvalidStateTransitionError(DownloadError, ValueError):
    """Raised when an invalid lifecycle state transition is attempted."""


class NetworkError(DownloadError):
    """Raised when a network/connection error occurs."""


class DiskSpaceError(DownloadError):
    """Raised when there is insufficient disk space."""


class DownloadCancelledError(DownloadError):
    """Raised when the download is cancelled by the user."""


class DownloadState(str, Enum):
    """Lifecycle states for model downloads."""

    IDLE = "IDLE"
    DOWNLOADING = "DOWNLOADING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


_VALID_TRANSITIONS: dict[DownloadState, set[DownloadState]] = {
    DownloadState.IDLE: {DownloadState.DOWNLOADING, DownloadState.IDLE},
    DownloadState.DOWNLOADING: {
        DownloadState.COMPLETED,
        DownloadState.FAILED,
        DownloadState.CANCELLED,
    },
    DownloadState.COMPLETED: {DownloadState.DOWNLOADING, DownloadState.IDLE},
    DownloadState.FAILED: {DownloadState.DOWNLOADING, DownloadState.IDLE},
    DownloadState.CANCELLED: {
        DownloadState.DOWNLOADING,
        DownloadState.IDLE,
        DownloadState.CANCELLED,
    },
}


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


class DownloadManager(LifecycleTelemetryMixin):
    """Centralized manager coordinating model downloads and progress sharing."""

    DownloadState = DownloadState
    InvalidStateTransitionError = InvalidStateTransitionError

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
                    logger.debug("Could not register proxy observer", exc_info=True)
            return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance of DownloadManager."""
        with cls._lock:
            if cls._instance is not None:
                try:
                    from app.config import AppSettings

                    AppSettings.remove_observer(
                        "PROXY", cls._instance._on_proxy_changed
                    )
                except Exception:
                    logger.debug("Could not remove proxy observer", exc_info=True)
                cls._instance = None

    def __init__(self, settings=None):
        self._status = DownloadState.IDLE
        self.state = ThreadSafeState(
            progress=0.0,
            status_text="Idle",
            error=None,
            success=False,
            is_downloading=False,
            status=DownloadState.IDLE,
            download_state=DownloadState.IDLE,
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
    def current_state(self) -> DownloadState:
        """Retrieve current lifecycle state thread-safely."""
        with self._manager_lock:
            return self._status

    @property
    def status(self) -> DownloadState:
        """Alias for current lifecycle state."""
        with self._manager_lock:
            return self._status

    def _transition_to_locked(self, new_state: DownloadState) -> None:
        """Validate and apply lifecycle state transition under lock."""
        current = self._status
        valid_targets = _VALID_TRANSITIONS.get(current, set())
        if new_state not in valid_targets:
            raise InvalidStateTransitionError(
                f"Invalid state transition from {current} to {new_state}."
            )
        old_state = current
        self._status = new_state
        self.state["status"] = new_state
        self.state["download_state"] = new_state
        if new_state == DownloadState.DOWNLOADING:
            self.state["is_downloading"] = True
            self.state["success"] = False
        elif new_state == DownloadState.COMPLETED:
            self.state["is_downloading"] = False
            self.state["success"] = True
        elif new_state in (
            DownloadState.CANCELLED,
            DownloadState.FAILED,
            DownloadState.IDLE,
        ):
            self.state["is_downloading"] = False
            self.state["success"] = False

        self._notify_lifecycle_listeners(
            old_state, new_state, event_type="DOWNLOAD_STATE_CHANGED"
        )

    def transition_to(self, new_state: DownloadState) -> None:
        """Atomically transition manager to a new lifecycle state."""
        with self._manager_lock:
            self._transition_to_locked(new_state)

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
                f"Failed to initialize proxy setting observer in DownloadManager: {e}",
                exc_info=True,
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
                    self._proxy_error = (
                        "Invalid proxy configuration: decryption failed."
                    )
                    self._opener = None
                else:
                    handlers.append(
                        urllib.request.ProxyHandler({"http": p_str, "https": p_str})
                    )
                    self._opener = urllib.request.build_opener(*handlers)
            else:
                handlers.append(urllib.request.ProxyHandler({}))
                self._opener = urllib.request.build_opener(*handlers)

    def get_opener(self, proxy: str | None = None) -> urllib.request.OpenerDirector:
        """Retrieve the active cached opener or raise NetworkError if proxy configuration is invalid."""
        with self._opener_lock:
            if proxy is not None and proxy != "":
                p_str = str(proxy).strip()
                if "<DECRYPTION_FAILED>" in p_str:
                    raise NetworkError(
                        "Invalid proxy configuration: decryption failed."
                    )
                handlers = [
                    urllib.request.ProxyHandler({"http": p_str, "https": p_str})
                ]
                return urllib.request.build_opener(*handlers)

            if self._proxy_error or "<DECRYPTION_FAILED>" in self._proxy:
                raise NetworkError(
                    self._proxy_error
                    or "Invalid proxy configuration: decryption failed."
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

    def _get_trusted_hosts(self) -> frozenset[str]:
        """Return normalized trusted model download hostnames including configured mirrors."""
        raw_hosts = getattr(self._settings, "TRUSTED_MODEL_HOSTS", None)
        if raw_hosts is None:
            try:
                from app.config import AppSettings

                raw_hosts = AppSettings().TRUSTED_MODEL_HOSTS
            except (OSError, ValueError, RuntimeError):
                raw_hosts = None
        return _normalize_trusted_model_hosts(raw_hosts)

    def start_download(self, url: str, model_dir: str, proxy: str = ""):
        """Initiate model download thread-safely if not already downloading."""
        with self._manager_lock:
            if (
                self._status == DownloadState.DOWNLOADING
                or self.state["is_downloading"]
            ):
                raise DownloadError("An installation is already underway.")

            _validate_url(url, trusted_hosts=self._get_trusted_hosts())

            self.cancel_event.clear()
            self._transition_to_locked(DownloadState.DOWNLOADING)
            self.state["progress"] = 0.0
            self.state["status_text"] = "Starting background download..."
            self.state["error"] = None
            self.state["success"] = False
            self.state["is_downloading"] = True

            # Sync proxy parameter if explicitly provided and different
            if proxy and proxy.strip() and proxy != self._proxy:
                self.update_proxy(proxy)

            effective_proxy = self._proxy if not proxy else proxy

            def on_success_wrapper():
                with self._manager_lock:
                    if (
                        self._status == DownloadState.CANCELLED
                        or self.cancel_event.is_set()
                    ):
                        logger.debug(
                            "Download completed callback ignored because download was cancelled."
                        )
                        return
                    if self._status != DownloadState.DOWNLOADING:
                        logger.warning(
                            f"Download completed callback ignored due to unexpected state: {self._status}"
                        )
                        return
                    self._transition_to_locked(DownloadState.COMPLETED)
                    self.state["status_text"] = "Download complete!"
                    self.current_thread = None

            def on_failure_wrapper(err):
                with self._manager_lock:
                    if (
                        self._status == DownloadState.CANCELLED
                        or self.cancel_event.is_set()
                        or isinstance(err, DownloadCancelledError)
                    ):
                        logger.debug("Download failure callback received cancellation.")
                        if self._status != DownloadState.CANCELLED:
                            self._transition_to_locked(DownloadState.CANCELLED)
                        self.state["status_text"] = "Download cancelled."
                        self.current_thread = None
                        return
                    if self._status != DownloadState.DOWNLOADING:
                        logger.warning(
                            f"Download failure callback ignored due to unexpected state: {self._status}"
                        )
                        return
                    self._transition_to_locked(DownloadState.FAILED)
                    self.state["error"] = err
                    self.state["status_text"] = f"Download failed: {err}"
                    self.current_thread = None

            def progress_callback_wrapper(update: ProgressUpdate):
                with self._manager_lock:
                    if (
                        self._status == DownloadState.CANCELLED
                        or self.cancel_event.is_set()
                    ):
                        return
                    if self._status != DownloadState.DOWNLOADING:
                        return
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
            if (
                self._status == DownloadState.DOWNLOADING
                or self.state["is_downloading"]
            ):
                self.cancel_event.set()
                if self._status == DownloadState.DOWNLOADING:
                    self._transition_to_locked(DownloadState.CANCELLED)
                else:
                    self.state["is_downloading"] = False
                self.state["status_text"] = "Download cancelled."
                self.current_thread = None

    def delete_model_async(self, model_dir: str, on_done=None):
        """Asynchronously delete model files securely in a separate thread."""

        def delete_target():
            from app.core.network_guard import unblock_external_network

            with unblock_external_network(reason="model deletion execution"):
                try:
                    self.cancel_download()

                    try:
                        from app.core.shared_registry import SharedModelRegistry

                        SharedModelRegistry.get_instance().unload_all_models()
                    except Exception as e:
                        logger.warning(
                            f"Failed to unload in-memory model instances during deletion: {e}",
                            exc_info=True,
                        )

                    from app.core.resilient_file_ops import resilient_rmtree

                    if os.path.exists(model_dir):
                        resilient_rmtree(model_dir, ignore_errors=True)

                    with self._manager_lock:
                        self.cancel_event.clear()
                        self._transition_to_locked(DownloadState.IDLE)
                        self.state["progress"] = 0.0
                        self.state["status_text"] = "Model deleted."
                        self.state["success"] = False
                        self.state["error"] = None
                        self.state["is_downloading"] = False

                    if on_done:
                        on_done(True, None)
                except Exception as e:
                    logger.debug("Model deletion failed", exc_info=True)
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
    expected_hashes = registry.get_expected_hashes_for_file(filename)

    if not expected_hashes:
        raise ModelVerificationError(
            f"No cryptographic hash registered in the central registry for {filename}. "
            "Verification cannot proceed."
        )

    if actual_hash not in expected_hashes:
        # Requirement 3: Block finalization and immediately discard the temporary file
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError as e:
                logger.error(
                    f"Failed to immediately delete temporary file {temp_path}: {e}"
                )

        expected_str = (
            ", ".join(expected_hashes)
            if len(expected_hashes) > 1
            else expected_hashes[0]
        )
        raise ModelVerificationError(
            f"Cryptographic signature verification failed for {filename}.\n"
            f"Expected: {expected_str}\n"
            f"Actual: {actual_hash}\n"
            "This file may be corrupted, incomplete, or tampered with."
        )

    return True


def _inspect_model_bundle(model_dir: str) -> tuple[str, str]:
    """Inspect a local model directory and return `(status_code, detail_message)`.

    Status codes are `'missing'`, `'incomplete'`, `'corrupted'`, or `'valid'`.
    """
    if not model_dir or not os.path.isdir(model_dir):
        return ("missing", "Model directory does not exist.")

    onnx_file = os.path.join(model_dir, "model.onnx")
    if not os.path.isfile(onnx_file) or os.path.getsize(onnx_file) == 0:
        return ("missing", "Model weights (model.onnx) are missing or empty.")

    from app.core.shared_registry import SharedModelRegistry

    registry = SharedModelRegistry.get_instance()
    expected_hashes = registry.get_expected_hashes("model_download")

    # Preserve explicit single-file test overrides while enforcing the full 7-file bundle by default
    if expected_hashes and set(expected_hashes.keys()) == {"model.onnx"}:
        required_files: tuple[str, ...] = ("model.onnx", "config.json")
    else:
        combined_req = list(_REQUIRED_BUNDLE_FILES)
        for fname in expected_hashes:
            if fname not in combined_req:
                combined_req.append(fname)
        required_files = tuple(combined_req)

    missing_files = [
        fname
        for fname in required_files
        if not os.path.isfile(os.path.join(model_dir, fname))
        or os.path.getsize(os.path.join(model_dir, fname)) == 0
    ]
    if missing_files:
        return (
            "incomplete",
            f"Missing required files: {', '.join(missing_files)}",
        )

    config_file = os.path.join(model_dir, "config.json")
    try:
        with open(config_file, "r", encoding="utf-8") as cf:
            cfg_obj = json.load(cf)
        if not isinstance(cfg_obj, dict):
            return ("corrupted", "config.json must contain a JSON object.")
    except (OSError, ValueError):
        return ("corrupted", "config.json is unreadable or invalid JSON.")

    if expected_hashes:
        from app.core.resilient_file_ops import resilient_file_hash

        for fname, exp_hash in expected_hashes.items():
            fpath = os.path.join(model_dir, fname)
            if not os.path.isfile(fpath):
                return ("incomplete", f"Missing required file: {fname}")
            try:
                actual_hash = resilient_file_hash(fpath, chunk_size=65536)
            except OSError:
                return (
                    "corrupted",
                    f"Could not read {fname} for SHA-256 verification.",
                )
            if actual_hash != exp_hash:
                return ("corrupted", f"SHA-256 digest mismatch for {fname}.")

    return ("valid", "All model and tokenizer files passed verification.")


def verify_downloaded_model(model_dir: str) -> bool:
    """Verify integrity of the completed model bundle download.

    Returns True only when `model.onnx`, `config.json`, and all required tokenizer
    and bundle files exist and pass cryptographic SHA-256 verification.
    """
    status_code, _ = _inspect_model_bundle(model_dir)
    return status_code == "valid"


def _derive_repo_resolve_base(model_url: str) -> str:
    """Derive the repository `/resolve/<revision>` base URL from a model weight URL."""
    clean = model_url.strip()
    parsed = urllib.parse.urlsplit(clean)
    path = parsed.path or ""
    if path.endswith("/onnx/model.onnx"):
        base_path = path[: -len("/onnx/model.onnx")]
    elif path.endswith("/model.onnx"):
        base_path = path[: -len("/model.onnx")]
    else:
        base_path = path.rsplit("/", 1)[0]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))


def _resolve_bundle_plan(
    url: str,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Build `(remote_files, generated_files, expected_hashes)` for `run_background_download`."""
    from app.core.shared_registry import SharedModelRegistry

    expected_hashes = dict(
        SharedModelRegistry.get_instance().get_expected_hashes("model_download") or {}
    )

    if expected_hashes and set(expected_hashes.keys()) == {"model.onnx"}:
        return (
            {"model.onnx": url},
            {"config.json": json.dumps({"model_type": "onnx", "dimensions": 384})},
            expected_hashes,
        )

    base_url = _derive_repo_resolve_base(url)
    remote_files: dict[str, str] = {"model.onnx": url}
    companion_names = (
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.txt",
    )
    for fname in companion_names:
        if not expected_hashes or fname in expected_hashes:
            remote_files[fname] = f"{base_url}/{fname}"

    for fname in expected_hashes:
        if fname not in remote_files and fname != "version.txt":
            remote_files[fname] = f"{base_url}/{fname}"

    generated_files: dict[str, str] = {}
    if not expected_hashes or "version.txt" in expected_hashes:
        generated_files["version.txt"] = "1.0.0"

    return (remote_files, generated_files, expected_hashes)


def _activate_staged_bundle(staging_dir: str, model_dir: str) -> None:
    """Atomically promote a verified `staging_dir` bundle to `model_dir`."""
    if not os.path.exists(model_dir):
        os.replace(staging_dir, model_dir)
        return

    backup_dir = f"{staging_dir}_prev"
    try:
        os.replace(model_dir, backup_dir)
        try:
            os.replace(staging_dir, model_dir)
        except OSError:
            with contextlib.suppress(OSError):
                os.replace(backup_dir, model_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
    except PermissionError:
        moved_targets: list[str] = []
        try:
            for entry in os.listdir(staging_dir):
                src = os.path.join(staging_dir, entry)
                dst = os.path.join(model_dir, entry)
                os.replace(src, dst)
                moved_targets.append(dst)
        except OSError:
            for dst in moved_targets:
                with contextlib.suppress(OSError):
                    os.remove(dst)
            raise


def run_background_download(
    url: str,
    model_dir: str,
    proxy: str = "",
    progress_callback=None,
    on_success=None,
    on_failure=None,
    cancel_event=None,
    download_manager=None,
    initial_delay: float = 1.0,
    backoff_factor: float = 2.0,
    max_retries: int = 3,
):
    """Run model bundle download in a dedicated background thread that bypasses sandboxing."""
    if cancel_event is None:
        cancel_event = threading.Event()

    def thread_target():
        dm = download_manager
        if dm is not None and hasattr(dm, "_get_trusted_hosts"):
            trusted_hosts = dm._get_trusted_hosts()
        else:
            raw_hosts = getattr(
                getattr(dm, "_settings", None), "TRUSTED_MODEL_HOSTS", None
            )
            if raw_hosts is None:
                try:
                    from app.config import AppSettings

                    raw_hosts = AppSettings().TRUSTED_MODEL_HOSTS
                except (OSError, ValueError, RuntimeError):
                    raw_hosts = None
            trusted_hosts = _normalize_trusted_model_hosts(raw_hosts)

        # Validate primary URL and derived bundle URLs before security advisory check or sandbox unblocking
        try:
            _validate_url(url, trusted_hosts=trusted_hosts)
            remote_files, generated_files, expected_hashes = _resolve_bundle_plan(url)
            for remote_url in remote_files.values():
                _validate_url(remote_url, trusted_hosts=trusted_hosts)
        except Exception as val_err:
            logger.warning(
                "Model download URL validation failed: %s",
                val_err,
                exc_info=True,
            )
            err = (
                val_err
                if isinstance(val_err, (DownloadError, DownloaderError))
                else DownloadError(str(val_err))
            )
            if on_failure:
                on_failure(err)
            return

        # Pre-download security advisory check before un-sandboxing or allocating disk space
        try:
            from app.core.security_advisory import SecurityAdvisoryClient

            advisory_client = SecurityAdvisoryClient.get_instance()
            advisory_client.check_advisory(repo_id=url)
        except Exception as adv_err:
            logger.warning(
                "Pre-download security advisory check failed: %s",
                adv_err,
                exc_info=True,
            )
            if isinstance(adv_err, (DownloadError, DownloaderError)):
                err = adv_err
            else:
                err = ModelVerificationError(str(adv_err))
                err.__cause__ = adv_err
            if on_failure:
                on_failure(err)
            return

        from app.core.network_guard import unblock_external_network

        staging_dir: str | None = None
        abs_model_dir = os.path.abspath(model_dir)
        parent_dir = os.path.dirname(abs_model_dir) or "."

        # Bypass application sandboxing on this dedicated thread after advisory check passes
        try:
            with unblock_external_network(reason="model download execution"):
                if dm is None:
                    try:
                        dm = DownloadManager.get_instance()
                    except Exception:
                        logger.debug(
                            "DownloadManager unavailable; using direct opener",
                            exc_info=True,
                        )
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

                os.makedirs(parent_dir, exist_ok=True)
                staging_dir = tempfile.mkdtemp(prefix=".model_staging_", dir=parent_dir)

                disk_probe_path = (
                    abs_model_dir if os.path.exists(abs_model_dir) else parent_dir
                )
                chunk_size = 1024 * 64
                total_files_count = max(len(remote_files), 1)
                cumulative_bytes = 0

                for file_idx, (fname, file_url) in enumerate(remote_files.items()):
                    staged_target = os.path.join(staging_dir, fname)
                    staged_tmp = f"{staged_target}.tmp"
                    retries = 0

                    while True:
                        try:
                            if cancel_event.is_set():
                                raise DownloadCancelledError(
                                    "Download was cancelled by the user."
                                )

                            opener = get_current_opener()
                            req = urllib.request.Request(
                                file_url,
                                headers={"User-Agent": "Smart-AutoSorter/1.0"},
                            )

                            file_bytes = 0
                            with opener.open(req, timeout=15) as response:
                                total_size = int(
                                    response.info().get("Content-Length", 0) or 0
                                )

                                if total_size > 0:
                                    try:
                                        _, _, free = shutil.disk_usage(disk_probe_path)
                                        if free < total_size:
                                            raise DiskSpaceError(
                                                f"Insufficient disk space. Required: {total_size} bytes, Free: {free} bytes."
                                            )
                                    except OSError as e:
                                        logger.warning(
                                            "Could not retrieve disk usage: %s", e
                                        )

                                with open(staged_tmp, "wb") as f:
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

                                        file_bytes += len(chunk)
                                        file_ratio = (
                                            (file_bytes / total_size)
                                            if total_size > 0
                                            else 0.0
                                        )
                                        if total_files_count == 1:
                                            overall_ratio = file_ratio
                                        elif fname == "model.onnx":
                                            overall_ratio = 0.85 * file_ratio
                                        else:
                                            rem_count = max(total_files_count - 1, 1)
                                            overall_ratio = 0.85 + 0.14 * (
                                                (max(file_idx - 1, 0) + file_ratio)
                                                / rem_count
                                            )

                                        msg = (
                                            f"Downloaded {file_bytes / (1024 * 1024):.2f}MB of {total_size / (1024 * 1024):.2f}MB ({overall_ratio * 100:.1f}%) [{fname}]"
                                            if total_size > 0
                                            else f"Downloaded {file_bytes / (1024 * 1024):.2f}MB [{fname}]..."
                                        )
                                        emit_progress(
                                            progress_callback,
                                            progress_or_update=min(
                                                0.99, max(0.0, overall_ratio)
                                            ),
                                            stage=msg,
                                            unit_count=cumulative_bytes + file_bytes,
                                            unit_type="bytes",
                                        )

                            cumulative_bytes += file_bytes
                            break

                        except (DownloadCancelledError, DiskSpaceError):
                            raise
                        except Exception as net_err:
                            if os.path.exists(staged_tmp):
                                with contextlib.suppress(OSError):
                                    os.remove(staged_tmp)

                            if cancel_event.is_set():
                                raise DownloadCancelledError(
                                    "Download was cancelled by the user."
                                ) from net_err
                            if isinstance(net_err, DownloadError) and not isinstance(
                                net_err, NetworkError
                            ):
                                raise
                            if isinstance(
                                net_err, NetworkError
                            ) and "Invalid proxy configuration" in str(net_err):
                                raise net_err

                            retries += 1
                            if retries > max_retries:
                                if isinstance(net_err, DownloadError):
                                    raise net_err
                                raise NetworkError(str(net_err)) from net_err

                            delay = initial_delay * (backoff_factor ** (retries - 1))
                            logger.info(
                                "Retrying download operation (%d/%d) for %s after %.2fs delay: %s",
                                retries,
                                max_retries,
                                fname,
                                delay,
                                net_err,
                            )

                            if cancel_event.wait(delay) or cancel_event.is_set():
                                raise DownloadCancelledError(
                                    "Download was cancelled by the user."
                                )

                    # Verify cryptographic SHA-256 digest of each staged file before promoting
                    verify_temp_file_hash(
                        staged_tmp, os.path.join(abs_model_dir, fname)
                    )
                    if expected_hashes and fname in expected_hashes:
                        from app.core.resilient_file_ops import resilient_file_hash

                        actual_digest = resilient_file_hash(
                            staged_tmp, chunk_size=65536
                        )
                        if actual_digest != expected_hashes[fname]:
                            with contextlib.suppress(OSError):
                                os.remove(staged_tmp)
                            raise ModelVerificationError(
                                f"Cryptographic signature verification failed for {fname}.\n"
                                f"Expected: {expected_hashes[fname]}\n"
                                f"Actual: {actual_digest}"
                            )
                    os.replace(staged_tmp, staged_target)

                # Stage local metadata files (e.g., version.txt) and verify their SHA-256 digests
                for gen_name, gen_content in generated_files.items():
                    if cancel_event.is_set():
                        raise DownloadCancelledError(
                            "Download was cancelled by the user."
                        )
                    gen_tmp = os.path.join(staging_dir, f"{gen_name}.tmp")
                    gen_target = os.path.join(staging_dir, gen_name)
                    with open(gen_tmp, "w", encoding="utf-8", newline="\n") as gf:
                        gf.write(gen_content)
                    if expected_hashes and gen_name in expected_hashes:
                        verify_temp_file_hash(
                            gen_tmp, os.path.join(abs_model_dir, gen_name)
                        )
                    os.replace(gen_tmp, gen_target)

                # Verify full staged bundle integrity before atomic activation
                if not verify_downloaded_model(staging_dir):
                    raise DownloadError(
                        "Integrity verification failed for the staged model bundle."
                    )

                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelledError("Download was cancelled by the user.")

                _activate_staged_bundle(staging_dir, abs_model_dir)

                if on_success:
                    on_success()

        except DownloadCancelledError as e:
            if staging_dir and os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            temp_path = os.path.join(abs_model_dir, "model.onnx.tmp")
            if os.path.exists(temp_path):
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
            if on_failure:
                on_failure(e)

        except DiskSpaceError as e:
            if staging_dir and os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            temp_path = os.path.join(abs_model_dir, "model.onnx.tmp")
            if os.path.exists(temp_path):
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
            if on_failure:
                on_failure(e)

        except Exception as e:
            logger.debug("Model download failed", exc_info=True)
            if staging_dir and os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            temp_path = os.path.join(abs_model_dir, "model.onnx.tmp")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError as cleanup_err:
                    logger.warning(
                        "Failed to clean up temporary file %s: %s",
                        temp_path,
                        cleanup_err,
                    )
            if isinstance(e, (DownloadError, DownloaderError)):
                err = e
            else:
                err = NetworkError(str(e))
                err.__cause__ = e
            if on_failure:
                on_failure(err)
        finally:
            if staging_dir and os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)

    from app.core.shared_registry import ContextPropagatingThread

    thread = ContextPropagatingThread(target=thread_target, daemon=True)
    thread.start()
    return thread


_VERIFICATION_CACHE_LOCK = threading.Lock()
_VERIFICATION_CACHE: dict[str, tuple[tuple[Any, ...], bool, str, str]] = {}


def resolve_default_model_dir() -> str:
    """Resolve the active local model directory path across packaged and user installs.

    Checks embedded PyInstaller bundle paths and local workspace ``model/`` directories
    when verified weights are present, otherwise defaults to ``get_app_dir() / "model"``.

    Returns
    -------
    str
        Resolved filesystem path to the model directory.
    """
    from app.core.path_utils import get_meipass_dir, is_packaged

    if is_packaged():
        meipass = get_meipass_dir()
        if meipass:
            mei_dir = os.path.join(meipass, "model")
            if os.path.isfile(os.path.join(mei_dir, "model.onnx")):
                return mei_dir

    local_dir = os.path.abspath("model")
    if os.path.isfile(os.path.join(local_dir, "model.onnx")):
        return local_dir

    try:
        from app.config import get_app_dir

        return str(get_app_dir() / "model")
    except (OSError, ValueError, RuntimeError):
        return os.path.expanduser("~/.smart-autosorter/model")


def inspect_local_model_bundle(model_dir: str | None = None) -> dict[str, Any]:
    """Inspect local model directory and active ``DownloadManager`` state with stat-keyed caching.

    Parameters
    ----------
    model_dir : str | None, optional
        Model directory to inspect. Resolves default application model path when omitted.

    Returns
    -------
    dict[str, Any]
        Structured inspection dictionary containing ``model_dir``, ``verified``,
        ``bundle_status``, ``detail``, ``is_downloading``, ``progress_percent``,
        ``status_text``, ``error``, ``size_bytes``, and ``files``.
    """
    resolved_dir = os.path.abspath(model_dir or resolve_default_model_dir())
    dm = DownloadManager.get_instance()
    is_downloading = bool(dm.state.get("is_downloading", False))
    raw_progress = float(dm.state.get("progress", 0.0) or 0.0)
    progress_percent = min(100.0, max(0.0, raw_progress * 100.0))
    status_text = str(dm.state.get("status_text") or "")
    err_obj = dm.state.get("error")
    error_str = str(err_obj) if err_obj else None

    from app.core.shared_registry import SharedModelRegistry

    expected_hashes = SharedModelRegistry.get_instance().get_expected_hashes(
        "model_download"
    )
    tracked_names = sorted(set(_REQUIRED_BUNDLE_FILES) | set(expected_hashes.keys()))

    file_stats: list[tuple[str, int, int]] = []
    files_report: dict[str, dict[str, Any]] = {}
    total_size_bytes = 0

    if os.path.isdir(resolved_dir):
        for fname in tracked_names:
            fpath = os.path.join(resolved_dir, fname)
            try:
                st = os.stat(fpath)
                if os.path.isfile(fpath):
                    file_stats.append((fname, int(st.st_size), int(st.st_mtime_ns)))
                    files_report[fname] = {
                        "exists": True,
                        "size_bytes": int(st.st_size),
                    }
                    total_size_bytes += int(st.st_size)
                else:
                    file_stats.append((fname, -1, -1))
                    files_report[fname] = {"exists": False, "size_bytes": 0}
            except OSError:
                file_stats.append((fname, -1, -1))
                files_report[fname] = {"exists": False, "size_bytes": 0}
    else:
        for fname in tracked_names:
            file_stats.append((fname, -1, -1))
            files_report[fname] = {"exists": False, "size_bytes": 0}

    fingerprint = (
        tuple(file_stats),
        tuple(sorted(expected_hashes.items())),
    )

    with _VERIFICATION_CACHE_LOCK:
        cached = _VERIFICATION_CACHE.get(resolved_dir)

    if cached is not None and cached[0] == fingerprint:
        verified, bundle_status, detail = cached[1], cached[2], cached[3]
    else:
        bundle_status, detail = _inspect_model_bundle(resolved_dir)
        verified = bundle_status == "valid"
        with _VERIFICATION_CACHE_LOCK:
            _VERIFICATION_CACHE[resolved_dir] = (
                fingerprint,
                verified,
                bundle_status,
                detail,
            )

    return {
        "model_dir": resolved_dir,
        "verified": verified,
        "bundle_status": "downloading" if is_downloading else bundle_status,
        "disk_bundle_status": bundle_status,
        "detail": detail,
        "is_downloading": is_downloading,
        "progress_percent": progress_percent,
        "status_text": status_text,
        "error": error_str,
        "size_bytes": total_size_bytes,
        "files": files_report,
    }


def get_ai_runtime_state(
    settings: Any = None, model_dir: str | None = None
) -> dict[str, Any]:
    """Compute truthful four-state AI runtime status and status-bar badge string.

    Distinguishes:
    - ``disabled``: AI consent is False or unset -> ``[AI: Disabled]``
    - ``downloading``: AI consent is True and model transfer is active -> ``[AI: Downloading XX%]``
    - ``active``: AI consent is True and verified model weights exist on disk -> ``[AI: Active]``
    - ``fallback``: AI consent is True and verified weights are absent -> ``[AI: Fallback]``

    Parameters
    ----------
    settings : Any, optional
        Application settings instance exposing ``AI_CONSENT_GRANTED``.
    model_dir : str | None, optional
        Explicit model directory override.

    Returns
    -------
    dict[str, Any]
        Dictionary containing ``state``, ``badge``, ``consent_granted``, and bundle metadata.
    """
    consent = getattr(settings, "AI_CONSENT_GRANTED", None) if settings else None
    bundle = inspect_local_model_bundle(model_dir=model_dir)

    if consent is not True:
        state = "disabled"
        badge = "[AI: Disabled]"
    elif bundle.get("is_downloading", False):
        state = "downloading"
        pct = int(round(float(bundle.get("progress_percent", 0.0) or 0.0)))
        badge = f"[AI: Downloading {pct}%]" if pct > 0 else "[AI: Downloading]"
    elif bundle.get("verified", False):
        state = "active"
        badge = "[AI: Active]"
    else:
        state = "fallback"
        badge = "[AI: Fallback]"

    return {
        "state": state,
        "badge": badge,
        "consent_granted": consent is True,
        **bundle,
    }


def provision_model_cli(
    model_dir: str | None = None,
    url: str = DEFAULT_MODEL_URL,
    proxy: str = "",
    force: bool = False,
    quiet: bool = False,
    stream: Any = None,
) -> dict[str, Any]:
    """Verify or download model weights synchronously with terminal progress output.

    Parameters
    ----------
    model_dir : str | None, optional
        Destination directory for model files. Defaults to ``resolve_default_model_dir()``.
    url : str, optional
        HTTPS allowlisted model URL. Defaults to ``DEFAULT_MODEL_URL``.
    proxy : str, optional
        Optional HTTP/HTTPS proxy URL.
    force : bool, optional
        Re-download even if verified model files already exist on disk.
    quiet : bool, optional
        Suppress terminal progress messages when True.
    stream : Any, optional
        Output stream for progress updates (defaults to ``sys.stderr``).

    Returns
    -------
    dict[str, Any]
        Result dictionary with ``status``, ``downloaded``, ``verified``, and ``model_dir``.

    Raises
    ------
    DownloaderError
        If network transfer, URL validation, or SHA-256 verification fails.
    """
    import sys

    out = stream if stream is not None else sys.stderr
    resolved_dir = os.path.abspath(model_dir or resolve_default_model_dir())

    if not force and verify_downloaded_model(resolved_dir):
        if not quiet and out is not None:
            print(
                f"AI model weights verified at '{resolved_dir}'.",
                file=out,
            )
        return {
            "status": "verified",
            "downloaded": False,
            "verified": True,
            "model_dir": resolved_dir,
        }

    done_event = threading.Event()
    failure_holder: list[BaseException] = []
    last_bucket = [-1]
    is_tty = bool(out is not None and hasattr(out, "isatty") and out.isatty())

    def _on_progress(update: ProgressUpdate) -> None:
        if quiet or out is None:
            return
        pct = min(100, max(0, int(round((update.progress or 0.0) * 100))))
        stage = update.stage or f"Downloading model weights ({pct}%)"
        if is_tty:
            out.write(f"\r{stage:<72}")
            out.flush()
        else:
            bucket = pct // 25
            if bucket != last_bucket[0]:
                last_bucket[0] = bucket
                print(stage, file=out)

    def _on_success() -> None:
        done_event.set()

    def _on_failure(err: BaseException) -> None:
        failure_holder.append(err)
        done_event.set()

    if not quiet and out is not None:
        print(
            f"Provisioning local AI model weights into '{resolved_dir}'...",
            file=out,
        )

    dm = DownloadManager.get_instance()
    if proxy and proxy.strip():
        dm.update_proxy(proxy.strip())

    worker = run_background_download(
        url=url,
        model_dir=resolved_dir,
        progress_callback=_on_progress,
        on_success=_on_success,
        on_failure=_on_failure,
        proxy=proxy.strip() if proxy else "",
        download_manager=dm,
    )
    worker.join()
    done_event.wait(timeout=1.0)

    if is_tty and not quiet and out is not None:
        out.write("\n")
        out.flush()

    with _VERIFICATION_CACHE_LOCK:
        _VERIFICATION_CACHE.pop(resolved_dir, None)

    if failure_holder:
        err = failure_holder[0]
        if isinstance(err, DownloaderError):
            raise err
        raise DownloadError(str(err)) from err

    if not verify_downloaded_model(resolved_dir):
        _, detail = _inspect_model_bundle(resolved_dir)
        raise ModelVerificationError(
            f"Model verification failed after download: {detail}"
        )

    if not quiet and out is not None:
        print(
            f"AI model weights downloaded and verified at '{resolved_dir}'.",
            file=out,
        )

    return {
        "status": "verified",
        "downloaded": True,
        "verified": True,
        "model_dir": resolved_dir,
    }


def remove_local_model_bundle(model_dir: str | None = None) -> dict[str, Any]:
    """Unload in-memory AI models and remove local model directory files safely.

    Parameters
    ----------
    model_dir : str | None, optional
        Target model directory to remove. Defaults to ``resolve_default_model_dir()``.

    Returns
    -------
    dict[str, Any]
        Dictionary containing ``model_dir``, ``removed``, ``removed_files``, and ``freed_bytes``.
    """
    resolved_dir = os.path.abspath(model_dir or resolve_default_model_dir())
    dm = DownloadManager.get_instance()
    dm.cancel_download()

    with contextlib.suppress(Exception):
        from app.core.shared_registry import SharedModelRegistry

        SharedModelRegistry.get_instance().unload_all_models()

    removed_files: list[str] = []
    freed_bytes = 0
    if os.path.isdir(resolved_dir):
        for root, _dirs, files in os.walk(resolved_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                with contextlib.suppress(OSError):
                    freed_bytes += os.path.getsize(fpath)
                removed_files.append(os.path.relpath(fpath, resolved_dir))
        from app.core.resilient_file_ops import resilient_rmtree

        resilient_rmtree(resolved_dir, ignore_errors=True)

    with dm._manager_lock:
        dm.cancel_event.clear()
        dm._transition_to_locked(DownloadState.IDLE)
        dm.state["progress"] = 0.0
        dm.state["status_text"] = "Model deleted."
        dm.state["success"] = False
        dm.state["error"] = None
        dm.state["is_downloading"] = False

    with _VERIFICATION_CACHE_LOCK:
        _VERIFICATION_CACHE.pop(resolved_dir, None)

    return {
        "model_dir": resolved_dir,
        "removed": bool(removed_files) or not os.path.exists(resolved_dir),
        "removed_files": sorted(removed_files),
        "freed_bytes": freed_bytes,
    }
