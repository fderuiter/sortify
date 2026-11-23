"""Centralized base exception hierarchy with explicit error chaining for Sortify."""


class SortifyBaseError(Exception):
    """Common base exception class for all custom application errors in Sortify."""


class DownloaderError(SortifyBaseError):
    """Base exception for model downloading and network transfer operations."""


class OfflineLoaderError(SortifyBaseError):
    """Base exception for offline model resolution and sandboxed loading routines."""


class OfflineModelLoadError(OfflineLoaderError):
    """Base exception for all offline model loading errors."""


class ArchiveCorruptionError(OfflineModelLoadError):
    """Raised when sidecar archive extraction fails due to file corruption or invalid zip format."""


class HashVerificationError(OfflineModelLoadError, ValueError):
    """Raised when cryptographic SHA-256 hash verification fails for model files."""


class ModelVerificationError(DownloaderError, OfflineModelLoadError, ValueError):
    """Raised when model weight security advisory checks, cryptographic signatures, or integrity validation fails."""


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


class CryptoError(SortifyBaseError, RuntimeError):
    """Base exception for cryptographic operations and key store operations."""


class CacheError(SortifyBaseError):
    """Base exception for database-backed cache operations."""


class ArchiveSafetyError(SortifyBaseError):
    """Base exception for archive decompression guardrail and safety limit failures."""


class UnsupportedArchiveFormatError(ArchiveSafetyError):
    """Raised when an archive uses an unsupported format (such as .7z or .rar) or fails stdlib container validation."""


class SchemaValidationError(SortifyBaseError, ValueError):
    """Base exception for schema contract validation failures across event loop boundaries."""

    def __init__(self, message: str, payload_context: str | None = None) -> None:
        super().__init__(message)
        self.payload_context = payload_context


class AuditExportError(SortifyBaseError, OSError):
    """Base exception for audit report export and file write operations."""


class PartialMoveError(SortifyBaseError, RuntimeError):
    """Raised when a folder was copied to its target but the source could only be partly removed.

    The target holds the only complete copy, so it is kept. Not an OSError, so
    move retries don't copy the leftover source again.
    """


class InvalidStateTransitionError(SortifyBaseError, ValueError):
    """Raised when an illegal transaction status transition or terminal record mutation is attempted."""


class EntryNotFoundError(InvalidStateTransitionError, KeyError):
    """Raised when a transaction entry identifier is not found in the transaction ledger."""
