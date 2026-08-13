import queue
import unicodedata
from unittest.mock import MagicMock, patch
import pytest

from app.core.analyzer_strategies import GenerativeNamingStrategy, gguf_worker_main
from app.core.path_utils import sanitize_name


def test_dynamic_gbnf_german():
    """Verify that setting active OCR language to 'de' generates correct GBNF grammar containing German umlauts."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    captured_calls = []

    def mock_run_prompt(prompt, max_tokens, grammar=None):
        captured_calls.append((prompt, max_tokens, grammar))
        return "Einkäufe"

    strategy._run_prompt = mock_run_prompt

    # Mock AppSettings to return German language
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "de"
        mock_settings_cls.return_value = mock_settings

        name = strategy._get_cluster_keywords(["dummy doc"])
        assert name == "Einkäufe"

        assert len(captured_calls) >= 1
        prompt, max_tokens, grammar = captured_calls[0]
        # Verify naming grammar has been customized with German characters
        assert "word" in grammar
        # Check that German characters are allowed in the character range of word
        for char in "äöüÄÖÜß":
            assert char in grammar


def test_dynamic_gbnf_french():
    """Verify that setting active OCR language to 'fr' generates correct GBNF grammar containing French accents."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    captured_calls = []

    def mock_run_prompt(prompt, max_tokens, grammar=None):
        captured_calls.append((prompt, max_tokens, grammar))
        return "Impôts"

    strategy._run_prompt = mock_run_prompt

    # Mock AppSettings to return French language
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "fr"
        mock_settings_cls.return_value = mock_settings

        name = strategy._get_cluster_keywords(["dummy doc"])
        assert name == "Impôts"

        assert len(captured_calls) >= 1
        prompt, max_tokens, grammar = captured_calls[0]
        assert "word" in grammar
        # French characters should be in the character bracket
        assert "â" in grammar and "ô" in grammar and "û" in grammar


def test_dynamic_gbnf_unsupported_language_fallback():
    """Verify fallback to English ASCII grammar when user configures an unsupported OCR language."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    captured_calls = []

    def mock_run_prompt(prompt, max_tokens, grammar=None):
        captured_calls.append((prompt, max_tokens, grammar))
        return "FallbackName"

    strategy._run_prompt = mock_run_prompt

    # Mock AppSettings to return an unsupported language
    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "unsupported_lang"
        mock_settings_cls.return_value = mock_settings

        name = strategy._get_cluster_keywords(["dummy doc"])
        assert name == "FallbackName"

        assert len(captured_calls) >= 1
        prompt, max_tokens, grammar = captured_calls[0]
        # Should have fallen back to default ASCII grammar
        assert "word" in grammar
        assert "[a-zA-Z0-9]+" in grammar


def test_dynamic_gbnf_nfc_normalization_in_sanitize_name():
    """Verify that folder names are explicitly written in Unicode Normalization Form C (NFC)."""
    # Decomposed representation of 'Einkäufe' (NFD form: 'Einka' + combining diaeresis + 'ufe')
    nfd_name = "Einka\u0308ufe"
    
    # Assert NFD form is different from NFC
    assert unicodedata.is_normalized("NFC", nfd_name) is False

    sanitized = sanitize_name(nfd_name)
    
    # Assert that output is NFC-normalized
    assert unicodedata.is_normalized("NFC", sanitized) is True
    assert sanitized == "Eink\u00e4ufe"


def test_gguf_worker_main_compilation_fallback_ascii():
    """Verify gguf_worker_main falls back to default ASCII grammar if dynamic grammar compilation fails."""
    input_queue = queue.Queue()
    output_queue = queue.Queue()

    task = {
        "prompt": "Test Prompt",
        "max_tokens": 5,
        "grammar": "some dynamic invalid grammar",
    }

    mock_llm = MagicMock()
    mock_llm.return_value = {"choices": [{"text": "OK"}]}

    with patch("llama_cpp.Llama", return_value=mock_llm):
        with patch("llama_cpp.LlamaGrammar.from_string") as mock_from_string:
            mock_compiled_grammar = MagicMock()

            def from_string_side_effect(grammar_str):
                # Raise exception on dynamic grammar, but succeed on fallback ASCII
                if "dynamic" in grammar_str:
                    raise Exception("Dynamic Grammar Compile error")
                return mock_compiled_grammar

            mock_from_string.side_effect = from_string_side_effect

            input_queue.put(task)
            input_queue.put(None)  # Sentinel to break loop

            with patch("os.walk", return_value=[("/dummy", [], ["model.gguf"])]):
                gguf_worker_main("/dummy", input_queue, output_queue, n_threads=1)

            # Get status message
            assert output_queue.get() == {"status": "ready"}
            
            # Get task result
            assert output_queue.get() == {"text": "OK"}
            
            # Verify that from_string was called for both, and llm was run with compiled default grammar
            mock_from_string.assert_any_call("some dynamic invalid grammar")
            mock_from_string.assert_any_call('root ::= word (" " word)? (" " word)? (" " word)?\nword ::= [a-zA-Z0-9]+')
            mock_llm.assert_any_call(
                "Test Prompt", max_tokens=5, echo=False, grammar=mock_compiled_grammar
            )


def test_word_boundary_preservation():
    """Verify that generative naming limits names to 4 words while preserving the structural boundary."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    def mock_run_prompt(prompt, max_tokens, grammar=None):
        return "This Is A Very Long Five Word Name"

    strategy._run_prompt = mock_run_prompt

    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "en"
        mock_settings_cls.return_value = mock_settings

        name = strategy._get_cluster_keywords(["dummy doc"])
        # Should truncate to 4 words
        assert name == "This Is A Very"
