from unittest.mock import MagicMock, patch

from sklearn.feature_extraction.text import TfidfVectorizer

from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.db import Database
from app.core.db_worker import DBWorker


def test_tfidf_vocabulary_fitted_exclusively_on_historical(tmp_path):
    """
    Verify:
    1. The vectorizer fits its vocabulary exclusively using historical document data
       to prevent target-driven weight warping.
    2. Any terms unique to the target cluster documents are completely ignored
       by the vectorizer's fitted vocabulary (transform results in zero overlap).
    """
    db_worker = DBWorker()
    try:
        db_path = tmp_path / "test_fit.db"
        db = Database(db_path, db_worker)
        base_dir = "test_fit_base"

        # 1. Insert historical documents with finance keywords
        db.upsert_document(
            base_dir,
            "hist_fin.txt",
            "h_f",
            "finance budget money corporate investment stock",
        )
        db.set_user_verified_target(base_dir, "h_f", "Finance")

        # Initialize strategy
        strategy = GenerativeNamingStrategy()
        strategy.set_db_context(db, base_dir)
        strategy._model_initialized = True
        strategy.generator = MagicMock()

        # Capture TfidfVectorizer instances to verify parameters and behavior
        original_fit_transform = TfidfVectorizer.fit_transform
        original_transform = TfidfVectorizer.transform

        vectorizer_instances = []

        def spy_fit_transform(self_vec, raw_documents, y=None):
            vectorizer_instances.append(self_vec)
            return original_fit_transform(self_vec, raw_documents, y)

        with (
            patch.object(TfidfVectorizer, "fit_transform", spy_fit_transform),
            patch.object(
                strategy, "_run_prompt", return_value="Custom Folder"
            ) as mock_run_prompt,
        ):
            # Target documents containing tech terms completely disjoint from historical finance
            target_docs = [
                "computer programming software python coding hardware developer"
            ]

            name = strategy._get_cluster_keywords(target_docs)
            assert name == "Custom Folder"

            # Verify TF-IDF was called and we captured the vectorizer
            assert len(vectorizer_instances) == 1
            vectorizer = vectorizer_instances[0]

            # The vectorizer must have sublinear_tf=True
            assert vectorizer.sublinear_tf is True

            # The vocabulary must contain historical words but NOT target words
            feature_names = vectorizer.get_feature_names_out()
            for word in [
                "finance",
                "budget",
                "money",
                "corporate",
                "investment",
                "stock",
            ]:
                assert word in feature_names
            for word in ["computer", "programming", "software", "python", "coding"]:
                assert word not in feature_names

    finally:
        db_worker.stop()


def test_sublinear_tf_scaling_and_full_text_use(tmp_path):
    """
    Verify:
    1. Logarithmic term-frequency scaling (sublinear_tf=True) is enabled in the vectorizer.
    2. Complete full available text is passed to the vectorizer and similarity calculations,
       not truncated to 1,000 characters.
    3. Selected few-shot historical examples are truncated to 500 characters.
    """
    db_worker = DBWorker()
    try:
        db_path = tmp_path / "test_scale.db"
        db = Database(db_path, db_worker)
        base_dir = "test_scale_base"

        # Long historical text (e.g., 800 characters)
        long_historical_text = (
            "recipe " * 150
            + "cooking kitchen cuisine chef ingredients gourmet delicious"
        )
        # Since it is long, we want to ensure its snippet in prompt is safely truncated to 500 chars.
        db.upsert_document(base_dir, "hist_cook.txt", "h_c", long_historical_text)
        db.set_user_verified_target(base_dir, "h_c", "Cooking Recipes")

        strategy = GenerativeNamingStrategy()
        strategy.set_db_context(db, base_dir)
        strategy._model_initialized = True
        strategy.generator = MagicMock()

        captured_target_text = []
        original_build_analyzer = TfidfVectorizer.build_analyzer

        def spy_build_analyzer(self_vec):
            analyzer = original_build_analyzer(self_vec)
            def wrapped_analyzer(doc):
                captured_target_text.append(doc)
                return analyzer(doc)
            return wrapped_analyzer

        # Target doc is long (e.g. 1500 characters of distinct words to ensure no truncation)
        long_target_doc = (
            "baking cake " * 150 + "recipe dessert sweet oven flour chocolate frosting"
        )
        assert len(long_target_doc) > 1200

        with (
            patch.object(TfidfVectorizer, "build_analyzer", spy_build_analyzer),
            patch.object(
                strategy, "_run_prompt", return_value="Baking Fun"
            ) as mock_run_prompt,
        ):
            name = strategy._get_cluster_keywords([long_target_doc])
            assert name == "Baking Fun"

            # Check target_text used for similarity mapping is the FULL text (no truncation to 1000)
            assert len(captured_target_text) == 1
            assert captured_target_text[0] == long_target_doc
            assert len(captured_target_text[0]) > 1000

            # Check prompt is constructed and the few shot example text is truncated to 500 characters
            prompt_passed = mock_run_prompt.call_args[0][0]
            assert "Cooking Recipes" in prompt_passed

            # Locate the snippet inside the prompt
            # Snippet starts with Cooking Recipes' Document
            example_index = prompt_passed.index("Document: recipe")
            # Get content from "Document: " to "\nFolder Name: Cooking Recipes"
            snippet_part = prompt_passed[example_index + len("Document: ") :]
            end_index = snippet_part.index("\nFolder Name:")
            snippet_text = snippet_part[:end_index]

            # Snippet length must be strictly <= 500 characters (excluding folder name)
            assert len(snippet_text) <= 500
    finally:
        db_worker.stop()


def test_resource_protection_exclusions(tmp_path):
    """
    Verify:
    1. To protect system resources, non-textual attachments (e.g., mp3, m4a), unsupported files,
       and skipped image files (PNG/JPG/JPEG or starting with [STATUS:SKIPPED]) are excluded from calculations.
    """
    db_worker = DBWorker()
    try:
        db_path = tmp_path / "test_exclude.db"
        db = Database(db_path, db_worker)
        base_dir = "test_exclude_base"

        # Insert some historical documents, including unsupported/non-textual ones
        # Valid textual document
        db.upsert_document(
            base_dir, "doc1.txt", "h1", "gourmet cooking recipe baking cupcakes kitchen"
        )
        db.set_user_verified_target(base_dir, "h1", "Cooking")

        # Non-textual attachment (unsupported extension)
        db.upsert_document(
            base_dir, "audio.mp3", "h2", "some vocal recording audio transcription"
        )
        db.set_user_verified_target(base_dir, "h2", "AudioFiles")

        # Image file (image extensions should be excluded from similarity checks to protect resources)
        db.upsert_document(
            base_dir, "chart.png", "h3", "graph chart table numbers visualization"
        )
        db.set_user_verified_target(base_dir, "h3", "Images")

        # Skipped file (starts with [STATUS:)
        db.upsert_document(
            base_dir,
            "huge.txt",
            "h4",
            "[STATUS:SKIPPED] OCR skipped due to extreme image resolution bounds",
        )
        db.set_user_verified_target(base_dir, "h4", "SkippedFiles")

        strategy = GenerativeNamingStrategy()
        strategy.set_db_context(db, base_dir)
        strategy._model_initialized = True
        strategy.generator = MagicMock()

        captured_historical_texts = []
        original_get_tfidf_stats = db.get_tfidf_stats

        def spy_get_tfidf_stats(base_dir_arg):
            N, top_terms, doc_terms, doc_metadata = original_get_tfidf_stats(base_dir_arg)
            eligible_filepaths = {row[0] for row in doc_terms}
            for filepath in doc_metadata:
                if filepath in eligible_filepaths:
                    doc_info = db.get_document(base_dir_arg, filepath)
                    if doc_info and doc_info.get("extracted_text"):
                        captured_historical_texts.append(doc_info["extracted_text"])
            return N, top_terms, doc_terms, doc_metadata

        with (
            patch.object(db, "get_tfidf_stats", spy_get_tfidf_stats),
            patch.object(
                strategy, "_run_prompt", return_value="Exclusion Folder"
            ) as mock_run_prompt,
        ):
            name = strategy._get_cluster_keywords(["cooking delicious recipe"])
            assert name == "Exclusion Folder"

            # The only historical document that should be processed is doc1.txt
            # audio.mp3, chart.png, and the skipped file must be completely excluded
            assert len(captured_historical_texts) == 1
            assert "gourmet cooking recipe" in captured_historical_texts[0]
            assert "some vocal recording" not in captured_historical_texts[0]
            assert "graph chart table" not in captured_historical_texts[0]
            assert "[STATUS:SKIPPED]" not in captured_historical_texts[0]

    finally:
        db_worker.stop()
