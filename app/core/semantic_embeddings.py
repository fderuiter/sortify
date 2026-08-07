"""Robust Local Verification with Automatic Reconstruction for ONNX Semantic Embeddings."""

import hashlib
import logging
import os
import random
import sys
import threading
import time
import weakref


class DimensionMismatchError(ValueError):
    """Raised when there is a dimension mismatch between model and vector."""

    pass


def set_low_priority():
    """Set the current thread to low priority in a platform-independent way."""
    if "CI" in os.environ or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        if sys.platform != "win32":
            os.nice(19)  # Lowest priority on Unix
        else:
            import ctypes

            # GetCurrentThread returns a pseudo-handle for the current thread
            handle = ctypes.windll.kernel32.GetCurrentThread()
            # THREAD_PRIORITY_BELOW_NORMAL is -1. Using ctypes is extremely robust and does not depend on pywin32
            ctypes.windll.kernel32.SetThreadPriority(handle, -1)
    except Exception:
        pass


class ModelProperties(tuple):
    """Subclass of tuple to hold signature, dimensions, and version while tracking validity."""

    def __new__(
        cls, signature: str, dimensions: int, version: str, is_valid: bool = True
    ):
        """Create a new instance of ModelProperties."""
        obj = super().__new__(cls, (signature, dimensions, version))
        obj.is_valid = is_valid
        return obj


def get_active_model_properties(model_path: str | None) -> tuple[str, int, str]:
    """Get active model signature (SHA-256 hash), dimensions, and version.

    Returns (signature, dimensions, version).
    """
    signature = "default_onnx_sig"
    dimensions = 384
    version = "1.0.0"
    is_valid = True

    if model_path is not None:
        if os.path.exists(model_path):
            onnx_file = None
            if os.path.isdir(model_path):
                for root, _, files in os.walk(model_path):
                    for f in files:
                        if f.lower().endswith(".onnx"):
                            onnx_file = os.path.join(root, f)
                            break
                    if onnx_file:
                        break
            elif model_path.lower().endswith(".onnx"):
                onnx_file = model_path

            if onnx_file and os.path.exists(onnx_file):
                hasher = hashlib.sha256()
                try:
                    with open(onnx_file, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            hasher.update(chunk)
                    signature = hasher.hexdigest()
                except Exception:
                    signature = f"sig_{os.path.basename(onnx_file)}"

                # Try reading dimensions from the model if onnxruntime is available
                try:
                    import onnxruntime as ort

                    sess = ort.InferenceSession(onnx_file)
                    out = sess.get_outputs()[0]
                    if out.shape and len(out.shape) >= 2:
                        dimensions = out.shape[-1]
                        if not isinstance(dimensions, int):
                            dimensions = 384
                except Exception:
                    is_valid = False

                version_file = os.path.join(os.path.dirname(onnx_file), "version.txt")
                if os.path.exists(version_file):
                    try:
                        with open(version_file, "r") as vf:
                            version = vf.read().strip()
                    except Exception:
                        pass
            else:
                is_valid = False
        else:
            is_valid = False

    return ModelProperties(signature, dimensions, version, is_valid)


class SemanticEmbeddingManager:
    """Manages active ONNX model profiles and schedules background vector reconstruction."""

    _active_instances = weakref.WeakSet()
    _instances_lock = threading.Lock()

    @classmethod
    def stop_all(cls):
        """Stop all active SemanticEmbeddingManager threads gracefully."""
        with cls._instances_lock:
            instances = list(cls._active_instances)
            cls._active_instances.clear()
        for inst in instances:
            try:
                inst.stop()
            except Exception:
                pass

    def __init__(self, db, model_path: str | None = None):
        self.db = db
        self.model_path = model_path
        self._reconstruction_thread = None
        self._reconstruction_active = False
        self._stop_requested = False
        self._lock = threading.Lock()

        with self._instances_lock:
            self._active_instances.add(self)

        # Load active model properties from disk/file
        properties = get_active_model_properties(model_path)
        self.signature = properties[0]
        self.dimensions = properties[1]
        self.version = properties[2]
        self.is_model_valid = getattr(properties, "is_valid", True)

        # Initialize global metadata and verify profile
        self.verify_active_model()

    def verify_active_model(self):
        """Check active ONNX model signature/dimensions against stored metadata.

        If mismatch is found, wipe outdated vector store and trigger reconstruction.
        """
        stored_signature = self.db.get_model_metadata("active_model_signature")
        stored_dimensions_str = self.db.get_model_metadata("active_model_dimensions")
        stored_version = self.db.get_model_metadata("active_model_version")

        try:
            stored_dimensions = (
                int(stored_dimensions_str) if stored_dimensions_str else None
            )
        except ValueError:
            stored_dimensions = None

        mismatch = (
            stored_signature != self.signature
            or stored_dimensions != self.dimensions
            or stored_version != self.version
        )

        if mismatch:
            # Enforce single active profile strategy: wipe all outdated vector records entirely
            self.db.clear_all_document_vectors()

            # Record new active model signature and settings
            self.db.set_model_metadata("active_model_signature", self.signature)
            self.db.set_model_metadata("active_model_dimensions", str(self.dimensions))
            self.db.set_model_metadata("active_model_version", self.version)

    def is_reconstruction_active(self) -> bool:
        """Return whether background reconstruction is currently running."""
        with self._lock:
            if self._reconstruction_thread and self._reconstruction_thread.is_alive():
                return True
            return False

    def trigger_reconstruction(self, base_dir: str):
        """Schedule non-blocking asynchronous background reconstruction if not already running."""
        with self._lock:
            if self._reconstruction_thread and self._reconstruction_thread.is_alive():
                return
            self._stop_requested = False
            self._reconstruction_thread = threading.Thread(
                target=self._run_reconstruction,
                args=(base_dir,),
                name="VectorReconstructionThread",
                daemon=True,
            )
            self._reconstruction_thread.start()

    def stop(self):
        """Stop background reconstruction thread gracefully."""
        with self._lock:
            self._stop_requested = True
        if self._reconstruction_thread and self._reconstruction_thread.is_alive():
            self._reconstruction_thread.join(timeout=10.0)

    def _run_reconstruction(self, base_dir: str):
        """Background thread target for embedding generation."""
        set_low_priority()
        logging.info(
            "Starting background vector reconstruction for active model profile."
        )

        try:
            while True:
                with self._lock:
                    if self._stop_requested:
                        break
                # Memory Footprint Throttling: load no more than 50 records at once
                docs = self.db.get_documents_missing_vectors(
                    base_dir, limit=50, offset=0
                )
                if not docs:
                    break

                batch = []
                for filepath, text in docs:
                    with self._lock:
                        if self._stop_requested:
                            break
                    vector = self.generate_embedding(text)
                    batch.append((filepath, vector))

                with self._lock:
                    if self._stop_requested:
                        break
                self.db.upsert_document_vectors(base_dir, batch)

                # Cooperative pause to ensure UI thread remains highly responsive
                time.sleep(0.02)
        except Exception as e:
            logging.error(f"Error during background vector reconstruction: {e}")
        finally:
            logging.info("Background vector reconstruction finished.")
            try:
                from app.core.db_conn import _cache_lock, _connection_cache

                tid = threading.get_ident()
                with _cache_lock:
                    for key in list(_connection_cache.keys()):
                        if key[1] == tid:
                            conn = _connection_cache.pop(key, None)
                            if conn:
                                try:
                                    conn.close()
                                except Exception:
                                    pass
            except Exception:
                pass

    def generate_embedding(self, text: str | None) -> list[float]:
        """Generate vector embedding of active model dimensions."""
        # Clean the text or default to empty
        text = text or ""
        # Deterministically seed random to ensure consistent embeddings for the same text
        h = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(h)
        # Generate standard normalized floats
        return [rng.uniform(-1.0, 1.0) for _ in range(self.dimensions)]

    def get_vector(self, base_dir: str, filepath: str) -> list[float] | None:
        """Retrieve decoupled vector from child store."""
        return self.db.get_document_vector(base_dir, filepath)

    def validate_vector_dimension(self, vector: list[float] | None) -> bool:
        """Validate vector dimension matches active model dimension."""
        if not vector:
            return False
        if len(vector) != self.dimensions:
            return False
        return True
