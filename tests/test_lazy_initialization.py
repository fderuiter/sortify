import tempfile
import sys
import os
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.analyzer import IncrementalAnalyzer
from app.core.analyzer_strategies import clustering_registry, GenerativeNamingStrategy


def test_standard_sorting_does_not_initialize_generative():
    """Verify that a standard deterministic sorting run does not trigger model weight loading."""
    db_worker = DBWorker()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "test.db", db_worker)
            analyzer = IncrementalAnalyzer(
                max_folders=3, stop_words={"the", "and"}, db=db
            )
            corpus = {
                "file1.txt": "Finance and stock market information.",
                "file2.txt": "Software engineering and python coding.",
            }
            analyzer.partial_fit("dummy_base", corpus)

            # Mock CONTEXTUAL_RENAMING to False to indicate standard/deterministic sorting
            settings = MagicMock()
            settings.CONTEXTUAL_RENAMING = False
            settings.MAX_DEPTH = 5
            settings.MAX_FEATURES = 3

            # Mock _init_model of GenerativeNamingStrategy to verify it is never called
            generative_strategy = clustering_registry.get_strategy("generative")
            with patch.object(generative_strategy, "_init_model") as mock_init:
                plan = analyzer.generate_sorting_plan("dummy_base", runtime_settings=settings)
                assert plan != {}
                mock_init.assert_not_called()
    finally:
        db_worker.stop()


def test_generative_sorting_initializes_generative_on_demand():
    """Verify that a generative sorting run initializes model weights on demand."""
    db_worker = DBWorker()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(Path(temp_dir) / "test.db", db_worker)
            analyzer = IncrementalAnalyzer(
                max_folders=3, stop_words={"the", "and"}, db=db
            )
            corpus = {
                "file1.txt": "Finance and stock market information.",
                "file2.txt": "Software engineering and python coding.",
            }
            analyzer.partial_fit("dummy_base", corpus)

            # Mock CONTEXTUAL_RENAMING to True to trigger generative strategy
            settings = MagicMock()
            settings.CONTEXTUAL_RENAMING = True
            settings.MAX_DEPTH = 5
            settings.MAX_FEATURES = 3

            generative_strategy = clustering_registry.get_strategy("generative")
            # Reset initialized state for test isolation
            generative_strategy._model_initialized = False

            with patch.object(generative_strategy, "_init_model") as mock_init:
                plan = analyzer.generate_sorting_plan("dummy_base", runtime_settings=settings)
                mock_init.assert_called_once()
    finally:
        db_worker.stop()


def test_ui_dynamic_progress_state_during_init():
    """Verify that during model loading, the is_loading flag is active and progress state can be displayed."""
    strategy = GenerativeNamingStrategy()
    strategy.model_path = "/dummy/existing/path"  # simulate existing model path
    
    # Mocking os.path.exists to return True
    with patch("os.path.exists", return_value=True), \
         patch("app.core.analyzer_strategies.block_external_network"):
         
         # Mock all heavy package loads
         mock_transformers = MagicMock()
         mock_torch = MagicMock()
         
         sys.modules["torch"] = mock_torch
         sys.modules["transformers"] = mock_transformers
         
         assert strategy.is_loading is False
         
         # Mock check inside AutoTokenizer.from_pretrained to verify is_loading is True
         def check_is_loading(*args, **kwargs):
             assert strategy.is_loading is True
             raise Exception("Abort early during tokenizer/model load")

         mock_transformers.AutoTokenizer.from_pretrained.side_effect = check_is_loading
         
         strategy._init_model()
         
         assert strategy.is_loading is False  # becomes False when completed/aborted
         assert strategy._model_initialized is True
