import os
import tempfile
from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import GenerativeNamingStrategy, gguf_worker_main
from app.core.db import Database
from app.core.db_worker import DBWorker


def test_dynamic_timeout_scaling():
    """Verify that queue polling timeouts scale dynamically with prompt length,

    capped at 60.0s.
    """
    strategy = GenerativeNamingStrategy()
    strategy._gguf_active = True
    strategy._gguf_failed = False
    strategy._gguf_process = MagicMock()
    strategy._gguf_process.is_alive.return_value = True
    strategy._gguf_input_queue = MagicMock()
    strategy._gguf_output_queue = MagicMock()

    # Case 1: Short prompt (approx 2 tokens)
    short_prompt = "Hi"
    with patch(
        "app.core.analyzer_strategies.cooperative_queue_get"
    ) as mock_get:
        mock_get.return_value = {"text": "Descriptive Name"}
        res = strategy._run_prompt(short_prompt, 15)
        assert res == "Descriptive Name"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        # Short prompt should fall back to the 8.0s minimum
        assert kwargs["timeout"] == 8.0

    # Case 2: Long prompt (approx 1000 tokens / 4000 chars)
    long_prompt = "A" * 4000
    with patch(
        "app.core.analyzer_strategies.cooperative_queue_get"
    ) as mock_get:
        mock_get.return_value = {"text": "Descriptive Name"}
        res = strategy._run_prompt(long_prompt, 15)
        assert res == "Descriptive Name"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        # estimated tokens = 4000 // 4 = 1000. timeout = 8.0 + 1000 / 20 = 58.0s
        assert kwargs["timeout"] == 58.0

    # Case 3: Extremely long prompt (approx 2000 tokens / 8000 chars) -> Capped at 60.0s
    huge_prompt = "A" * 8000
    with patch(
        "app.core.analyzer_strategies.cooperative_queue_get"
    ) as mock_get:
        mock_get.return_value = {"text": "Descriptive Name"}
        res = strategy._run_prompt(huge_prompt, 15)
        assert res == "Descriptive Name"
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        # Should be capped at exactly 60.0s
        assert kwargs["timeout"] == 60.0


def test_gguf_worker_thread_adaptation():
    """Verify that gguf_worker_main configures n_threads on the llama_cpp.Llama

    instance.
    """
    import sys
    mock_llama = MagicMock()
    mock_input_queue = MagicMock()
    mock_output_queue = MagicMock()

    # Setup fake model directory structure containing a .gguf file
    with tempfile.TemporaryDirectory() as tmp_dir:
        gguf_file_path = os.path.join(tmp_dir, "model.gguf")
        with open(gguf_file_path, "w") as f:
            f.write("fake GGUF model content")

        mock_llama_class = MagicMock(return_value=mock_llama)
        mock_llama_module = MagicMock()
        mock_llama_module.Llama = mock_llama_class

        with patch.dict(sys.modules, {"llama_cpp": mock_llama_module}):
            with patch("os.walk", return_value=[(tmp_dir, [], ["model.gguf"])]):
                # Mock queue behaviour to break loop early or put mock task
                mock_input_queue.get.side_effect = [None]  # Break loop immediately

                # Test with explicit thread count
                gguf_worker_main(
                    tmp_dir,
                    mock_input_queue,
                    mock_output_queue,
                    n_threads=6,
                )

                mock_llama_class.assert_called_once_with(
                    model_path=os.path.join(tmp_dir, "model.gguf"),
                    n_ctx=2048,
                    verbose=False,
                    n_threads=6,
                )


def test_sqlite_tfidf_few_shot_retrieval_and_injection(tmp_path):
    """Verify that historical document examples are retrieved from SQLite and

    correct matches are injected into the prompt based on TF-IDF + exact cosine
    similarity on CPU.
    """
    from app.core.db_conn import clear_connection_cache
    db_worker = DBWorker()
    try:
        db_path = tmp_path / "test.db"
        db = Database(db_path, db_worker)

        # Insert some historical documents
        base_dir = "test_base"
        db.upsert_document(
            base_dir,
            "cooking_muffins.txt",
            "hash1",
            "Baking recipe for chocolate cookies, sweet muffins, and cake in the oven.",
        )
        db.set_user_verified_target(base_dir, "hash1", "Cooking Recipes")

        db.upsert_document(
            base_dir,
            "finance_earnings.txt",
            "hash2",
            "Corporate quarterly financial earnings reports, stock portfolios, and balance sheets.",
        )
        db.set_user_verified_target(base_dir, "hash2", "Finance and Earnings")

        # Initialize GenerativeNamingStrategy
        strategy = GenerativeNamingStrategy()
        strategy.set_db_context(db, base_dir)

        # Mock prompt execution to inspect the prompt generated
        captured_prompts = []

        def mock_run_prompt(prompt, max_tokens):
            captured_prompts.append(prompt)
            return "Mocked Folder Name"

        strategy._model_initialized = True
        strategy.generator = MagicMock()  # Trick initializer into thinking model exists
        strategy._run_prompt = mock_run_prompt

        # Case 1: Target documents are similar to cooking/baking
        target_docs = ["Baking delicious chocolate cakes and sweet cupcakes in the kitchen"]
        name = strategy._get_cluster_keywords(target_docs)

        assert name == "Mocked Folder Name"
        assert len(captured_prompts) == 1

        prompt = captured_prompts[0]
        # Verify few-shot context is present
        assert "historical examples of documents and their corresponding user-corrected folder names" in prompt
        # Verify that the cooking example was injected as a few-shot match
        assert "Cooking Recipes" in prompt
        assert "chocolate cookies, sweet muffins" in prompt

        # Verify that the irrelevant finance example was NOT injected as a match because similarity is 0.0 or extremely low compared to the cooking query
        assert "Finance and Earnings" not in prompt

        # Case 2: Target documents are similar to corporate finance
        target_docs_finance = ["The corporate financial portfolio showing balanced sheet dividends and stock reports."]
        captured_prompts.clear()

        name_fin = strategy._get_cluster_keywords(target_docs_finance)
        assert name_fin == "Mocked Folder Name"
        assert len(captured_prompts) == 1

        prompt_fin = captured_prompts[0]
        assert "Finance and Earnings" in prompt_fin
        assert "Cooking Recipes" not in prompt_fin

    finally:
        db_worker.stop()
        clear_connection_cache()
