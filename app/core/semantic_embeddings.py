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
                    from app.core.shared_registry import SharedModelRegistry

                    # Ensure thread limits are applied via SharedModelRegistry initialization
                    _ = SharedModelRegistry.get_instance()

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

    @property
    def is_mock(self) -> bool:
        """Returns True if the engine is running in mock state (no valid physical model file exists)."""
        if not self.model_path:
            return True

        properties = get_active_model_properties(self.model_path)
        is_valid = getattr(properties, "is_valid", False)

        if is_valid != self.is_model_valid:
            self.signature = properties[0]
            self.dimensions = properties[1]
            self.version = properties[2]
            self.is_model_valid = is_valid
            if is_valid:
                self.verify_active_model()

        return not self.is_model_valid

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
            from app.core.shared_registry import ContextPropagatingThread

            self._reconstruction_thread = ContextPropagatingThread(
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
            from app.core.shared_registry import block_external_network

            with block_external_network(reason="background vector reconstruction"):
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

    def get_embedding(self, text: str | None) -> list[float]:
        """Generate vector embedding of active model dimensions (alias for generate_embedding)."""
        return self.generate_embedding(text)

    def generate_embedding(self, text: str | None) -> list[float]:
        """Generate vector embedding of active model dimensions."""
        # Clean the text or default to empty
        text = text or ""

        # If model is valid and model_path is provided, try loading local ONNX model
        if self.model_path and getattr(self, "is_model_valid", True):
            try:
                onnx_file = None
                if os.path.exists(self.model_path):
                    if os.path.isdir(self.model_path):
                        for root, _, files in os.walk(self.model_path):
                            for f in files:
                                if f.lower().endswith(".onnx"):
                                    onnx_file = os.path.join(root, f)
                                    break
                            if onnx_file:
                                break
                    elif self.model_path.lower().endswith(".onnx"):
                        onnx_file = self.model_path

                if onnx_file and os.path.exists(onnx_file):
                    tokenizer_path = (
                        self.model_path
                        if os.path.isdir(self.model_path)
                        else os.path.dirname(self.model_path)
                    )

                    # Ensure offline boundaries using block_external_network
                    from app.core.shared_registry import (
                        SharedModelRegistry,
                        block_external_network,
                    )

                    registry = SharedModelRegistry.get_instance()

                    # Cache tokenizer in the shared registry to avoid heavy disk loads per text
                    tokenizer_key = f"tokenizer_{tokenizer_path}"
                    tokenizer = registry._models.get(tokenizer_key)

                    if tokenizer is None:
                        with block_external_network(reason="tokenizer initialization"):
                            from transformers import AutoTokenizer

                            tokenizer = AutoTokenizer.from_pretrained(
                                tokenizer_path, local_files_only=True
                            )
                        registry._models[tokenizer_key] = tokenizer

                    # Load/get ONNX session from registry (which applies thread limits)
                    session = registry.get_onnx_session(onnx_file)

                    # Tokenize input text
                    inputs = tokenizer(
                        text, padding=True, truncation=True, return_tensors="np"
                    )

                    # Prepare inputs for ONNX session run
                    import numpy as np

                    session_inputs = {}
                    for node in session.get_inputs():
                        if node.name in inputs:
                            session_inputs[node.name] = inputs[node.name]

                    # Run model session inference
                    outputs = session.run(None, session_inputs)
                    token_embeddings = outputs[0]

                    # Retrieve attention mask
                    if "attention_mask" in inputs:
                        attention_mask = inputs["attention_mask"]
                    else:
                        attention_mask = np.ones(
                            token_embeddings.shape[:2], dtype=np.int64
                        )

                    # Standard Mean Pooling:
                    # embedding = sum(token_embeddings * attention_mask) / sum(attention_mask)
                    input_mask_expanded = np.expand_dims(attention_mask, axis=-1)
                    sum_embeddings = np.sum(
                        token_embeddings * input_mask_expanded, axis=1
                    )
                    sum_mask = np.sum(input_mask_expanded, axis=1)
                    sum_mask = np.clip(sum_mask, a_min=1e-9, a_max=None)
                    embedding = sum_embeddings / sum_mask
                    embedding_vector = embedding[0]

                    # L2 Normalization
                    norm = np.linalg.norm(embedding_vector)
                    if norm > 0:
                        normalized_embedding = embedding_vector / norm
                    else:
                        normalized_embedding = embedding_vector

                    return normalized_embedding.tolist()

            except Exception as e:
                logging.error(
                    f"Local ONNX embedding generation failed: {e}. Falling back to deterministic dummy generator."
                )

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
