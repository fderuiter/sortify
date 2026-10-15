"""Centralized base exception hierarchy with explicit error chaining for Sortify."""


class SortifyBaseError(Exception):
    """Common base exception class for all custom application errors in Sortify."""

    pass


class DownloaderError(SortifyBaseError):
    """Base exception for model downloading and network transfer operations."""

    pass


class OfflineLoaderError(SortifyBaseError):
    """Base exception for offline model resolution and sandboxed loading routines."""

    pass


class OfflineModelLoadError(OfflineLoaderError):
    """Base exception for all offline model loading errors."""

    pass


class ArchiveCorruptionError(OfflineModelLoadError):
    """Raised when sidecar archive extraction fails due to file corruption or invalid zip format."""

    pass


class HashVerificationError(OfflineModelLoadError, ValueError):
    """Raised when cryptographic SHA-256 hash verification fails for model files."""

    pass


class ModelWeightsNotFoundError(OfflineModelLoadError, FileNotFoundError):
    """Raised when model weights cannot be found in any searched locations."""

    def __init__(
        self,
        model_id: str = "model",
        searched_paths: list[str] | None = None,
        message: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.searched_paths = searched_paths or []
        if not message:
            paths_str = (
                ", ".join(f"'{p}'" for p in self.searched_paths)
                if self.searched_paths
                else "searched locations"
            )
            message = (
                f"Model weights for '{model_id}' were not found in any of the searched paths: {paths_str}. "
                f"Please ensure the model bundle is downloaded and placed in one of these locations."
            )
        super().__init__(message)


class SemanticEmbeddingError(SortifyBaseError):
    """Base exception for semantic vector embeddings and ONNX model validation."""

    pass


class CryptoError(SortifyBaseError, RuntimeError):
    """Base exception for cryptographic operations and key store operations."""

    pass


class CacheError(SortifyBaseError):
    """Base exception for database-backed cache operations."""

    pass


class ArchiveSafetyError(SortifyBaseError):
    """Base exception for archive decompression guardrail and safety limit failures."""

    pass


class SchemaValidationError(SortifyBaseError, ValueError):
    """Base exception for schema contract validation failures across event loop boundaries."""

    def __init__(self, message: str, payload_context: str | None = None) -> None:
        super().__init__(message)
        self.payload_context = payload_context


class AuditExportError(SortifyBaseError, OSError):
    """Base exception for audit report export and file write operations."""

    pass
