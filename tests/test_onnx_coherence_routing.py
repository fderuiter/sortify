import logging
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.semantic_embeddings import ModelProperties

class MockSettings:
    MAX_FOLDERS = 5
    STOP_WORDS = {"the", "and"}
    AI_CONSENT_GRANTED = True


def test_coherence_routing_low_coherence():
    """
    File clusters with a cohesion score below 0.3 are automatically routed to 'Review Required' 
    with no generative model initialization.
    """
    strategy = GenerativeNamingStrategy()
    strategy.stop_words = {"the", "and"}
    strategy.max_folders = 3
    strategy.model_path = "dummy_path"

    # Setup files in the cluster with low cohesion/coherence (mutually orthogonal vectors)
    # Documents:
    # 1. finance: [1.0, 0.0, 0.0]
    # 2. astronomy: [0.0, 1.0, 0.0]
    # 3. cooking: [0.0, 0.0, 1.0]
    # Centroid: [1/3, 1/3, 1/3], norm = sqrt(1/3) = 0.577
    # Cosine similarities to centroid:
    # dot([1, 0, 0], [1/3, 1/3, 1/3]) / (1 * 0.577) = 0.333 / 0.577 = 0.577
    # Wait, let's use vectors that have even lower similarity, or just mock the return values!
    
    # Let's mock embedding_manager in the strategy
    mock_emb_mgr = MagicMock()
    mock_emb_mgr.is_mock = False
    mock_emb_mgr.is_reconstruction_active.return_value = False
    
    # We want three vectors whose average cosine similarity to their mean vector is < 0.3.
    # Wait, the easiest way is to mock get_embedding to return vectors that are highly divergent.
    # Or even simpler, let's mock get_embedding to return specific vectors.
    # E.g., v1 = [1, 0, 0, 0], v2 = [-1, 0, 0, 0]. Centroid is [0, 0, 0, 0].
    # Cosine similarity will be 0.0, so mean cohesion score is 0.0 < 0.3.
    mock_emb_mgr.get_embedding.side_effect = [
        [1.0, 0.0, 0.0, 0.0],
        [-1.0, 0.0, 0.0, 0.0]
    ]

    documents = [
        "high risk banking funds wealth deposit stocks",
        "alien cosmic travel spaceships galaxies planets"
    ]

    # Pre-fetched corpus to satisfy model metadata requirements
    pre_fetched_corpus = {
        "model_metadata": {
            "active_model_signature": "sig_123",
            "active_model_dimensions": "4",
            "active_model_version": "1.0.0"
        },
        "examples": []
    }
    strategy.pre_fetched_corpus = pre_fetched_corpus

    with patch("app.core.semantic_embeddings.SemanticEmbeddingManager", return_value=mock_emb_mgr):
        with patch.object(strategy, "_init_model") as mock_init, \
             patch.object(strategy, "_run_prompt") as mock_prompt:
            
            # Since cohesion is below 0.3, it should route directly to 'Review Required'
            folder_name = strategy._get_cluster_keywords(documents)
            
            assert folder_name == "Review Required"
            # Ensure generative model was NOT initialized or run
            mock_init.assert_not_called()
            mock_prompt.assert_not_called()


def test_coherence_routing_high_confidence_match():
    """
    Documents with a historical folder match score of 0.85 or higher are named 
    and organized without initiating generative model.
    """
    strategy = GenerativeNamingStrategy()
    strategy.stop_words = {"the", "and"}
    strategy.max_folders = 3
    strategy.model_path = "dummy_path"

    mock_emb_mgr = MagicMock()
    mock_emb_mgr.is_mock = False
    mock_emb_mgr.is_reconstruction_active.return_value = False
    mock_emb_mgr.validate_vector_dimension.return_value = True

    # High-coherence cluster vectors: all identical [1.0, 0.0, 0.0, 0.0]
    mock_emb_mgr.get_embedding.return_value = [1.0, 0.0, 0.0, 0.0]

    documents = [
        "banking wealth stocks gold",
        "investment portfolio funds wealth"
    ]

    # Pre-fetched corpus with a historical match vector [1.0, 0.0, 0.0, 0.0] under folder "Finance"
    pre_fetched_corpus = {
        "model_metadata": {
            "active_model_signature": "sig_123",
            "active_model_dimensions": "4",
            "active_model_version": "1.0.0"
        },
        "examples": [
            {
                "filepath": "hist1.txt",
                "user_verified_target_path": "Finance",
                "vector": [1.0, 0.0, 0.0, 0.0],
                "text": "financial investments banking"
            }
        ]
    }
    strategy.pre_fetched_corpus = pre_fetched_corpus

    with patch("app.core.semantic_embeddings.SemanticEmbeddingManager", return_value=mock_emb_mgr):
        with patch.object(strategy, "_init_model") as mock_init, \
             patch.object(strategy, "_run_prompt") as mock_prompt:
            
            folder_name = strategy._get_cluster_keywords(documents)
            
            assert folder_name == "Finance"
            # Ensure generative model was NOT initialized or run
            mock_init.assert_not_called()
            mock_prompt.assert_not_called()


def test_coherence_routing_fallback_to_generative():
    """
    Generative LLM naming is executed only when the similarity match falls between 0.3 and 0.85.
    """
    strategy = GenerativeNamingStrategy()
    strategy.stop_words = {"the", "and"}
    strategy.max_folders = 3
    strategy.model_path = "dummy_path"
    strategy.generator = MagicMock()  # Mock generator to avoid skipping generative path

    mock_emb_mgr = MagicMock()
    mock_emb_mgr.is_mock = False
    mock_emb_mgr.is_reconstruction_active.return_value = False
    mock_emb_mgr.validate_vector_dimension.return_value = True

    # Cluster vectors are highly coherent but slightly different from historical match
    # Cluster centroid is [1.0, 0.0, 0.0, 0.0]
    mock_emb_mgr.get_embedding.return_value = [1.0, 0.0, 0.0, 0.0]

    documents = [
        "banking wealth stocks gold",
        "investment portfolio funds wealth"
    ]

    # Historical folder match with vector [0.5, 0.866, 0.0, 0.0] -> cosine similarity = 0.5 (between 0.3 and 0.85)
    pre_fetched_corpus = {
        "model_metadata": {
            "active_model_signature": "sig_123",
            "active_model_dimensions": "4",
            "active_model_version": "1.0.0"
        },
        "examples": [
            {
                "filepath": "hist1.txt",
                "user_verified_target_path": "Finance",
                "vector": [0.5, 0.866, 0.0, 0.0],
                "text": "financial investments banking"
            }
        ]
    }
    strategy.pre_fetched_corpus = pre_fetched_corpus

    with patch("app.core.semantic_embeddings.SemanticEmbeddingManager", return_value=mock_emb_mgr):
        with patch.object(strategy, "_init_model") as mock_init, \
             patch.object(strategy, "_run_prompt", return_value="Generative Named Folder") as mock_prompt:
            
            folder_name = strategy._get_cluster_keywords(documents)
            
            assert folder_name == "Generative Named Folder"
            # Generative model MUST be initialized and run
            mock_init.assert_called_once()
            mock_prompt.assert_called_once()


def test_coherence_routing_fallback_to_tfidf():
    """
    If similarity match falls below 0.3 but cohesion is >= 0.3, return TF-IDF naming 
    without initiating generative model.
    """
    strategy = GenerativeNamingStrategy()
    strategy.stop_words = {"the", "and"}
    strategy.max_folders = 3
    strategy.model_path = "dummy_path"
    strategy.max_features = 3  # Set max_features to avoid exception in TF-IDF keywords extraction

    mock_emb_mgr = MagicMock()
    mock_emb_mgr.is_mock = False
    mock_emb_mgr.is_reconstruction_active.return_value = False
    mock_emb_mgr.validate_vector_dimension.return_value = True

    # Cluster vectors are identical [1.0, 0.0, 0.0, 0.0] (cohesion = 1.0 >= 0.3)
    mock_emb_mgr.get_embedding.return_value = [1.0, 0.0, 0.0, 0.0]

    documents = [
        "astronomy space planets stars",
        "cosmic voyage rocket launch"
    ]

    # Historical folder vector [0.0, 0.0, 0.0, 1.0] -> cosine similarity = 0.0 (< 0.3)
    pre_fetched_corpus = {
        "model_metadata": {
            "active_model_signature": "sig_123",
            "active_model_dimensions": "4",
            "active_model_version": "1.0.0"
        },
        "examples": [
            {
                "filepath": "hist1.txt",
                "user_verified_target_path": "Finance",
                "vector": [0.0, 0.0, 0.0, 1.0],
                "text": "financial investments banking"
            }
        ]
    }
    strategy.pre_fetched_corpus = pre_fetched_corpus

    with patch("app.core.semantic_embeddings.SemanticEmbeddingManager", return_value=mock_emb_mgr):
        with patch.object(strategy, "_init_model") as mock_init, \
             patch.object(strategy, "_run_prompt") as mock_prompt:
            
            folder_name = strategy._get_cluster_keywords(documents)
            
            # Should fall back to TF-IDF keywords, containing capitalized words from the text
            assert folder_name != "Review Required"
            assert "Astronomy" in folder_name or "Space" in folder_name or "Cosmic" in folder_name
            # Generative model must NOT be initialized or run
            mock_init.assert_not_called()
            mock_prompt.assert_not_called()
