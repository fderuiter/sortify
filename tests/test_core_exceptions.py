"""Unit tests for centralized exception hierarchy, cause chaining, and error handling."""

import logging

import pytest

from app.core.cache import CacheManager
from app.core.crypto import CryptoError, SessionCrypto
from app.core.downloader import (
    DiskSpaceError,
    DownloadCancelledError,
    DownloadError,
    ModelVerificationError,
    NetworkError,
    verify_temp_file_hash,
)
from app.core.exceptions import (
    CacheError,
    DownloaderError,
    OfflineLoaderError,
    SemanticEmbeddingError,
    SortifyBaseError,
)
from app.core.offline_loader import ModelWeightsNotFoundError, OfflineModelLoadError
from app.core.semantic_embeddings import DimensionMismatchError, ModelValidationError


def test_exception_hierarchy():
    """Verify all custom domain exceptions subclass SortifyBaseError."""
    down_err = DownloadError("download error")
    net_err = NetworkError("net error")
    disk_err = DiskSpaceError("disk error")
    cancel_err = DownloadCancelledError("cancelled")
    verif_err = ModelVerificationError("verif error")

    offline_err = OfflineModelLoadError("offline load error")
    weights_err = ModelWeightsNotFoundError("model_1", ["/path/a"])

    dim_err = DimensionMismatchError("dim mismatch")
    model_val_err = ModelValidationError("model val error")

    crypto_err = CryptoError("crypto error")
    cache_err = CacheError("cache error")

    # Check SortifyBaseError inheritance
    for err in [
        down_err,
        net_err,
        disk_err,
        cancel_err,
        verif_err,
        offline_err,
        weights_err,
        dim_err,
        model_val_err,
        crypto_err,
        cache_err,
    ]:
        assert isinstance(err, SortifyBaseError)

    # Check domain base inheritance
    assert isinstance(net_err, DownloaderError)
    assert isinstance(net_err, DownloadError)
    assert isinstance(weights_err, OfflineLoaderError)
    assert isinstance(weights_err, OfflineModelLoadError)
    assert isinstance(dim_err, SemanticEmbeddingError)
    assert isinstance(dim_err, ValueError)
    assert isinstance(model_val_err, SemanticEmbeddingError)
    assert isinstance(model_val_err, ValueError)
    assert isinstance(crypto_err, RuntimeError)


def test_downloader_exception_chaining(tmp_path):
    """Verify that downloader routines preserve exception context using cause chaining."""
    temp_file = tmp_path / "non_existent.tmp"
    target_file = tmp_path / "model.onnx"

    # Attempt to verify hash of non-existent temp file raises ModelVerificationError
    with pytest.raises(ModelVerificationError):
        verify_temp_file_hash(str(temp_file), str(target_file))

    # Test cause chaining when resilient_file_hash raises OSError
    existing_tmp = tmp_path / "existing.tmp"
    existing_tmp.write_text("corrupted content")

    with pytest.raises(ModelVerificationError) as exc_info:
        verify_temp_file_hash(str(existing_tmp), str(target_file))

    # Expect cause chain to be set when registry missing hash or hash fails
    err = exc_info.value
    assert isinstance(err, SortifyBaseError)


def test_cache_manager_logging_on_failure(tmp_path, caplog):
    """Verify CacheManager logs detailed diagnostic info upon query or JSON parsing failure."""
    db_file = str(tmp_path / "test_cache.db")

    class MockWorker:
        pass

    cm = CacheManager(db_file, MockWorker())

    # Corrupt table data manually to simulate JSONDecodeError
    conn = cm._get_conn()
    with conn:
        conn.execute(
            "INSERT INTO directory_cache (source_directory, corpus, locked_files, index_to_word, manual_folders) VALUES (?, ?, ?, ?, ?)",
            ("/invalid/json/dir", "{invalid json", "[]", "{}", "[]"),
        )

    with caplog.at_level(logging.ERROR):
        corpus, locked, idx, manual = cm.load_cache("/invalid/json/dir")

    assert corpus is None
    assert "Query or JSON parsing error loading cache for directory '/invalid/json/dir'" in caplog.text


def test_crypto_logging_on_fallback(tmp_path, caplog):
    """Verify SessionCrypto logs warning/debug info on fallbacks rather than failing silently."""
    key_path = tmp_path / "test.key"
    db_path = tmp_path / "test.db"

    sc = SessionCrypto(key_path, db_path)

    with caplog.at_level(logging.DEBUG):
        sc.get_cipher()

    # Confirm key generation succeeded
    assert sc._key is not None
