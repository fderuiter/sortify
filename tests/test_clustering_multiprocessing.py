import os
from unittest.mock import MagicMock
import pytest
from app.core.analyzer_strategies import RecursiveKMeansStrategy


def test_multiprocessing_clustering_success():
    """Verify that clustering executes successfully via child process and returns a valid plan."""
    os.environ["FORCE_MULTIPROCESSING_TEST"] = "1"
    try:
        strategy = RecursiveKMeansStrategy()
        filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
        documents = [
            "apple orange banana fruit salad recipe",
            "python programming code tutorial algorithm",
            "banana apple fruit salad organic fruit",
            "programming tutorial in python and rust",
        ]
        plan, error = strategy.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words={"and", "in", "from"},
            max_depth=3,
            max_features=2,
        )
        # Verify that we got a valid clustering plan and a non-negative error
        assert isinstance(plan, dict)
        assert len(plan) > 0
        assert isinstance(error, float)
        assert error >= 0.0
    finally:
        os.environ.pop("FORCE_MULTIPROCESSING_TEST", None)


def test_multiprocessing_clustering_cancellation():
    """Verify that clustering is immediately cancelled and returns an empty plan if cancel_check triggers."""
    os.environ["FORCE_MULTIPROCESSING_TEST"] = "1"
    try:
        strategy = RecursiveKMeansStrategy()
        filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
        documents = [
            "apple orange banana fruit salad recipe",
            "python programming code tutorial algorithm",
            "banana apple fruit salad organic fruit",
            "programming tutorial in python and rust",
        ]

        # A cancel check that is immediately True
        cancel_check = MagicMock(return_value=True)

        plan, error = strategy.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words={"and", "in", "from"},
            max_depth=3,
            max_features=2,
            cancel_check=cancel_check,
        )

        assert plan == {}
        assert error == 0.0
        assert cancel_check.called
    finally:
        os.environ.pop("FORCE_MULTIPROCESSING_TEST", None)


def test_multiprocessing_clustering_fallback_on_error(mocker):
    """Verify that the system falls back to in-thread calculation if the process pool fails to start."""
    os.environ["FORCE_MULTIPROCESSING_TEST"] = "1"
    try:
        strategy = RecursiveKMeansStrategy()
        filenames = ["doc1.txt", "doc2.txt", "doc3.txt"]
        documents = ["doc1 text", "doc2 text", "doc3 text"]

        # Mock multiprocessing.Process.start to raise an exception
        mocker.patch(
            "multiprocessing.Process.start",
            side_effect=RuntimeError("Mock process start failure"),
        )

        plan, error = strategy.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words=set(),
            max_depth=3,
            max_features=2,
        )

        # Should have fallen back to in-thread calculation and completed successfully
        assert isinstance(plan, dict)
        assert len(plan) > 0
    finally:
        os.environ.pop("FORCE_MULTIPROCESSING_TEST", None)
