import pytest
import numpy as np
from unittest.mock import patch, MagicMock
from app.core.analyzer_strategies import (
    RecursiveKMeansStrategy,
    GenerativeNamingStrategy,
    get_status_friendly_name,
    sanitize_placeholder_tags
)

def test_get_status_friendly_name():
    assert get_status_friendly_name("[STATUS:ENCRYPTED]") == "Password Protected Files"
    assert get_status_friendly_name("[STATUS:FAILED]") == "Failed Extractions"
    assert get_status_friendly_name("[STATUS:EMPTY]") == "Empty Files"
    assert get_status_friendly_name("[STATUS:UNSUPPORTED]") == "Unsupported Files"
    assert get_status_friendly_name("[STATUS:ERROR: Corrupt Image File]") == "Corrupted Files"

def test_sanitize_placeholder_tags():
    text = "Hello world [STATUS:ENCRYPTED] testing"
    assert sanitize_placeholder_tags(text).strip() == "Hello world  testing"

def test_kmeans_calculates_centers_using_only_valid_embeddings():
    """
    Verify that RecursiveKMeansStrategy isolates files with missing or failed embeddings (e.g., zero-filled or None vectors)
    from mathematical centroid calculations.
    """
    # 3 files with valid embeddings, 2 files with missing/None embeddings
    filenames = ["valid1.txt", "valid2.txt", "valid3.txt", "missing1.txt", "missing2.txt"]
    documents = [
        "restaurant dinner pizza delicious",
        "pizza restaurant menu hot",
        "dinner delicious pasta garlic",
        "some normal text",
        "some other text"
    ]
    
    # 3 valid vectors, 2 None/missing vectors
    dim = 128
    v1 = [0.1] * dim
    v2 = [0.2] * dim
    v3 = [0.3] * dim
    pre_fetched_vectors = [v1, v2, v3, None, None]
    
    strategy = RecursiveKMeansStrategy()
    strategy._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}
    strategy.max_folders = 3
    strategy.max_depth = 5
    strategy.stop_words = set()
    strategy.max_features = 3
    
    # We can patch MiniBatchKMeans.fit_predict to inspect what X is passed
    from sklearn.cluster import MiniBatchKMeans
    original_fit_predict = MiniBatchKMeans.fit_predict
    
    fit_predict_X = []
    def mock_fit_predict(self, X, y=None, **kwargs):
        fit_predict_X.append(X)
        return original_fit_predict(self, X, y, **kwargs)
        
    with patch.object(MiniBatchKMeans, "fit_predict", mock_fit_predict):
        plan = strategy._cluster_recursive(filenames, documents, depth=1)
        
        # Verify that MiniBatchKMeans was fitted only on the 3 valid vectors
        assert len(fit_predict_X) > 0
        X_passed = fit_predict_X[0]
        # Should only contain 3 samples (representing the 3 valid files), not 5!
        assert len(X_passed) == 3
        # No 0.0 imputation or shifting from the missing files
        assert not np.any(np.all(X_passed == 0.0, axis=1))

def test_status_sentinel_folders_named_dynamically():
    """
    Verify that folders consisting entirely of files that failed extraction are named dynamically
    using predefined user-friendly templates (e.g. Password Protected Files, Failed Extractions).
    """
    strategy = RecursiveKMeansStrategy()
    
    # Folders consisting entirely of ENCRYPTED status sentinels
    docs_encrypted = ["[STATUS:ENCRYPTED]", "[STATUS:ENCRYPTED]", "[STATUS:ENCRYPTED]"]
    name = strategy._get_cluster_keywords(docs_encrypted)
    assert name == "Password Protected Files"
    
    # Folders consisting entirely of FAILED status sentinels
    docs_failed = ["[STATUS:FAILED]", "[STATUS:FAILED]"]
    name_failed = strategy._get_cluster_keywords(docs_failed)
    assert name_failed == "Failed Extractions"

def test_folder_naming_suppresses_technical_status_words():
    """
    Verify that folder-naming algorithms filter out system placeholder tags before selecting top keywords
    or sending prompts to generative models.
    """
    strategy = RecursiveKMeansStrategy()
    strategy.stop_words = set()
    strategy.max_features = 3
    
    # Mix of normal text and technical status placeholder tags
    documents = [
        "important invoice [STATUS:ENCRYPTED] payment billing",
        "payment billing invoice crucial [STATUS:FAILED]",
        "billing invoice payment details"
    ]
    
    # Extract keywords
    name = strategy._get_cluster_keywords(documents)
    # The name should NOT contain status, encrypted, or failed
    lower_name = name.lower()
    assert "status" not in lower_name
    assert "encrypted" not in lower_name
    assert "failed" not in lower_name
