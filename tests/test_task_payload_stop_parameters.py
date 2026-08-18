import queue
from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import GenerativeNamingStrategy, gguf_worker_main


def test_gguf_worker_applies_stop_sequences():
    """Verify that gguf_worker_main passes stop parameters to local decoder during token generation."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    task_with_stop = {
        "prompt": "Folder Name Prompt",
        "max_tokens": 15,
        "stop": ["\n", "\n\n"],
    }

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "Target Folder Name"}]}

    with patch("llama_cpp.Llama", return_value=mock_llm):
        input_queue.put(task_with_stop)
        input_queue.put(None)

        with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
            gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

        assert output_queue.get() == {"status": "ready"}
        assert output_queue.get() == {"text": "Target Folder Name"}

        mock_llm.assert_called_once_with(
            "Folder Name Prompt", max_tokens=15, echo=False, stop=["\n", "\n\n"]
        )


def test_gguf_worker_applies_stop_sequences_alternate_key():
    """Verify that gguf_worker_main accepts stop_sequences parameter in task payload."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    task_with_stop_seqs = {
        "prompt": "Folder Name Prompt",
        "max_tokens": 15,
        "stop_sequences": ["\n"],
    }

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "Folder Title"}]}

    with patch("llama_cpp.Llama", return_value=mock_llm):
        input_queue.put(task_with_stop_seqs)
        input_queue.put(None)

        with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
            gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

        assert output_queue.get() == {"status": "ready"}
        assert output_queue.get() == {"text": "Folder Title"}

        mock_llm.assert_called_once_with(
            "Folder Name Prompt", max_tokens=15, echo=False, stop=["\n"]
        )


def test_gguf_worker_fallback_when_stop_absent():
    """Verify that tasks submitted without stop parameters execute normally with standard token limits."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    task_without_stop = {
        "prompt": "Standard Task Prompt",
        "max_tokens": 20,
    }

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "Standard Response"}]}

    with patch("llama_cpp.Llama", return_value=mock_llm):
        input_queue.put(task_without_stop)
        input_queue.put(None)

        with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
            gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

        assert output_queue.get() == {"status": "ready"}
        assert output_queue.get() == {"text": "Standard Response"}

        # Assert "stop" was NOT passed in kwargs
        mock_llm.assert_called_once_with(
            "Standard Task Prompt", max_tokens=20, echo=False
        )


def test_run_prompt_enqueues_stop_payload():
    """Verify that _run_prompt passes stop sequences to the GGUF worker input queue."""
    strategy = GenerativeNamingStrategy()
    strategy._gguf_active = True
    strategy._gguf_failed = False
    strategy._gguf_process = MagicMock()
    strategy._gguf_process.is_alive.return_value = True

    mock_input_queue = MagicMock()
    strategy._gguf_input_queue = mock_input_queue

    with patch(
        "app.core.analyzer_strategies.cooperative_queue_get",
        return_value={"text": "Generated Name"},
    ):
        result = strategy._run_prompt(
            "Prompt Text", 15, grammar="grammar_def", stop=["\n", "\n\n"]
        )
        assert result == "Generated Name"

        mock_input_queue.put.assert_called_once_with(
            {
                "prompt": "Prompt Text",
                "max_tokens": 15,
                "grammar": "grammar_def",
                "stop": ["\n", "\n\n"],
            }
        )


def test_automated_folder_naming_includes_line_break_stop_sequences():
    """Verify that automated folder naming requests supply line break stop sequences in task payloads."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    captured_kwargs = {}

    def mock_run_prompt(prompt, max_tokens, grammar=None, **kwargs):
        captured_kwargs.update(kwargs)
        return "New Folder"

    strategy._run_prompt = mock_run_prompt

    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "en"
        mock_settings_cls.return_value = mock_settings

        folder_name = strategy._get_cluster_keywords(["doc 1 content", "doc 2 content"])
        assert folder_name == "New Folder"

        assert "stop" in captured_kwargs
        assert "\n" in captured_kwargs["stop"]
