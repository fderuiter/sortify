"""Tests for the Imputed Vector Fallback for Robust Semantic Clustering.

These tests verify:
1. Zero fallbacks to TF-IDF when some embeddings are missing but at least one valid embedding exists.
2. Complete fallback to TF-IDF when 100% of the partition's embeddings are missing.
3. Perfect dimension matching and absence of mathematical alignment or dimension mismatch exceptions when zero vectors are present.
"""

from unittest.mock import patch

import pytest
from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.analyzer_strategies import RecursiveKMeansStrategy


def test_imputed_vector_fallback_partial_missing():
    """
    Verify that when up to 90% of documents in a cluster are missing embeddings,
    provided at least one valid embedding exists, the system still performs
    dense vector clustering (and does NOT fall back to TF-IDF).
    """
    # 10 documents
    filenames = [f"doc_{i}.txt" for i in range(10)]
    documents = [
        "restaurant dinner pizza delicious cheese food slice",
        "pizza restaurant wings delivery local menu hot",
        "pepperoni pizza pizza slice wings crust italian",
        "dinner slice delicious restaurant pasta sauce garlic",
        "python consulting developer web application engineering code",
        "django web development programming python api architecture",
        "consulting software coding python dev developer systems",
        "backend python developer contract remote code development",
        "legal contract agreement non-disclosure clause liability lawyer",
        "lawyer legal agreements court patent intellectual property"
    ]
    
    # 90% (9 documents) have None/missing embeddings, only the first one has a valid vector
    # Dim of active embedding model is 384
    dim = 384
    valid_vector = [0.1] * dim
    
    pre_fetched_vectors = [valid_vector] + [None] * 9
    
    strategy = RecursiveKMeansStrategy()
    
    # Track TfidfVectorizer instantiations to distinguish between keyword generation and clustering fallback
    init_max_features = []
    original_init = TfidfVectorizer.__init__

    def mock_init(self, *args, **kwargs):
        init_max_features.append(kwargs.get("max_features"))
        original_init(self, *args, **kwargs)
    
    with patch.object(TfidfVectorizer, "__init__", mock_init):
        plan, error = strategy.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=3,
            stop_words={"the", "and", "of", "to", "for"},
            max_depth=2,  # Stop before recursive calls which would contain partitions with 100% missing vectors
            max_features=3,
            pre_fetched_vectors=pre_fetched_vectors,
        )
        
        # Verify it did NOT fall back to TF-IDF clustering (which uses max_features=1000)
        assert 1000 not in init_max_features, f"Should not fall back to TF-IDF (max_features=1000) when some valid vectors exist! Max features logged: {init_max_features}"
        
        # Verify a valid plan was generated
        assert plan is not None
        assert isinstance(plan, dict)


def test_imputed_vector_fallback_100_percent_missing():
    """
    Verify that the system falls back to full TF-IDF clustering
    if 100% of the documents in a partition/batch lack embeddings.
    """
    # 5 documents
    filenames = [f"doc_{i}.txt" for i in range(5)]
    documents = [
        "restaurant dinner pizza delicious cheese food slice",
        "pizza restaurant wings delivery local menu hot",
        "python consulting developer web application engineering code",
        "django web development programming python api architecture",
        "legal contract agreement non-disclosure clause liability lawyer"
    ]
    
    # 100% are missing (pre_fetched_vectors is either None or all Nones)
    pre_fetched_vectors = [None] * 5
    
    strategy = RecursiveKMeansStrategy()
    
    # Track TfidfVectorizer instantiations
    init_max_features = []
    original_init = TfidfVectorizer.__init__

    def mock_init(self, *args, **kwargs):
        init_max_features.append(kwargs.get("max_features"))
        original_init(self, *args, **kwargs)
    
    with patch.object(TfidfVectorizer, "__init__", mock_init):
        plan, error = strategy.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words={"the"},
            max_depth=3,
            max_features=3,
            pre_fetched_vectors=pre_fetched_vectors,
        )
        
        # Verify it fell back to TF-IDF (which uses max_features=1000)
        assert 1000 in init_max_features, "Must fall back to TF-IDF (max_features=1000) when 100% of the embeddings are missing!"
        assert plan is not None


def test_no_alignment_exceptions_with_different_dimensions():
    """
    Verify that the zero-imputed vectors strictly match the dimension of the valid vector
    (even if it's 128 instead of 384, etc.) to prevent alignment crashes.
    """
    filenames = ["doc_A.txt", "doc_B.txt", "doc_C.txt"]
    documents = [
        "restaurant dinner pizza slice",
        "python consulting contract invoice",
        "legal agreement lawyer contract NDA"
    ]
    
    # Valid vector is dimension 128
    dim = 128
    valid_vector = [0.5] * dim
    
    # doc_A has valid, doc_B and doc_C are missing
    pre_fetched_vectors = [valid_vector, None, None]
    
    strategy = RecursiveKMeansStrategy()
    
    # This should run successfully without raising any ValueError/dimension mismatch or mathematical alignment exception
    try:
        plan, error = strategy.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words={"the"},
            max_depth=2,
            max_features=2,
            pre_fetched_vectors=pre_fetched_vectors,
        )
    except Exception as e:
        pytest.fail(f"Mathematical alignment or dimension mismatch exception occurred: {e}")
        
    assert plan is not None
