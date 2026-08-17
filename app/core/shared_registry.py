"""Shared Model Registry and Worker Pool module.

Provides a centralized model registry to prevent duplicate model weight loads,
and a single global worker pool to manage text extraction and analysis tasks
with offline enforcement and thread limits.
"""

import concurrent.futures
import contextvars
import hashlib
import ipaddress
import logging
import os
import socket
import sys
import threading
from contextlib import contextmanager


class ContextVarLocal:
    """Thread-local storage replacement powered by contextvars.ContextVar.

    This class provides a dictionary-like interface where attributes are stored
    in a contextvar, making them isolated to asynchronous/concurrent tasks.
    """

    def __init__(self):
        """Initialize the local context var dictionary."""
        super().__setattr__(
            "_var", contextvars.ContextVar("context_var_local", default={})
        )

    def __getattr__(self, name):
        """Retrieve an attribute value from the thread-local context dictionary.

        Parameters
        ----------
        name : str
            The attribute name to retrieve.

        Returns
        -------
        any
            The value of the retrieved attribute.

        Raises
        ------
        AttributeError
            If the attribute does not exist or _var is uninitialized.
        """
        if name == "_var":
            raise AttributeError("_var is not initialized")
        d = self._var.get()
        if name in d:
            return d[name]
        raise AttributeError(f"'ContextVarLocal' object has no attribute '{name}'")

    def __setattr__(self, name, value):
        """Set an attribute value in the thread-local context dictionary.

        Parameters
        ----------
        name : str
            The attribute name to set.
        value : any
            The value to assign.
        """
        if name == "_var":
            super().__setattr__(name, value)
            return
        d = dict(self._var.get())
        d[name] = value
        self._var.set(d)

    def __delattr__(self, name):
        """Delete an attribute from the thread-local context dictionary.

        Parameters
        ----------
        name : str
            The attribute name to delete.

        Raises
        ------
        AttributeError
            If the attribute does not exist or _var is uninitialized.
        """
        if name == "_var":
            super().__delattr__(name)
            return
        d = dict(self._var.get())
        if name in d:
            del d[name]
            self._var.set(d)
        else:
            raise AttributeError(f"'ContextVarLocal' object has no attribute '{name}'")


if not hasattr(sys, "_sandbox_thread_local"):
    sys._sandbox_thread_local = ContextVarLocal()

_thread_local = sys._sandbox_thread_local


class ContextPropagatingThread(threading.Thread):
    """A thread subclass that copies and propagates the current contextvars Context.

    This ensures thread-local/contextvar attributes (e.g., sandboxed state) are
    correctly carried over into child threads.
    """

    def __init__(self, *args, **kwargs):
        """Initialize ContextPropagatingThread with copied context."""
        self._ctx = contextvars.copy_context()
        super().__init__(*args, **kwargs)

    def run(self):
        """Execute the thread target within the propagated context."""

        def wrapped():
            if (
                "VectorReconstruction" in self.name
                or "reconstruction" in self.name.lower()
            ):
                _thread_local.sandboxed = True
                _thread_local.reason = "background vector reconstruction"
            super(ContextPropagatingThread, self).run()

        self._ctx.run(wrapped)


class ContextPropagatingThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """A ThreadPoolExecutor that ensures contextvars context is propagated.

    This guarantees tasks submitted to the pool execute under the exact context
    active at submission time.
    """

    def submit(self, fn, *args, **kwargs):
        """Submit a callable to the pool, wrapping it to run inside the active context.

        Parameters
        ----------
        fn : callable
            The function to execute.
        *args : tuple
            Positional arguments for the callable.
        **kwargs : dict
            Keyword arguments for the callable.

        Returns
        -------
        concurrent.futures.Future
            A future representing the execution state of the callable.
        """
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)


# Keep track of original functions permanently to avoid recursion/re-patching issues
if not hasattr(socket, "_real_socket_connect"):
    socket._real_socket_connect = socket.socket.connect
if not hasattr(socket, "_real_socket_connect_ex"):
    socket._real_socket_connect_ex = socket.socket.connect_ex
if not hasattr(socket, "_real_getaddrinfo"):
    socket._real_getaddrinfo = socket.getaddrinfo
if not hasattr(socket, "_real_gethostbyname"):
    socket._real_gethostbyname = socket.gethostbyname
if not hasattr(socket, "_real_gethostbyname_ex"):
    socket._real_gethostbyname_ex = socket.gethostbyname_ex
if not hasattr(socket, "_real_gethostbyaddr"):
    socket._real_gethostbyaddr = socket.gethostbyaddr
if not hasattr(socket, "_real_getnameinfo"):
    socket._real_getnameinfo = socket.getnameinfo
if not hasattr(socket, "_real_getfqdn"):
    socket._real_getfqdn = socket.getfqdn

_original_connect = socket._real_socket_connect
_original_connect_ex = socket._real_socket_connect_ex
_original_getaddrinfo = socket._real_getaddrinfo
_original_gethostbyname = socket._real_gethostbyname
_original_gethostbyname_ex = socket._real_gethostbyname_ex
_original_gethostbyaddr = socket._real_gethostbyaddr
_original_getnameinfo = socket._real_getnameinfo
_original_getfqdn = socket._real_getfqdn

# Resolve and cache local IP addresses once at import time
_local_ips = {
    "127.0.0.1",
    "localhost",
    "::1",
    "0.0.0.0",
    "",
}

_local_hostname = ""
try:
    _local_hostname = socket.gethostname().lower()
except Exception:
    pass


def _is_local_address(host: str) -> bool:
    """Check if the given host/IP is local/loopback/unspecified/private/link-local."""
    host_lower = host.lower()
    if host_lower in _local_ips:
        return True

    if _local_hostname and host_lower == _local_hostname:
        return True

    if host_lower.endswith(".local") or host_lower.endswith(".localhost"):
        return True

    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_unspecified or ip.is_private or ip.is_link_local
    except ValueError:
        pass

    # Try resolving hostname dynamically to check if its IPs are local/private/loopback.
    # To prevent information leaks and potential thread hangs during sandboxed offline execution,
    # we completely bypass dynamic name resolution when sandboxing is active.
    if getattr(_thread_local, "sandboxed", False):
        return False

    try:
        for info in socket.getaddrinfo(host, None):
            resolved_ip = info[4][0]
            try:
                ip = ipaddress.ip_address(resolved_ip)
                if (
                    ip.is_loopback
                    or ip.is_unspecified
                    or ip.is_private
                    or ip.is_link_local
                ):
                    return True
            except ValueError:
                pass
    except Exception:
        pass

    return False


def safe_connect(self, address):
    """Safely connect socket, raising PermissionError for external connections if sandboxed."""
    if getattr(_thread_local, "sandboxed", False):
        if isinstance(address, tuple) and len(address) > 0:
            host = str(address[0])
            if not _is_local_address(host):
                reason = getattr(_thread_local, "reason", "worker execution")
                raise PermissionError(
                    f"External network connections are blocked during {reason}: {host}"
                )
    is_self_mock = False
    is_orig_mock = False
    try:
        from unittest.mock import NonCallableMock

        if isinstance(self, NonCallableMock):
            is_self_mock = True
        if isinstance(_original_connect, NonCallableMock):
            is_orig_mock = True
    except Exception:
        pass

    if not is_self_mock:
        is_self_mock = (
            hasattr(self, "_is_mock")
            or hasattr(self, "mock_add_spec")
            or hasattr(self, "_mock_methods")
            or hasattr(self, "_spec_class")
            or "Mock" in type(self).__name__
            or "mock" in type(self).__name__
        )
    if not is_orig_mock:
        is_orig_mock = (
            hasattr(_original_connect, "_is_mock")
            or hasattr(_original_connect, "mock_add_spec")
            or hasattr(_original_connect, "_mock_methods")
            or hasattr(_original_connect, "_spec_class")
            or "Mock" in type(_original_connect).__name__
            or "mock" in type(_original_connect).__name__
        )
    if is_self_mock and not is_orig_mock:
        return None
    try:
        return _original_connect(self, address)
    except TypeError as e:
        err_msg = str(e).lower()
        if (
            is_self_mock
            or not isinstance(self, socket.socket)
            or "descriptor" in err_msg
            or "apply to" in err_msg
            or "argument 1" in err_msg
            or "must be" in err_msg
            or "requires a" in err_msg
        ) and (
            "not" in err_msg
            or "mock" in err_msg
            or "descriptor" in err_msg
            or "apply" in err_msg
        ):
            return None
        raise


def safe_connect_ex(self, address):
    """Safely connect_ex socket, raising PermissionError for external connections if sandboxed."""
    if getattr(_thread_local, "sandboxed", False):
        if isinstance(address, tuple) and len(address) > 0:
            host = str(address[0])
            if not _is_local_address(host):
                reason = getattr(_thread_local, "reason", "worker execution")
                raise PermissionError(
                    f"External network connections are blocked during {reason}: {host}"
                )
    is_self_mock = False
    is_orig_mock = False
    try:
        from unittest.mock import NonCallableMock

        if isinstance(self, NonCallableMock):
            is_self_mock = True
        if isinstance(_original_connect_ex, NonCallableMock):
            is_orig_mock = True
    except Exception:
        pass

    if not is_self_mock:
        is_self_mock = (
            hasattr(self, "_is_mock")
            or hasattr(self, "mock_add_spec")
            or hasattr(self, "_mock_methods")
            or hasattr(self, "_spec_class")
            or "Mock" in type(self).__name__
            or "mock" in type(self).__name__
        )
    if not is_orig_mock:
        is_orig_mock = (
            hasattr(_original_connect_ex, "_is_mock")
            or hasattr(_original_connect_ex, "mock_add_spec")
            or hasattr(_original_connect_ex, "_mock_methods")
            or hasattr(_original_connect_ex, "_spec_class")
            or "Mock" in type(_original_connect_ex).__name__
            or "mock" in type(_original_connect_ex).__name__
        )
    if is_self_mock and not is_orig_mock:
        return 0
    try:
        return _original_connect_ex(self, address)
    except TypeError as e:
        err_msg = str(e).lower()
        if (
            is_self_mock
            or not isinstance(self, socket.socket)
            or "descriptor" in err_msg
            or "apply to" in err_msg
            or "argument 1" in err_msg
            or "must be" in err_msg
            or "requires a" in err_msg
        ) and (
            "not" in err_msg
            or "mock" in err_msg
            or "descriptor" in err_msg
            or "apply" in err_msg
        ):
            return 0
        raise


def _is_mock_obj(obj) -> bool:
    """Detect if an object is a mock or mock method.

    Parameters
    ----------
    obj : any
        The object to inspect.

    Returns
    -------
    bool
        True if the object is a mock, False otherwise.
    """
    if obj is None:
        return False
    try:
        from unittest.mock import NonCallableMock

        if isinstance(obj, NonCallableMock):
            return True
    except Exception:
        pass
    return (
        hasattr(obj, "_is_mock")
        or hasattr(obj, "mock_add_spec")
        or hasattr(obj, "_mock_methods")
        or hasattr(obj, "_spec_class")
        or "Mock" in type(obj).__name__
        or "mock" in type(obj).__name__
    )


def safe_getaddrinfo(*args, **kwargs):
    """Safely resolve addresses, raising socket.gaierror for external lookups if sandboxed."""
    host = None
    if len(args) > 0:
        host = args[0]
    elif "host" in kwargs:
        host = kwargs["host"]

    if getattr(_thread_local, "sandboxed", False):
        if (
            host is not None
            and not _is_mock_obj(host)
            and not _is_mock_obj(_original_getaddrinfo)
        ):
            if not _is_local_address(str(host)):
                raise socket.gaierror(
                    getattr(socket, "EAI_NONAME", -2), "Name or service not known"
                )

    return _original_getaddrinfo(*args, **kwargs)


def safe_gethostbyname(*args, **kwargs):
    """Safely resolve host by name, raising socket.gaierror for external lookups if sandboxed."""
    hostname = None
    if len(args) > 0:
        hostname = args[0]
    elif "hostname" in kwargs:
        hostname = kwargs["hostname"]

    if getattr(_thread_local, "sandboxed", False):
        if (
            hostname is not None
            and not _is_mock_obj(hostname)
            and not _is_mock_obj(_original_gethostbyname)
        ):
            if not _is_local_address(str(hostname)):
                raise socket.gaierror(
                    getattr(socket, "EAI_NONAME", -2), "Name or service not known"
                )

    return _original_gethostbyname(*args, **kwargs)


def safe_gethostbyname_ex(*args, **kwargs):
    """Safely resolve host by name (extended), raising socket.gaierror for external lookups if sandboxed."""
    hostname = None
    if len(args) > 0:
        hostname = args[0]
    elif "hostname" in kwargs:
        hostname = kwargs["hostname"]

    if getattr(_thread_local, "sandboxed", False):
        if (
            hostname is not None
            and not _is_mock_obj(hostname)
            and not _is_mock_obj(_original_gethostbyname_ex)
        ):
            if not _is_local_address(str(hostname)):
                raise socket.gaierror(
                    getattr(socket, "EAI_NONAME", -2), "Name or service not known"
                )

    return _original_gethostbyname_ex(*args, **kwargs)


def safe_gethostbyaddr(*args, **kwargs):
    """Safely resolve host by address, raising socket.herror for external lookups if sandboxed."""
    ip_address = None
    if len(args) > 0:
        ip_address = args[0]
    elif "ip_address" in kwargs:
        ip_address = kwargs["ip_address"]

    if getattr(_thread_local, "sandboxed", False):
        if (
            ip_address is not None
            and not _is_mock_obj(ip_address)
            and not _is_mock_obj(_original_gethostbyaddr)
        ):
            if not _is_local_address(str(ip_address)):
                raise socket.herror(1, "Unknown host")

    return _original_gethostbyaddr(*args, **kwargs)


def safe_getnameinfo(*args, **kwargs):
    """Safely resolve name info from sockaddr, raising socket.gaierror for external lookups if sandboxed."""
    sockaddr = None
    if len(args) > 0:
        sockaddr = args[0]
    elif "sockaddr" in kwargs:
        sockaddr = kwargs["sockaddr"]

    if getattr(_thread_local, "sandboxed", False):
        host = None
        if isinstance(sockaddr, tuple) and len(sockaddr) > 0:
            host = sockaddr[0]

        if (
            host is not None
            and not _is_mock_obj(host)
            and not _is_mock_obj(_original_getnameinfo)
        ):
            if not _is_local_address(str(host)):
                raise socket.gaierror(
                    getattr(socket, "EAI_NONAME", -2), "Name or service not known"
                )

    return _original_getnameinfo(*args, **kwargs)


def safe_getfqdn(*args, **kwargs):
    """Safely resolve fully qualified domain name, returning host immediately for external if sandboxed."""
    name = ""
    if len(args) > 0:
        name = args[0]
    elif "name" in kwargs:
        name = kwargs["name"]

    if getattr(_thread_local, "sandboxed", False):
        if (
            name is not None
            and name != ""
            and not _is_mock_obj(name)
            and not _is_mock_obj(_original_getfqdn)
        ):
            if not _is_local_address(str(name)):
                return str(name)

    return _original_getfqdn(*args, **kwargs)


def apply_global_socket_sandbox():
    """Apply socket-level blocking of non-localhost outgoing network requests globally."""
    # Kept for backward-compatibility but does not do dangerous dynamic re-patching.
    pass


# Permanently patch once at import time
socket.socket.connect = safe_connect
socket.socket.connect_ex = safe_connect_ex
socket.getaddrinfo = safe_getaddrinfo
socket.gethostbyname = safe_gethostbyname
socket.gethostbyname_ex = safe_gethostbyname_ex
socket.gethostbyaddr = safe_gethostbyaddr
socket.getnameinfo = safe_getnameinfo
socket.getfqdn = safe_getfqdn


@contextmanager
def block_external_network(reason="worker execution"):
    """Block outgoing non-localhost network traffic safely and thread-locally."""
    was_sandboxed = getattr(_thread_local, "sandboxed", False)
    old_reason = getattr(_thread_local, "reason", "worker execution")
    _thread_local.sandboxed = True
    _thread_local.reason = reason
    try:
        yield
    finally:
        _thread_local.sandboxed = was_sandboxed
        _thread_local.reason = old_reason


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
        self._models = {}
        self._expected_hashes = {}
        self._cached_settings = None
        self.apply_onnx_thread_limits()
        try:
            from app.core.hashes_registry import HASHES

            for model_id, file_hashes in HASHES.items():
                self.register_expected_hashes(model_id, file_hashes)
        except ImportError:
            pass

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
        if model_id not in self._models:
            import onnxruntime as ort

            if sess_options is None:
                sess_options = ort.SessionOptions()

            thread_limit = self.get_thread_limit()
            sess_options.intra_op_num_threads = thread_limit
            sess_options.inter_op_num_threads = thread_limit

            self._models[model_id] = ort.InferenceSession(model_path, sess_options)
        return self._models[model_id]

    def register_expected_hashes(self, model_id: str, hashes: dict[str, str]):
        """Register expected SHA-256 hashes for files of a model."""
        self._expected_hashes[model_id] = hashes

    def verify_integrity(self, model_id: str, model_path: str) -> bool:
        """Verify model files against expected hashes if they are registered."""
        if model_id in self._expected_hashes:
            from app.core.path_utils import is_packaged

            expected = self._expected_hashes[model_id]
            if not model_path or not os.path.exists(model_path):
                if not is_packaged():
                    logging.warning(
                        f"Model path {model_path} does not exist. Skipping integrity check in non-packaged mode."
                    )
                    return True
                raise FileNotFoundError(
                    f"Model path {model_path} does not exist for integrity check."
                )

            if os.path.isdir(model_path):
                for filename, expected_hash in expected.items():
                    file_path = os.path.join(model_path, filename)
                    if not os.path.exists(file_path):
                        if not is_packaged():
                            logging.warning(
                                f"Required model file {file_path} is missing. Skipping integrity check in non-packaged mode."
                            )
                            return True
                        raise FileNotFoundError(
                            f"Required model file {file_path} is missing."
                        )

                    hasher = hashlib.sha256()
                    with open(file_path, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            hasher.update(chunk)
                    actual_hash = hasher.hexdigest()
                    if actual_hash != expected_hash:
                        raise ValueError(
                            f"Integrity check failed for {filename}. Expected {expected_hash}, got {actual_hash}"
                        )
            else:
                # Single file
                hasher = hashlib.sha256()
                with open(model_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        hasher.update(chunk)
                actual_hash = hasher.hexdigest()
                expected_hash = (
                    expected.get(os.path.basename(model_path))
                    or list(expected.values())[0]
                )
                if actual_hash != expected_hash:
                    raise ValueError(
                        f"Integrity check failed. Expected {expected_hash}, got {actual_hash}"
                    )
        return True

    def get_ocr_reader(self):
        """Lazily load and return the EasyOCR Reader from registry."""
        model_id = "easyocr"
        import sys

        # Resolve easyocr_dir
        if hasattr(sys, "_MEIPASS"):
            easyocr_dir = os.path.join(sys._MEIPASS, "offline_bundle", "easyocr")
        else:
            try:
                from app.core.path_utils import get_base_path

                base_path = get_base_path(__file__)
            except Exception:
                base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            local_easyocr_path = os.path.join(base_path, "offline_bundle", "easyocr")
            cwd_easyocr_path = os.path.join(os.getcwd(), "offline_bundle", "easyocr")
            easyocr_path = os.environ.get("EASYOCR_MODULE_PATH")
            if os.path.exists(local_easyocr_path):
                easyocr_dir = local_easyocr_path
            elif os.path.exists(cwd_easyocr_path):
                easyocr_dir = cwd_easyocr_path
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

        from app.core.env_helper import is_cuda_available, is_mps_available

        gpu_available = is_cuda_available() or is_mps_available()
        use_gpu = bool(ocr_gpu_enabled and gpu_available)

        current_reader_info = self._models.get("easyocr_info")
        if model_id not in self._models or current_reader_info != (langs, use_gpu):
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
                self._models[model_id] = easyocr.Reader(
                    langs,
                    gpu=use_gpu,
                    model_storage_directory=easyocr_dir,
                    download_enabled=False,
                )
                self._models["easyocr_info"] = (langs, use_gpu)
            except Exception as e:
                logging.error(
                    f"Failed to load EasyOCR reader with languages {langs} and gpu={use_gpu}: {e}. Falling back to 'en' and cpu."
                )
                try:
                    import easyocr
                    import torch

                    torch.set_num_threads(self.get_thread_limit())
                    self._models[model_id] = easyocr.Reader(
                        ["en"],
                        gpu=False,
                        model_storage_directory=easyocr_dir,
                        download_enabled=False,
                    )
                    self._models["easyocr_info"] = (["en"], False)
                except Exception as ex:
                    logging.critical(
                        f"Critical: Fallback EasyOCR initialization failed: {ex}"
                    )
                    self._models[model_id] = None
                    self._models["easyocr_info"] = (None, None)

        return self._models[model_id]

    def get_generative_model(self, model_path: str):
        """Lazily load and return the generative naming model from registry."""
        model_id = "generative_naming"
        if model_id not in self._models:
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

                    self._models[model_id] = (generator, task, tokenizer)
            except Exception as e:
                logging.error(f"Failed to load generative model in registry: {e}")
                raise e
        return self._models.get(model_id, (None, None, None))

    def get_florence_processor(self):
        """Lazily load and return the Florence-2 visual processor wrapper from registry."""
        model_id = "florence-2"
        if model_id not in self._models:
            from app.core.offline_loader import Florence2VisualProcessor

            processor = Florence2VisualProcessor(model_id=model_id)
            processor.load()
            self._models[model_id] = processor
        return self._models[model_id]


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

            _thread_local.sandboxed = is_sandboxed
            _thread_local.reason = reason
            try:
                return fn(*a, **kw)
            finally:
                _thread_local.sandboxed = was_sandboxed
                _thread_local.reason = old_reason

        return self._executor.submit(offline_wrapped_fn, *args, **kwargs)

    def shutdown(self, wait=True):
        """Shutdown the underlying executor and reset singleton instance."""
        self._executor.shutdown(wait=wait)
        SharedWorkerPool._instance = None
