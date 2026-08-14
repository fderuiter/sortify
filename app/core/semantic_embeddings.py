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


class ModelValidationError(ValueError):
    """Raised when the active model configuration contract or SHA-256 validation fails."""

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


_model_properties_cache = {}
_model_properties_cache_lock = threading.Lock()


def get_active_model_properties(model_path: str | None) -> tuple[str, int, str]:
    """Get active model signature (SHA-256 hash), dimensions, and version.

    Returns (signature, dimensions, version).
    """
    signature = "default_onnx_sig"
    dimensions = 384
    version = "1.0.0"
    is_valid = True

    if model_path is None:
        return ModelProperties(signature, dimensions, version, is_valid)

    # Fast path: check if we can resolve the onnx file and retrieve its modification time
    onnx_file = None
    if os.path.exists(model_path):
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
        try:
            mtime = os.path.getmtime(onnx_file)
        except Exception:
            mtime = 0.0

        cache_key = (model_path, onnx_file, mtime)
        with _model_properties_cache_lock:
            if cache_key in _model_properties_cache:
                return _model_properties_cache[cache_key]

        # Cache miss: compute properties by reading and parsing the file
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

        props = ModelProperties(signature, dimensions, version, is_valid)
        with _model_properties_cache_lock:
            _model_properties_cache[cache_key] = props
        return props
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

    def __init__(
        self,
        db,
        model_path: str | None = None,
        bypass_validation: bool = False,
        force_validation: bool = False,
    ):
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

        self.validation_error_message = None

        if self.model_path is not None and not bypass_validation:
            is_pytest = "PYTEST_CURRENT_TEST" in os.environ
            if force_validation or not is_pytest:
                try:
                    self.validate_model_assets()
                except ModelValidationError as e:
                    self.is_model_valid = False
                    self.validation_error_message = str(e)
                    raise e

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

        If mismatch is found, update stored active model metadata without executing
        a full-database purge of old vectors.
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
            # Record new active model signature and settings
            self.db.set_model_metadata("active_model_signature", self.signature)
            self.db.set_model_metadata("active_model_dimensions", str(self.dimensions))
            self.db.set_model_metadata("active_model_version", self.version)

    def validate_model_assets(self):
        """Validate model properties against a strict configuration contract."""
        if not self.model_path:
            return

        # 1. Check if model path exists
        if not os.path.exists(self.model_path):
            raise ModelValidationError(f"Model path does not exist: {self.model_path}")

        # 2. Find the ONNX file
        onnx_file = None
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

        if not onnx_file or not os.path.exists(onnx_file):
            raise ModelValidationError(
                f"Model path does not contain a valid ONNX model file: {self.model_path}"
            )

        # 3. Compute SHA-256 signature of the ONNX file
        hasher = hashlib.sha256()
        try:
            with open(onnx_file, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            computed_sig = hasher.hexdigest()
        except Exception as e:
            raise ModelValidationError(
                f"Failed to read or compute SHA-256 signature of ONNX model: {e}"
            )

        # 4. Check signature against hashes registry
        from app.core.hashes_registry import HASHES

        valid_onnx_hashes = set()
        for category, files in HASHES.items():
            if "model.onnx" in files:
                valid_onnx_hashes.add(files["model.onnx"])

        if computed_sig not in valid_onnx_hashes:
            raise ModelValidationError(
                f"Model SHA-256 signature mismatch or unrecognized: '{computed_sig}'. "
                f"Expected one of: {list(valid_onnx_hashes)}"
            )

        # 5. Check tokenizer files exist
        model_dir = (
            os.path.dirname(onnx_file)
            if not os.path.isdir(self.model_path)
            else self.model_path
        )
        required_tokenizer_files = ["vocab.txt", "tokenizer_config.json"]
        for tf in required_tokenizer_files:
            tf_path = os.path.join(model_dir, tf)
            if not os.path.exists(tf_path):
                raise ModelValidationError(f"Required tokenizer file is missing: {tf}")

        # 6. Check ONNX session initialization and dimensions
        try:
            from app.core.shared_registry import SharedModelRegistry

            _ = SharedModelRegistry.get_instance()
            import onnxruntime as ort

            sess = ort.InferenceSession(onnx_file)
            out = sess.get_outputs()[0]
            if not out.shape or len(out.shape) < 2:
                raise ModelValidationError(
                    "ONNX model output shape is incompatible (must be at least 2D)."
                )
            dimensions = out.shape[-1]
            if not isinstance(dimensions, int) or dimensions <= 0:
                raise ModelValidationError(f"Invalid model dimensions: {dimensions}")
        except Exception as e:
            if isinstance(e, ModelValidationError):
                raise e
            raise ModelValidationError(
                f"ONNX session initialization or dimension extraction failed: {e}"
            )

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
                        base_dir,
                        limit=50,
                        offset=0,
                        active_model_signature=self.signature,
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
                    self.db.upsert_document_vectors(
                        base_dir, batch, model_signature=self.signature
                    )

                    # Cooperative pause to ensure UI thread remains highly responsive
                    time.sleep(0.02)

                # Process in-memory tracked corrupted vectors for this base_dir
                corrupted_paths = self.db.get_corrupted_vectors_by_base_dir(base_dir)
                if corrupted_paths:
                    chunk_size = 50
                    for i in range(0, len(corrupted_paths), chunk_size):
                        with self._lock:
                            if self._stop_requested:
                                break
                        chunk = corrupted_paths[i : i + chunk_size]
                        # Fetch extracted_text of only these corrupt file paths
                        docs = self.db.get_documents_by_filepaths(base_dir, chunk)
                        if not docs:
                            continue

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
                        self.db.upsert_document_vectors(
                            base_dir, batch, model_signature=self.signature
                        )

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
        if not text or not text.strip():
            text_cleaned = text or ""
            h = hashlib.sha256(text_cleaned.encode("utf-8")).digest()
            rng = random.Random(h)
            return [rng.uniform(-1.0, 1.0) for _ in range(self.dimensions)]

        # Clean the text or default to empty
        text = text or ""

        # If model_path is provided, we must use local ONNX model and must not do silent fallback
        if self.model_path is not None:
            if not getattr(self, "is_model_valid", True):
                msg = getattr(
                    self,
                    "validation_error_message",
                    "Model validation failed or files are missing.",
                )
                raise ModelValidationError(msg)

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
                    sum_mask = np.clip(sum_mask, a_min=1, a_max=None)
                    embedding = sum_embeddings / sum_mask
                    embedding_vector = embedding[0]

                    # L2 Normalization
                    norm = np.linalg.norm(embedding_vector)
                    if norm > 0:
                        normalized_embedding = embedding_vector / norm
                    else:
                        normalized_embedding = embedding_vector

                    return normalized_embedding.tolist()
                else:
                    raise ModelValidationError("Model ONNX file not found.")

            except Exception as e:
                if isinstance(e, ModelValidationError):
                    raise e
                msg = f"Local ONNX embedding generation failed: {e}"
                logging.error(msg)
                raise ModelValidationError(msg) from e

        # If model_path is None, we run standard deterministic dummy generator (mock/test mode)
        h = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(h)
        # Generate standard normalized floats
        return [rng.uniform(-1.0, 1.0) for _ in range(self.dimensions)]

    def get_vector(self, base_dir: str, filepath: str) -> list[float] | None:
        """Retrieve decoupled vector from child store."""
        return self.db.get_document_vector(base_dir, filepath)

    def get_vectors_batch(
        self, base_dir: str, filepaths: list[str]
    ) -> dict[str, list[float]]:
        """Retrieve decoupled vectors for a list of document filepaths in batched format.

        Handles missing vectors or invalid dimensions by reconstructing them on-the-fly,
        prioritizing the local thread-locked decrypted document cache.
        """
        if not base_dir or not filepaths:
            return {}

        filepaths_norm = [fp.replace("\\", "/") for fp in filepaths]

        # 1. Chunk the query to the DB's batch vector retrieval in groups of 50
        retrieved_vectors = {}
        chunk_size = 50
        for i in range(0, len(filepaths_norm), chunk_size):
            chunk = filepaths_norm[i : i + chunk_size]
            batch_result = self.db.get_document_vectors_batch(base_dir, chunk)
            retrieved_vectors.update(batch_result)

        # 2. Identify missing or invalid vectors
        missing_or_invalid = []
        for fp in filepaths_norm:
            vec = retrieved_vectors.get(fp)
            if not self.validate_vector_dimension(vec):
                missing_or_invalid.append(fp)

        if not missing_or_invalid:
            return retrieved_vectors

        # 3. For missing/invalid, prioritize reading from the local thread-locked decrypted cache in self.db
        # Call _populate_cache_if_needed beforehand to warm the cache if needed
        self.db._populate_cache_if_needed(base_dir)

        cache_texts = {}
        with self.db._cache_lock:
            if (
                self.db._cached_base_dir == base_dir
                and self.db._cached_documents is not None
            ):
                # Cache contains tuples: (filepath, decrypted_text, file_hash, user_verified_target_path)
                # Let's map filepath to decrypted_text
                for row in self.db._cached_documents:
                    cache_texts[row[0]] = row[1]

        still_missing = []
        resolved_texts = {}
        for fp in missing_or_invalid:
            if fp in cache_texts:
                resolved_texts[fp] = cache_texts[fp]
            else:
                still_missing.append(fp)

        # 4. For any files still missing from cache, load them from disk in chunks of 50
        if still_missing:
            for i in range(0, len(still_missing), chunk_size):
                chunk = still_missing[i : i + chunk_size]
                disk_docs = self.db.get_documents_by_filepaths(base_dir, chunk)
                for dfp, text in disk_docs:
                    resolved_texts[dfp.replace("\\", "/")] = text

        # 5. Generate vector embeddings locally on-the-fly
        # 6. Securely upsert newly generated embeddings back to the database in chunks of 50
        new_vectors_to_upsert = []
        for fp, text in resolved_texts.items():
            vec = self.generate_embedding(text)
            retrieved_vectors[fp] = vec
            new_vectors_to_upsert.append((fp, vec))

        if new_vectors_to_upsert:
            for i in range(0, len(new_vectors_to_upsert), chunk_size):
                chunk_upsert = new_vectors_to_upsert[i : i + chunk_size]
                self.db.upsert_document_vectors(
                    base_dir, chunk_upsert, model_signature=self.signature
                )

        return retrieved_vectors

    def validate_vector_dimension(self, vector: list[float] | None) -> bool:
        """Validate vector dimension matches active model dimension."""
        if not vector:
            return False
        if len(vector) != self.dimensions:
            return False
        return True
