import queue
from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import GenerativeNamingStrategy, gguf_worker_main


def test_gguf_worker_main_grammar_compilation():
    """Verify that the GGUF worker compiles and applies grammar constraints on demand,

    and falls back to unconstrained generation if compilation fails.
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

    with patch("llama_cpp.Llama", return_value=mock_llm) as mock_llama_class:
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
            mock_llm.assert_any_call(
                "Test Prompt", max_tokens=5, echo=False, grammar=mock_compiled_grammar
            )

            # Second task processing (invalid grammar raises exception in compilation and falls back to default ASCII)
            res_invalid = output_queue.get()
            assert res_invalid == {
                "text": "YES"
            }
            mock_llm.assert_any_call(
                "Test Prompt", max_tokens=5, echo=False, grammar=mock_compiled_grammar
            )


def test_generative_naming_strategy_passes_correct_grammars():
    """Test that GenerativeNamingStrategy correctly passes YES/NO and naming grammars

    to _run_prompt.
    """
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    captured_calls = []

    def mock_run_prompt(prompt, max_tokens, grammar=None):
        captured_calls.append((prompt, max_tokens, grammar))
        if "YES or NO" in prompt:
            return "YES"
        return "Clean Folder Name"

    strategy._run_prompt = mock_run_prompt

    # Test folder naming grammar passing
    name = strategy._get_cluster_keywords(["doc content 1", "doc content 2"])
    assert name == "Clean Folder Name"

    assert len(captured_calls) >= 1
    prompt, max_tokens, grammar = captured_calls[0]
    assert "word" in grammar
    assert "[a-zA-Z0-9]+" in grammar

    # Reset and test validation grammar passing inside generate_plan
    captured_calls.clear()

    # Setup database with dummy docs and plan
    db = MagicMock()
    strategy.set_db_context(db, "/dummy")

    # We will trigger filter_plan inside generate_plan
    filenames = ["file1.txt"]
    documents = ["doc content 1"]

    # We patch super().generate_plan to return a plan structure
    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
        return_value=({"file1.txt": None}, 0.0),
    ):
        new_plan, error = strategy.generate_plan(filenames, documents, 2, set())

        # Verify that validation grammar was passed
        assert len(captured_calls) == 1
        prompt, max_tokens, grammar = captured_calls[0]
        assert "YES" in prompt
        assert grammar == 'root ::= "YES" | "NO"'
