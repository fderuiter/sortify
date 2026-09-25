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


class SemanticEmbeddingError(SortifyBaseError):
    """Base exception for semantic vector embeddings and ONNX model validation."""

    pass


class CryptoError(SortifyBaseError, RuntimeError):
    """Base exception for cryptographic operations and key store operations."""

    pass


class CacheError(SortifyBaseError):
    """Base exception for database-backed cache operations."""

    pass
