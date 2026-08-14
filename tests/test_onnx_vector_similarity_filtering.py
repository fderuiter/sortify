from unittest.mock import patch

from app.core.analyzer_strategies import GenerativeNamingStrategy


def test_vector_similarity_filtering_below_threshold():
    """Verify that documents falling below the similarity threshold are cleanly routed to the 'Review Required' fallback directory."""
    strategy = GenerativeNamingStrategy()
    strategy.threshold = 0.7

    filenames = ["finance1.txt", "finance2.txt", "unrelated.txt"]
    documents = [
        "stock investment banking money portfolio",
        "market investment stocks wealth wealth",
        "alien planets astronomy rocket launch"
    ]

    # Pre-fetched vectors where finance1 and finance2 are close, unrelated is orthogonal
    # Let's define:
    # finance1:   [1.0, 0.0, 0.0]
    # finance2:   [0.9, 0.1, 0.0]
    # unrelated:  [0.0, 0.0, 1.0]
    # Centroid:   Mean of all three = [1.9/3, 0.1/3, 1/3] = [0.633, 0.033, 0.333]
    #
    # Let's compute cosine similarities:
    # finance1:   dot([1, 0, 0], [0.633, 0.033, 0.333]) / (1 * norm([0.633, 0.033, 0.333]))
    #             norm = sqrt(0.633^2 + 0.033^2 + 0.333^2) = sqrt(0.400 + 0.001 + 0.111) = sqrt(0.512) = 0.715
    #             sim = 0.633 / 0.715 = 0.885
    # finance2:   dot([0.9, 0.1, 0], [0.633, 0.033, 0.333]) / (norm([0.9, 0.1, 0]) * 0.715)
    #             norm([0.9, 0.1, 0]) = sqrt(0.81 + 0.01) = 0.905
    #             dot = 0.9*0.633 + 0.1*0.033 = 0.570 + 0.003 = 0.573
    #             sim = 0.573 / (0.905 * 0.715) = 0.573 / 0.647 = 0.885
    # unrelated:  dot([0, 0, 1], [0.633, 0.033, 0.333]) / (1 * 0.715) = 0.333 / 0.715 = 0.466
    #
    # Under threshold = 0.7:
    # finance1 & finance2 have sim 0.885 >= 0.7, so they are kept.
    # unrelated has sim 0.466 < 0.7, so it goes to "Review Required".

    pre_fetched_vectors = [
        [1.0, 0.0, 0.0],
        [0.9, 0.1, 0.0],
        [0.0, 0.0, 1.0]
    ]

    strategy._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}

    # Mock super().generate_plan to return the initial plan (all files in one cluster "Finance")
    initial_plan = {
        "Finance": {
            "finance1.txt": None,
            "finance2.txt": None,
            "unrelated.txt": None
        }
    }

    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=(initial_plan, 0.0)
    ):
        new_plan, error = strategy.generate_plan(filenames, documents, 3, set(), pre_fetched_vectors=pre_fetched_vectors)

        # unrelated.txt should be in "Review Required"
        assert "Review Required" in new_plan
        assert "unrelated.txt" in new_plan["Review Required"]

        # finance1.txt and finance2.txt should remain in "Finance"
        assert "Finance" in new_plan
        assert "finance1.txt" in new_plan["Finance"]
        assert "finance2.txt" in new_plan["Finance"]
        assert "unrelated.txt" not in new_plan["Finance"]


def test_vector_similarity_filtering_no_outliers():
    """Verify that if all documents are above the threshold, none are routed to fallback."""
    strategy = GenerativeNamingStrategy()
    strategy.threshold = 0.3  # Very low threshold

    filenames = ["finance1.txt", "finance2.txt", "finance3.txt"]
    documents = [
        "money investment bank",
        "investment money stock",
        "wealth finance portfolio"
    ]

    pre_fetched_vectors = [
        [1.0, 0.0, 0.0],
        [0.9, 0.1, 0.0],
        [0.8, 0.2, 0.0]
    ]

    strategy._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}

    initial_plan = {
        "Finance": {
            "finance1.txt": None,
            "finance2.txt": None,
            "finance3.txt": None
        }
    }

    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=(initial_plan, 0.0)
    ):
        new_plan, error = strategy.generate_plan(filenames, documents, 3, set(), pre_fetched_vectors=pre_fetched_vectors)

        # "Review Required" should not be created
        assert "Review Required" not in new_plan

        # All files should remain in "Finance"
        assert "Finance" in new_plan
        assert len(new_plan["Finance"]) == 3
        assert "finance1.txt" in new_plan["Finance"]
        assert "finance2.txt" in new_plan["Finance"]
        assert "finance3.txt" in new_plan["Finance"]


def test_vector_similarity_filtering_tfidf_fallback():
    """Verify that if no dense vectors are pre-fetched or available, we fall back to TF-IDF vector representations gracefully."""
    strategy = GenerativeNamingStrategy()
    strategy.threshold = 0.5

    filenames = ["file1.txt", "file2.txt", "unrelated.txt"]
    documents = [
        "quantum physics theory relativity science mechanics",
        "relativity science physics mechanics quantum",
        "chocolate baking recipe dessert sugar kitchen cake cookies"
    ]

    # Do not set _vector_map or pre_fetched_vectors. It should fallback to TF-IDF.
    # The TF-IDF vectors for file1 and file2 will be very similar, and "unrelated" will be orthogonal.
    # Therefore, "unrelated.txt" will fall below the threshold and be routed to "Review Required".

    initial_plan = {
        "Science": {
            "file1.txt": None,
            "file2.txt": None,
            "unrelated.txt": None
        }
    }

    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=(initial_plan, 0.0)
    ):
        new_plan, error = strategy.generate_plan(filenames, documents, 3, set())

        # unrelated.txt should be in "Review Required"
        assert "Review Required" in new_plan
        assert "unrelated.txt" in new_plan["Review Required"]

        # file1.txt and file2.txt should remain in "Science"
        assert "Science" in new_plan
        assert "file1.txt" in new_plan["Science"]
        assert "file2.txt" in new_plan["Science"]
        assert "unrelated.txt" not in new_plan["Science"]
