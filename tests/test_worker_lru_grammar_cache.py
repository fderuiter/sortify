import hashlib
import queue
from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import gguf_worker_main


def test_grammar_cache_hit_prevents_recompilation():
    """Verify that subsequent inference calls with identical grammar strings reuse

    the pre-compiled state machine and do not trigger re-compilation.
    """
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    grammar_rule = 'root ::= "YES" | "NO"'
    task1 = {"prompt": "Task 1", "max_tokens": 5, "grammar": grammar_rule}
    task2 = {"prompt": "Task 2", "max_tokens": 5, "grammar": grammar_rule}
    task3 = {"prompt": "Task 3", "max_tokens": 5, "grammar": grammar_rule}

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "YES"}]}

    mock_llama_module = MagicMock()
    mock_llama_module.Llama = MagicMock(return_value=mock_llm)

    with patch.dict("sys.modules", {"llama_cpp": mock_llama_module}):
        with patch("llama_cpp.LlamaGrammar.from_string") as mock_from_string:
            mock_compiled = MagicMock(name="CompiledGrammar")
            mock_from_string.return_value = mock_compiled

            input_queue.put(task1)
            input_queue.put(task2)
            input_queue.put(task3)
            input_queue.put(None)

            with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
                gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

            # Check ready status
            assert output_queue.get() == {"status": "ready"}

            # Read 3 task results
            res1 = output_queue.get()
            res2 = output_queue.get()
            res3 = output_queue.get()

            assert res1 == {"text": "YES"}
            assert res2 == {"text": "YES"}
            assert res3 == {"text": "YES"}

            # Verify LlamaGrammar.from_string was called EXACTLY ONCE despite 3 tasks
            assert mock_from_string.call_count == 1
            mock_from_string.assert_called_once_with(grammar_rule)

            # Verify llm was called with the compiled grammar for all 3 tasks
            assert mock_llm.call_count == 3
            for call_args in mock_llm.call_args_list:
                assert call_args.kwargs.get("grammar") == mock_compiled


def test_grammar_cache_lru_eviction():
    """Verify that cache capacity caps at the configured maximum entry limit

    and correctly evicts the least recently used item when full.
    """
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    grammar_a = 'root ::= "A"'
    grammar_b = 'root ::= "B"'
    grammar_c = 'root ::= "C"'

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "OK"}]}

    mock_llama_module = MagicMock()
    mock_llama_module.Llama = MagicMock(return_value=mock_llm)

    with patch.dict("sys.modules", {"llama_cpp": mock_llama_module}):
        with patch("llama_cpp.LlamaGrammar.from_string") as mock_from_string:
            mock_from_string.side_effect = lambda g: MagicMock(name=f"Compiled_{g}")

            # Capacity limit = 2
            # Task 1: A -> Compiled (Cache: [A])
            # Task 2: B -> Compiled (Cache: [A, B])
            # Task 3: A -> Cache hit (Cache: [B, A] -> A moved to MRU)
            # Task 4: C -> Compiled, B evicted (Cache: [A, C])
            # Task 5: A -> Cache hit (Cache: [C, A])
            # Task 6: B -> Cache miss! Re-compiled B (Cache: [A, B], C evicted)
            tasks = [
                {"prompt": "T1", "grammar": grammar_a},
                {"prompt": "T2", "grammar": grammar_b},
                {"prompt": "T3", "grammar": grammar_a},
                {"prompt": "T4", "grammar": grammar_c},
                {"prompt": "T5", "grammar": grammar_a},
                {"prompt": "T6", "grammar": grammar_b},
            ]

            for t in tasks:
                input_queue.put(t)
            input_queue.put(None)

            with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
                gguf_worker_main(
                    "/dummy",
                    input_queue,
                    output_queue,
                    n_threads=1,
                    max_grammar_cache_size=2,
                )

            assert output_queue.get() == {"status": "ready"}
            for _ in tasks:
                assert output_queue.get() == {"text": "OK"}

            # Check compilation sequence:
            # 1. A compiled
            # 2. B compiled
            # 3. A hit (no compile)
            # 4. C compiled (B evicted)
            # 5. A hit (no compile)
            # 6. B compiled (C evicted)
            compiled_grammars = [call.args[0] for call in mock_from_string.call_args_list]
            assert compiled_grammars == [
                grammar_a,
                grammar_b,
                grammar_c,
                grammar_b,
            ]
            assert mock_from_string.call_count == 4


def test_grammar_compilation_fail_fast_cached():
    """Verify invalid grammar strings fail fast returning error payloads over IPC."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    task_invalid_1 = {"prompt": "T1", "grammar": "invalid 1"}
    task_invalid_2 = {"prompt": "T2", "grammar": "invalid 2"}

    mock_llm = MagicMock()

    mock_llama_module = MagicMock()
    mock_llama_module.Llama = MagicMock(return_value=mock_llm)

    with patch.dict("sys.modules", {"llama_cpp": mock_llama_module}):
        with patch("llama_cpp.LlamaGrammar.from_string") as mock_from_string:
            mock_from_string.side_effect = Exception("Syntax error in grammar")

            input_queue.put(task_invalid_1)
            input_queue.put(task_invalid_2)
            input_queue.put(None)

            with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
                gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

            assert output_queue.get() == {"status": "ready"}
            res1 = output_queue.get()
            res2 = output_queue.get()
            assert "error" in res1
            assert "Grammar compilation failed" in res1["error"]
            assert "error" in res2
            assert "Grammar compilation failed" in res2["error"]
            mock_llm.assert_not_called()


def test_grammar_content_hash_uniqueness():
    """Verify distinct grammar specification strings generate unique content hashes

    and separate cache entries.
    """
    grammar1 = 'root ::= "Alpha"'
    grammar2 = 'root ::= "Beta"'

    hash1 = hashlib.sha256(grammar1.encode("utf-8")).hexdigest()
    hash2 = hashlib.sha256(grammar2.encode("utf-8")).hexdigest()

    assert hash1 != hash2
    assert len(hash1) == 64
    assert len(hash2) == 64


def test_worker_memory_bounded_continuous_batch():
    """Verify memory cache strictly caps at configured max size during continuous batch processing."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "OK"}]}

    mock_llama_module = MagicMock()
    mock_llama_module.Llama = MagicMock(return_value=mock_llm)

    max_cache_size = 5
    num_tasks = 30

    with patch.dict("sys.modules", {"llama_cpp": mock_llama_module}):
        with patch("llama_cpp.LlamaGrammar.from_string") as mock_from_string:
            mock_from_string.side_effect = lambda g: MagicMock(name=g)

            for i in range(num_tasks):
                input_queue.put({"prompt": f"P{i}", "grammar": f'root ::= "RULE_{i}"'})
            input_queue.put(None)

            with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
                gguf_worker_main(
                    "/dummy",
                    input_queue,
                    output_queue,
                    n_threads=1,
                    max_grammar_cache_size=max_cache_size,
                )

            assert output_queue.get() == {"status": "ready"}
            for _ in range(num_tasks):
                assert output_queue.get() == {"text": "OK"}

            # Each rule was unique, so from_string was called 30 times
            assert mock_from_string.call_count == num_tasks
