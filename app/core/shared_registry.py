"""Shared Model Registry and Worker Pool module.

Provides a centralized model registry to prevent duplicate model weight loads,
and a single global worker pool to manage text extraction and analysis tasks
with offline enforcement and thread limits.

The context-propagating thread helpers live in ``app.core.concurrency`` and the
network sandbox in ``app.core.network_guard``; both are re-exported here for
existing callers.
"""

import concurrent.futures
import logging
import os
import threading
from contextlib import contextmanager
from typing import Callable

from app.core.concurrency import (  # noqa: F401
    ContextPropagatingThread,
    ContextPropagatingThreadPoolExecutor,
    ContextVarLocal,
)
from app.core.network_guard import (  # noqa: F401
    _is_local_address,
    _thread_local,
    block_external_network,
    safe_connect,
    safe_connect_ex,
    safe_getaddrinfo,
    safe_getfqdn,
    safe_gethostbyaddr,
    safe_gethostbyname,
    safe_gethostbyname_ex,
    safe_getnameinfo,
)
from app.core.network_guard import install as apply_global_socket_sandbox


class SharedModelRegistry:
    """Centralized registry for caching heavy model references (e.g. generative model, EasyOCR reader)."""

    _instance = None

    @classmethod
    def get_instance(cls):
        """Retrieve the singleton instance of SharedModelRegistry."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        apply_global_socket_sandbox()
        self._lock = threading.RLock()
        self._per_model_locks = {}
        self._models = {}
        self._expected_hashes = {}
        self._model_health = {}
        self._cached_settings = None
        self.apply_onnx_thread_limits()
        try:
            from app.core.hashes_registry import HASHES

            for model_id, file_hashes in HASHES.items():
                self.register_expected_hashes(model_id, file_hashes)
        except ImportError:
            pass

    def _get_model_lock(self, model_id: str) -> threading.Lock:
        """Get or create a per-model initialization lock."""
        with self._lock:
            if model_id not in self._per_model_locks:
                self._per_model_locks[model_id] = threading.Lock()
            return self._per_model_locks[model_id]

    def record_model_health(
        self,
        model_id: str,
        status: str,
        failure_reason: str | None = None,
        suggested_recovery_action: str | None = None,
    ) -> dict:
        """Record diagnostic health status for a model in the shared registry."""
        import time

        with self._lock:
            record = {
                "model_id": model_id,
                "status": status,
                "failure_reason": failure_reason,
                "suggested_recovery_action": suggested_recovery_action,
                "timestamp": time.time(),
            }
            self._model_health[model_id] = record
            return record

    def get_model_health(self, model_id: str) -> dict:
        """Retrieve diagnostic health status for a specific model."""
        import time

        with self._lock:
            if model_id in self._model_health:
                return dict(self._model_health[model_id])
            return {
                "model_id": model_id,
                "status": "UNINITIALIZED",
                "failure_reason": None,
                "suggested_recovery_action": None,
                "timestamp": time.time(),
            }

    def get_all_model_health(self) -> dict:
        """Retrieve diagnostic health status mapping for all registered models."""
        with self._lock:
            return {k: dict(v) for k, v in self._model_health.items()}

    def get_thread_limit(self) -> int:
        """Get the current thread limit from configuration, falling back to 2."""
        try:
            if getattr(self, "_cached_settings", None) is not None:
                settings = self._cached_settings
            else:
                from app.config import AppSettings

                settings = AppSettings()
                from unittest.mock import Mock

                if not isinstance(settings, Mock) and not isinstance(AppSettings, Mock):
                    self._cached_settings = settings
            limit = getattr(settings, "MODEL_THREADS", 2)
            if not isinstance(limit, int) or limit < 1 or limit > 32:
                return 2
            return limit
        except Exception:
            return 2

    def apply_onnx_thread_limits(self):
        """Apply thread limits dynamically to all initialized ONNX runtime sessions."""
        try:
            import onnxruntime as ort

            if not hasattr(ort.InferenceSession, "_original_init"):
                original_init = ort.InferenceSession.__init__
                ort.InferenceSession._original_init = original_init

                registry_instance = self

                def wrapped_init(sess, model_path, sess_options=None, *args, **kwargs):
                    thread_limit = registry_instance.get_thread_limit()
                    if sess_options is None:
                        sess_options = ort.SessionOptions()
                    sess_options.intra_op_num_threads = thread_limit
                    sess_options.inter_op_num_threads = thread_limit
                    original_init(sess, model_path, sess_options, *args, **kwargs)

                ort.InferenceSession.__init__ = wrapped_init
        except ImportError:
            pass

    def get_onnx_session(self, model_path: str, sess_options=None):
        """Lazily load and return an ONNX InferenceSession with configured thread limits."""
        model_id = f"onnx_{model_path}"
        if model_id in self._models and self._models[model_id] is not None:
            return self._models[model_id]

        model_lock = self._get_model_lock(model_id)
        with model_lock:
            if model_id in self._models and self._models[model_id] is not None:
                return self._models[model_id]

            import onnxruntime as ort

            if sess_options is None:
                sess_options = ort.SessionOptions()

            thread_limit = self.get_thread_limit()
            sess_options.intra_op_num_threads = thread_limit
            sess_options.inter_op_num_threads = thread_limit

            session = ort.InferenceSession(model_path, sess_options)
            with self._lock:
                self._models[model_id] = session
            return session

    def register_expected_hashes(self, model_id: str, hashes: dict[str, str]):
        """Register expected SHA-256 hashes for files of a model."""
        with self._lock:
            self._expected_hashes[model_id] = hashes

    def get_expected_hashes(self, category: str | None = None) -> dict:
        """Retrieve expected hashes for a specific category or all registered categories."""
        with self._lock:
            if category is None:
                return {k: dict(v) for k, v in self._expected_hashes.items()}
            if category in self._expected_hashes:
                return dict(self._expected_hashes[category])
            return {}

    def get_expected_hashes_for_file(self, filename: str) -> list[str]:
        """Search across all registered categories to retrieve all expected SHA-256 hashes for a filename."""
        with self._lock:
            expected_hashes = []
            for cat_hashes in self._expected_hashes.values():
                if filename in cat_hashes:
                    h = cat_hashes[filename]
                    if h not in expected_hashes:
                        expected_hashes.append(h)
            return expected_hashes

    def get_expected_hash(
        self, filename: str, category: str | None = None
    ) -> str | None:
        """Retrieve the expected SHA-256 hash for a filename, optionally scoped to a category."""
        with self._lock:
            if category is not None:
                return self._expected_hashes.get(category, {}).get(filename)
            for cat_hashes in self._expected_hashes.values():
                if filename in cat_hashes:
                    return cat_hashes[filename]
            return None

    def has_expected_hash_for_file(self, filename: str) -> bool:
        """Check if any registered category contains an expected hash for the given filename."""
        return len(self.get_expected_hashes_for_file(filename)) > 0

    def verify_integrity(self, model_id: str, model_path: str) -> bool:
        """Verify model files against expected hashes if they are registered."""
        from app.core.exceptions import HashVerificationError

        with self._lock:
            expected = self._expected_hashes.get(model_id)

        if expected:
            from app.core.path_utils import is_packaged

            if not model_path or not os.path.exists(model_path):
                if not is_packaged():
                    logging.warning(
                        f"Model path {model_path} does not exist. Skipping integrity check in non-packaged mode."
                    )
                    return True
                reason = f"Model path {model_path} does not exist for integrity check."
                self.record_model_health(
                    model_id,
                    status="FAILED",
                    failure_reason=reason,
                    suggested_recovery_action="RE_DOWNLOAD_MODEL",
                )
                raise FileNotFoundError(reason)

            if os.path.isdir(model_path):
                for filename, expected_hash in expected.items():
                    file_path = os.path.join(model_path, filename)
                    if not os.path.exists(file_path):
                        # A missing registered file is a failure in every mode:
                        # returning early here used to skip the remaining files.
                        reason = f"Required model file {file_path} is missing."
                        self.record_model_health(
                            model_id,
                            status="FAILED",
                            failure_reason=reason,
                            suggested_recovery_action="RE_DOWNLOAD_MODEL",
                        )
                        raise FileNotFoundError(reason)

                    from app.core.resilient_file_ops import resilient_file_hash

                    actual_hash = resilient_file_hash(file_path)
                    if actual_hash != expected_hash:
                        reason = f"Integrity check failed for {filename}. Expected {expected_hash}, got {actual_hash}"
                        self.record_model_health(
                            model_id,
                            status="FAILED",
                            failure_reason=reason,
                            suggested_recovery_action="RE_DOWNLOAD_MODEL",
                        )
                        raise HashVerificationError(reason)
            else:
                # Single file
                from app.core.resilient_file_ops import resilient_file_hash

                actual_hash = resilient_file_hash(model_path)
                expected_hash = (
                    expected.get(os.path.basename(model_path))
                    or list(expected.values())[0]
                )
                if actual_hash != expected_hash:
                    reason = (
                        f"Integrity check failed for {os.path.basename(model_path)}. "
                        f"Expected {expected_hash}, got {actual_hash}"
                    )
                    self.record_model_health(
                        model_id,
                        status="FAILED",
                        failure_reason=reason,
                        suggested_recovery_action="RE_DOWNLOAD_MODEL",
                    )
                    raise HashVerificationError(reason)

        self.record_model_health(model_id, status="HEALTHY")
        return True

    def get_ocr_reader(self):
        """Lazily load and return the EasyOCR Reader from registry."""
        model_id = "easyocr"

        # Resolve easyocr_dir
        try:
            from app.core.offline_loader import OfflineModelLoader

            easyocr_dir = OfflineModelLoader.resolve_model_path("easyocr")
        except Exception as e:
            from app.core.path_utils import is_packaged

            # A packaged build only loads weights that passed verification
            # (#722); falling back here would pick up the refused folder.
            if is_packaged():
                logging.error(f"OCR is unavailable: no verified EasyOCR model: {e}")
                return None
            try:
                from app.core.path_utils import get_base_path

                base_path = get_base_path(__file__)
            except Exception:
                base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            # The working directory is deliberately not searched: it is often
            # a folder of untrusted files that this app is sorting.
            local_easyocr_path = os.path.join(base_path, "offline_bundle", "easyocr")
            easyocr_path = os.environ.get("EASYOCR_MODULE_PATH")
            if os.path.exists(local_easyocr_path):
                easyocr_dir = local_easyocr_path
            elif easyocr_path:
                easyocr_dir = os.path.join(easyocr_path, "model")
            else:
                easyocr_dir = os.path.expanduser("~/.EasyOCR/model")

        # Get settings
        from app.config import AppSettings

        settings = getattr(self, "_cached_settings", None) or AppSettings()

        # Safely retrieve OCR_LANGUAGES and OCR_GPU_ENABLED, handling mocks/missing attributes
        raw_langs = getattr(settings, "OCR_LANGUAGES", "en")
        if not isinstance(raw_langs, str):
            raw_langs = "en"

        langs = [lang.strip() for lang in raw_langs.split(",") if lang.strip()]
        if not langs:
            langs = ["en"]

        ocr_gpu_enabled = getattr(settings, "OCR_GPU_ENABLED", False)
        if not isinstance(ocr_gpu_enabled, bool):
            ocr_gpu_enabled = False

        if ocr_gpu_enabled:
            from app.core.env_helper import is_cuda_available, is_mps_available

            use_gpu = bool(is_cuda_available() or is_mps_available())
        else:
            use_gpu = False

        current_reader_info = self._models.get("easyocr_info")
        reader = self._models.get(model_id)
        if reader is not None and current_reader_info == (langs, use_gpu):
            return reader

        model_lock = self._get_model_lock(model_id)
        with model_lock:
            current_reader_info = self._models.get("easyocr_info")
            reader = self._models.get(model_id)
            if reader is not None and current_reader_info == (langs, use_gpu):
                return reader

            # Check integrity if expected hashes are registered
            if model_id in self._expected_hashes:
                self.verify_integrity(model_id, easyocr_dir)

            try:
                import easyocr
                import torch

                torch.set_num_threads(self.get_thread_limit())
                logging.info(
                    f"Initializing EasyOCR reader with languages {langs} and gpu={use_gpu}"
                )
                reader = easyocr.Reader(
                    langs,
                    gpu=use_gpu,
                    model_storage_directory=easyocr_dir,
                    download_enabled=False,
                )
                info = (langs, use_gpu)
            except Exception as e:
                logging.error(
                    f"Failed to load EasyOCR reader with languages {langs} and gpu={use_gpu}: {e}. Falling back to 'en' and cpu."
                )
                try:
                    import easyocr
                    import torch

                    torch.set_num_threads(self.get_thread_limit())
                    reader = easyocr.Reader(
                        ["en"],
                        gpu=False,
                        model_storage_directory=easyocr_dir,
                        download_enabled=False,
                    )
                    info = (["en"], False)
                except Exception as ex:
                    logging.critical(
                        f"Critical: Fallback EasyOCR initialization failed: {ex}"
                    )
                    reader = None
                    info = (None, None)

            with self._lock:
                self._models[model_id] = reader
                self._models["easyocr_info"] = info

            return reader

    def get_generative_model(self, model_path: str):
        """Lazily load and return the generative naming model from registry."""
        model_id = "generative_naming"
        if model_id in self._models and self._models[model_id] is not None:
            return self._models[model_id]

        model_lock = self._get_model_lock(model_id)
        with model_lock:
            if model_id in self._models and self._models[model_id] is not None:
                return self._models[model_id]

            if not model_path or not os.path.exists(model_path):
                logging.warning("Offline model bundle path not found.")
                return None, None, None

            # Models loaded by the shared registry successfully pass SHA-256 integrity checks before execution [cite:cf_009]
            self.verify_integrity(model_id, model_path)

            try:
                # Use block_external_network to ensure offline execution boundaries
                with block_external_network():
                    import torch
                    from transformers import (
                        AutoModelForCausalLM,
                        AutoModelForSeq2SeqLM,
                        AutoTokenizer,
                        pipeline,
                    )

                    torch.set_num_threads(self.get_thread_limit())

                    tokenizer = AutoTokenizer.from_pretrained(
                        model_path, local_files_only=True
                    )
                    try:
                        model = AutoModelForSeq2SeqLM.from_pretrained(
                            model_path, local_files_only=True
                        )
                        task = "text2text-generation"
                    except Exception:
                        model = AutoModelForCausalLM.from_pretrained(
                            model_path, local_files_only=True
                        )
                        task = "text-generation"

                    quantized_model = torch.quantization.quantize_dynamic(
                        model, {torch.nn.Linear}, dtype=torch.qint8
                    )

                    generator = pipeline(
                        task, model=quantized_model, tokenizer=tokenizer, device=-1
                    )

                    gen_tuple = (generator, task, tokenizer)
                    with self._lock:
                        self._models[model_id] = gen_tuple
            except Exception as e:
                logging.error(f"Failed to load generative model in registry: {e}")
                raise e

            return self._models.get(model_id, (None, None, None))

    def get_jev_classifier(self):
        """Lazily load and return the cached singleton JevClassifierEngine instance."""
        model_id = "jev_classifier"
        if model_id in self._models and self._models[model_id] is not None:
            return self._models[model_id]

        model_lock = self._get_model_lock(model_id)
        with model_lock:
            if model_id in self._models and self._models[model_id] is not None:
                return self._models[model_id]

            from app.core.jev_classifier import JevClassifierEngine

            engine = JevClassifierEngine()
            with self._lock:
                self._models[model_id] = engine
            return engine

    def get_florence_processor(self):
        """Lazily load and return the Florence-2 visual processor wrapper from registry."""
        model_id = "florence-2"
        if model_id in self._models and self._models[model_id] is not None:
            return self._models[model_id]

        model_lock = self._get_model_lock(model_id)
        with model_lock:
            if model_id in self._models and self._models[model_id] is not None:
                return self._models[model_id]

            from app.core.offline_loader import Florence2VisualProcessor

            processor = Florence2VisualProcessor(model_id=model_id)
            processor.load()
            with self._lock:
                self._models[model_id] = processor
            return processor

    def _free_memory(self):
        """Drop object references, run garbage collection, and clear hardware acceleration buffers."""
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, "ipc_collect"):
                    torch.cuda.ipc_collect()
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                if hasattr(torch.mps, "empty_cache"):
                    torch.mps.empty_cache()
        except Exception:
            pass

    def is_model_loaded(self, model_id: str) -> bool:
        """Check if a specific model or model type is currently loaded in memory."""
        with self._lock:
            if model_id in self._models and self._models[model_id] is not None:
                return True
            if model_id == "onnx":
                return any(
                    (k.startswith("onnx_") or k.startswith("tokenizer_"))
                    and v is not None
                    for k, v in self._models.items()
                )
            if model_id == "easyocr":
                return "easyocr" in self._models and self._models["easyocr"] is not None
            return False

    def unload_model(self, model_id: str) -> bool:
        """Explicitly unload a specific machine learning model from active memory in a thread-safe manner.

        Drops object references, runs garbage collection, and clears hardware acceleration buffers.
        """
        with self._lock:
            keys_to_remove = []

            if model_id == "easyocr":
                keys_to_remove = ["easyocr", "easyocr_info"]
            elif model_id == "onnx":
                keys_to_remove = [
                    k
                    for k in set(self._models.keys())
                    | set(self._per_model_locks.keys())
                    if k.startswith("onnx_") or k.startswith("tokenizer_")
                ]
            else:
                keys_to_remove = [
                    k
                    for k in set(self._models.keys())
                    | set(self._per_model_locks.keys())
                    if k == model_id or k.startswith(f"{model_id}_")
                ]

            lock_keys = [k for k in keys_to_remove if k != "easyocr_info"]

        unloaded = False
        for lock_key in lock_keys:
            lock = self._get_model_lock(lock_key)
            with lock:
                with self._lock:
                    obj = self._models.pop(lock_key, None)
                    if lock_key == "easyocr":
                        self._models.pop("easyocr_info", None)
                    self._per_model_locks.pop(lock_key, None)

                if obj is not None:
                    unloaded = True
                    if hasattr(obj, "unload") and callable(getattr(obj, "unload")):
                        try:
                            obj.unload()
                        except Exception:
                            pass

        if unloaded:
            self._free_memory()

        return unloaded

    def unload_all_models(self) -> None:
        """Explicitly unload all machine learning models from active memory in a thread-safe manner."""
        with self._lock:
            lock_keys = [
                k
                for k in set(self._models.keys()) | set(self._per_model_locks.keys())
                if k != "easyocr_info"
            ]

        for lock_key in lock_keys:
            lock = self._get_model_lock(lock_key)
            with lock:
                with self._lock:
                    obj = self._models.pop(lock_key, None)
                    self._per_model_locks.pop(lock_key, None)

                if obj is not None:
                    if hasattr(obj, "unload") and callable(getattr(obj, "unload")):
                        try:
                            obj.unload()
                        except Exception:
                            pass

        with self._lock:
            self._models.clear()
            self._per_model_locks.clear()

        self._free_memory()


class SharedWorkerPool:
    """Global background task worker pool restricting concurrency and enforcing offline boundaries."""

    _instance = None

    @classmethod
    def get_instance(cls, max_workers=None):
        """Retrieve the singleton instance of SharedWorkerPool, initializing it if necessary."""
        if cls._instance is None:
            # Respect system limits / CPU counts to prevent starvation
            if max_workers is None:
                max_workers = min(4, os.cpu_count() or 2)
            cls._instance = cls(max_workers=max_workers)
        return cls._instance

    def __init__(self, max_workers: int):
        apply_global_socket_sandbox()
        self._executor = ContextPropagatingThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="GlobalSharedWorker",
            initializer=apply_global_socket_sandbox,
        )
        self.max_workers = max_workers

    def submit(self, fn, *args, **kwargs):
        """Submit a task to the pool, ensuring offline boundaries are enforced."""
        parent_sandboxed = getattr(_thread_local, "sandboxed", None)
        parent_reason = getattr(_thread_local, "reason", "worker execution")

        def offline_wrapped_fn(*a, **kw):
            is_sandboxed = True
            reason = parent_reason if parent_sandboxed is True else "worker execution"

            was_sandboxed = getattr(_thread_local, "sandboxed", False)
            old_reason = getattr(_thread_local, "reason", "worker execution")
            was_in_pool = getattr(_thread_local, "in_shared_worker_pool", False)

            _thread_local.sandboxed = is_sandboxed
            _thread_local.reason = reason
            _thread_local.in_shared_worker_pool = True
            try:
                return fn(*a, **kw)
            finally:
                _thread_local.sandboxed = was_sandboxed
                _thread_local.reason = old_reason
                _thread_local.in_shared_worker_pool = was_in_pool

        if (
            getattr(_thread_local, "in_shared_worker_pool", False)
            or threading.current_thread().name.startswith("GlobalSharedWorker")
            or threading.current_thread().name.startswith("DBWorker")
        ):
            fut = concurrent.futures.Future()
            try:
                res = offline_wrapped_fn(*args, **kwargs)
                fut.set_result(res)
            except Exception as exc:
                fut.set_exception(exc)
            return fut

        return self._executor.submit(offline_wrapped_fn, *args, **kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        """Map a function over iterables using the shared worker pool, enforcing offline boundaries."""
        parent_sandboxed = getattr(_thread_local, "sandboxed", None)
        parent_reason = getattr(_thread_local, "reason", "worker execution")

        def offline_wrapped_fn(*a, **kw):
            is_sandboxed = True
            reason = parent_reason if parent_sandboxed is True else "worker execution"

            was_sandboxed = getattr(_thread_local, "sandboxed", False)
            old_reason = getattr(_thread_local, "reason", "worker execution")
            was_in_pool = getattr(_thread_local, "in_shared_worker_pool", False)

            _thread_local.sandboxed = is_sandboxed
            _thread_local.reason = reason
            _thread_local.in_shared_worker_pool = True
            try:
                return fn(*a, **kw)
            finally:
                _thread_local.sandboxed = was_sandboxed
                _thread_local.reason = old_reason
                _thread_local.in_shared_worker_pool = was_in_pool

        if (
            getattr(_thread_local, "in_shared_worker_pool", False)
            or threading.current_thread().name.startswith("GlobalSharedWorker")
            or threading.current_thread().name.startswith("DBWorker")
        ):
            return [offline_wrapped_fn(*args) for args in zip(*iterables)]

        return self._executor.map(
            offline_wrapped_fn, *iterables, timeout=timeout, chunksize=chunksize
        )

    def shutdown(self, wait=False, cancel_futures=True):
        """Shutdown the underlying executor and reset singleton instance."""
        if hasattr(self, "_executor") and self._executor:
            try:
                self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            except TypeError:
                self._executor.shutdown(wait=wait)
            except Exception:
                pass
        SharedWorkerPool._instance = None

    @classmethod
    def shutdown_instance(cls, wait=False):
        """Shutdown the global SharedWorkerPool singleton instance if it exists."""
        if cls._instance is not None:
            try:
                cls._instance._executor.shutdown(wait=wait, cancel_futures=True)
            except Exception:
                pass
            cls._instance = None


class AudioConcurrencyGuard:
    """Concurrency guard restricting active audio transcription processes across worker threads."""

    _instance = None
    _class_lock = threading.Lock()

    @classmethod
    def get_instance(cls, limit: int | None = None):
        """Retrieve the singleton instance of AudioConcurrencyGuard, updating limit if specified."""
        with cls._class_lock:
            if cls._instance is None:
                if limit is None:
                    limit = 2
                cls._instance = cls(limit=limit)
            elif limit is not None:
                cls._instance.set_limit(limit)
            return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance (useful for testing)."""
        with cls._class_lock:
            cls._instance = None

    def __init__(self, limit: int = 2):
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._limit = max(1, int(limit))
        self._active_count = 0

    @property
    def limit(self) -> int:
        """Get the configured maximum concurrent audio tasks limit."""
        with self._lock:
            return self._limit

    @property
    def active_count(self) -> int:
        """Get the current count of active audio tasks."""
        with self._lock:
            return self._active_count

    def set_limit(self, new_limit: int):
        """Update the maximum concurrent audio tasks limit and notify waiting threads."""
        with self._lock:
            self._limit = max(1, int(new_limit))
            self._cond.notify_all()

    def acquire_slot(
        self,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 0.05,
    ) -> bool:
        """Acquire an audio execution slot.

        Returns True if acquired, or False if cancellation was requested before acquisition.
        """
        with self._lock:
            if cancel_check and cancel_check():
                return False
            while self._active_count >= self._limit:
                self._cond.wait(timeout=poll_interval)
                if cancel_check and cancel_check():
                    return False
            self._active_count += 1
            return True

    def release_slot(self):
        """Release an audio execution slot and notify waiting tasks."""
        with self._lock:
            if self._active_count > 0:
                self._active_count -= 1
            self._cond.notify_all()

    @contextmanager
    def guard(
        self,
        cancel_check: Callable[[], bool] | None = None,
        poll_interval: float = 0.05,
    ):
        """Context manager for acquiring and releasing an audio execution slot.

        Yields True if slot was acquired, or False if cancelled before acquiring.
        """
        acquired = self.acquire_slot(
            cancel_check=cancel_check, poll_interval=poll_interval
        )
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            self.release_slot()
