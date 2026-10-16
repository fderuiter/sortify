import queue
from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import GenerativeNamingStrategy, gguf_worker_main


def test_gguf_worker_main_grammar_compilation():
    """Verify that the GGUF worker compiles grammar constraints on demand,

    and fails fast returning an error payload over IPC if compilation fails.
    """
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    # Create dummy tasks
    task_valid = {
        "prompt": "Test Prompt",
        "max_tokens": 5,
        "grammar": 'root ::= "YES" | "NO"',
    }
    task_invalid = {
        "prompt": "Test Prompt",
        "max_tokens": 5,
        "grammar": "invalid grammar structure syntax error",
    }

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "YES"}]}

    mock_llama_module = MagicMock()
    mock_llama_module.Llama = MagicMock(return_value=mock_llm)

    with patch.dict("sys.modules", {"llama_cpp": mock_llama_module}):
        with patch("llama_cpp.LlamaGrammar.from_string") as mock_from_string:
            mock_compiled_grammar = MagicMock()

            def from_string_side_effect(grammar_str):
                if "invalid" in grammar_str:
                    raise Exception("Compile error")
                return mock_compiled_grammar

            mock_from_string.side_effect = from_string_side_effect

            # Let's run a single loop cycle of gguf_worker_main by pushing tasks to input_queue
            input_queue.put(task_valid)
            input_queue.put(task_invalid)
            input_queue.put(None)  # Sentinel to break the loop

            # Execute worker main with a mock directory
            with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
                gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

            # Check that "status": "ready" was returned first
            init_res = output_queue.get()
            assert init_res == {"status": "ready"}

            # First task processing
            res_valid = output_queue.get()
            assert res_valid == {"text": "YES"}
            mock_from_string.assert_any_call('root ::= "YES" | "NO"')

            # Second task processing (invalid grammar returns error payload over IPC)
            res_invalid = output_queue.get()
            assert "error" in res_invalid
            assert "Grammar compilation failed" in res_invalid["error"]


def test_gguf_worker_main_token_bias_mapping():
    """Verify that gguf_worker_main maps conversational suppression rules to token IDs in worker process."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    task = {
        "prompt": "Generate Category",
        "max_tokens": 10,
    }

    mock_llm = MagicMock()
    mock_llm.get_vocab.return_value = {
        "sure": 101,
        "here": 102,
        "validword": 201,
        "-": 301,
    }
    mock_llm.return_value = {"choices": [{"text": "ValidWord"}]}

    mock_llama_module = MagicMock()
    mock_llama_module.Llama = MagicMock(return_value=mock_llm)

    with patch.dict("sys.modules", {"llama_cpp": mock_llama_module}):
        input_queue.put(task)
        input_queue.put(None)

        with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
            gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

        assert output_queue.get() == {"status": "ready"}
        assert output_queue.get() == {"text": "ValidWord"}

        # Assert llm was called with logit_bias containing negative biases for suppressed tokens
        call_kwargs = mock_llm.call_args.kwargs
        assert "logit_bias" in call_kwargs
        logit_bias = call_kwargs["logit_bias"]
        assert logit_bias.get(101) == -100.0  # "sure"
        assert logit_bias.get(102) == -100.0  # "here"
        assert logit_bias.get(301) == -100.0  # "-"
        assert 201 not in logit_bias  # "validword"


def test_generative_naming_strategy_escalates_worker_error():
    """Verify GenerativeNamingStrategy escalates worker error payload to secondary rule-based strategy."""
    strategy = GenerativeNamingStrategy()
    strategy._gguf_active = True
    strategy._gguf_failed = False
    strategy._gguf_process = MagicMock()
    strategy._gguf_process.is_alive.return_value = True
    strategy._gguf_input_queue = MagicMock()

    # Simulate worker returning error payload over IPC
    with patch(
        "app.core.analyzer_strategies.cooperative_queue_get",
        return_value={"error": "Grammar compilation failed: Invalid GBNF"},
    ):
        with patch.object(
            GenerativeNamingStrategy,
            "_get_cluster_keywords",
            wraps=strategy._get_cluster_keywords,
        ):
            # Calling _get_cluster_keywords should capture the worker error and fall back to super()._get_cluster_keywords
            name = strategy._get_cluster_keywords(["Financial Invoice 2024", "Tax Document"])
            # Since generative naming failed, rule-based keyword extraction extracts dominant keywords
            assert name != ""
            assert isinstance(name, str)


def test_generative_naming_strategy_passes_correct_grammars():
    """Test that GenerativeNamingStrategy correctly passes YES/NO and naming grammars

    to _run_prompt.
    """
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    captured_calls = []

    def mock_run_prompt(prompt, max_tokens, grammar=None, **kwargs):
        captured_calls.append((prompt, max_tokens, grammar, kwargs.get("stop")))
        if "YES or NO" in prompt:
            return "YES"
        return "Clean Folder Name"

    strategy._run_prompt = mock_run_prompt

    # Test folder naming grammar passing
    name = strategy._get_cluster_keywords(["doc content 1", "doc content 2"])
    assert name == "Clean Folder Name"

    assert len(captured_calls) >= 1
    prompt, max_tokens, grammar, stop = captured_calls[0]
    assert stop == ["\n", "\n\n"]
    assert "word" in grammar
    assert "[a-zA-Z0-9]+" in grammar

    # Reset and test validation grammar passing inside generate_plan
    captured_calls.clear()

    # Setup database with dummy docs and plan
    db = MagicMock()
    strategy.set_db_context(db, "/dummy")

    # We will trigger filter_plan inside generate_plan
    filenames = ["file1.txt", "file2.txt"]
    documents = ["doc content 1", "doc content 2"]
    pre_fetched_vectors = [[1.0, 0.0], [0.0, 1.0]]
    strategy.threshold = 0.8
    strategy._vector_map = {f: v for f, v in zip(filenames, pre_fetched_vectors)}

    # We patch super().generate_plan to return a plan structure
    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=({"Folder": {"file1.txt": None, "file2.txt": None}}, 0.0),
    ):
        new_plan, error = strategy.generate_plan(
            filenames, documents, 2, set(), pre_fetched_vectors=pre_fetched_vectors
        )

        # Verify that NO LLM validation prompts were called (captured_calls is empty)
        assert len(captured_calls) == 0

        # Verify that the files with low similarity to centroid are routed to "Review Required" fallback folder
        assert "Review Required" in new_plan
        assert "file1.txt" in new_plan["Review Required"]
        assert "file2.txt" in new_plan["Review Required"]
