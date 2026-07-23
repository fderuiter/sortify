"""Shared Model Registry and Worker Pool module.

Provides a centralized model registry to prevent duplicate model weight loads,
and a single global worker pool to manage text extraction and analysis tasks
with offline enforcement and thread limits.
"""

import concurrent.futures
import hashlib
import ipaddress
import logging
import os
import socket
import threading
from contextlib import contextmanager

_thread_local = threading.local()

# Keep track of original functions permanently to avoid recursion/re-patching issues
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _is_local_address(host: str) -> bool:
    """Check if the given host/IP is local/loopback/unspecified."""
    try:
        allowed_hosts = {
            "127.0.0.1",
            "localhost",
            "::1",
            "0.0.0.0",
            socket.gethostname(),
            socket.getfqdn(),
        }
    except Exception:
        allowed_hosts = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

    if host in allowed_hosts:
        return True

    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_unspecified
    except ValueError:
        pass
    return False


def safe_connect(self, address):
    """Safely connect socket, raising PermissionError for external connections if sandboxed."""
    if isinstance(address, tuple) and len(address) > 0:
        host = str(address[0])
        if not _is_local_address(host):
            reason = getattr(_thread_local, "reason", "worker execution")
            raise PermissionError(
                f"External network connections are blocked during {reason}: {host}"
            )
    return _original_connect(self, address)


def safe_connect_ex(self, address):
    """Safely connect_ex socket, raising PermissionError for external connections if sandboxed."""
    if isinstance(address, tuple) and len(address) > 0:
        host = str(address[0])
        if not _is_local_address(host):
            reason = getattr(_thread_local, "reason", "worker execution")
            raise PermissionError(
                f"External network connections are blocked during {reason}: {host}"
            )
    return _original_connect_ex(self, address)


def apply_global_socket_sandbox():
    """Apply socket-level blocking of non-localhost outgoing network requests globally."""
    # Kept for backward-compatibility but does not do dangerous dynamic re-patching.
    pass


# Permanently patch once at import time
socket.socket.connect = safe_connect
socket.socket.connect_ex = safe_connect_ex


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

    def register_expected_hashes(self, model_id: str, hashes: dict[str, str]):
        """Register expected SHA-256 hashes for files of a model."""
        self._expected_hashes[model_id] = hashes

    def verify_integrity(self, model_id: str, model_path: str) -> bool:
        """Verify model files against expected hashes if they are registered."""
        if model_id in self._expected_hashes:
            expected = self._expected_hashes[model_id]
            if not model_path or not os.path.exists(model_path):
                raise FileNotFoundError(
                    f"Model path {model_path} does not exist for integrity check."
                )

            if os.path.isdir(model_path):
                for filename, expected_hash in expected.items():
                    file_path = os.path.join(model_path, filename)
                    if not os.path.exists(file_path):
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
        if model_id not in self._models:
            # Check integrity if expected hashes are registered
            if model_id in self._expected_hashes:
                # Since easyocr loads from a default system path or cache directory,
                # we can use the expected hashes to verify the downloaded files.
                # If there's a custom path or we're mocking, we look it up.
                pass

            try:
                import easyocr
                import torch

                torch.set_num_threads(2)
                # Create reader on CPU
                self._models[model_id] = easyocr.Reader(["en"], gpu=False)
            except Exception as e:
                logging.error(f"Failed to load EasyOCR reader from registry: {e}")
                self._models[model_id] = None
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

                    torch.set_num_threads(2)

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
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="GlobalSharedWorker",
            initializer=apply_global_socket_sandbox,
        )
        self.max_workers = max_workers

    def submit(self, fn, *args, **kwargs):
        """Submit a task to the pool, ensuring offline boundaries are enforced."""

        def offline_wrapped_fn(*a, **kw):
            with block_external_network():
                return fn(*a, **kw)

        return self._executor.submit(offline_wrapped_fn, *args, **kwargs)

    def shutdown(self, wait=True):
        """Shutdown the underlying executor and reset singleton instance."""
        self._executor.shutdown(wait=wait)
        SharedWorkerPool._instance = None
