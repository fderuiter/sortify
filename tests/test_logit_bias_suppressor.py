from unittest.mock import MagicMock, patch

import torch

from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    NegativeLogitBiasProcessor,
)
from app.core.path_utils import is_valid_name


def test_should_bias_token():
    strategy = GenerativeNamingStrategy()

    # Conversational filler words
    assert strategy._should_bias_token("sure") is True
    assert strategy._should_bias_token("here") is True
    assert strategy._should_bias_token("is") is True
    assert strategy._should_bias_token("containing") is True

    # Special character clean-up and casing
    assert strategy._should_bias_token("Ġsure") is True
    assert strategy._should_bias_token(" here") is True
    assert strategy._should_bias_token("SURE") is True

    # Punctuation & hyphens
    assert strategy._should_bias_token("-") is True
    assert strategy._should_bias_token("abc-def") is True
    assert strategy._should_bias_token("hello!") is True
    assert strategy._should_bias_token(",") is True

    # Legitimate non-filler words should NOT be biased
    assert strategy._should_bias_token("apple") is False
    assert strategy._should_bias_token("finance") is False
    assert strategy._should_bias_token("technology") is False
    assert strategy._should_bias_token("Project") is False
    assert strategy._should_bias_token("Alpha") is False


def test_negative_logit_bias_processor():
    token_biases = {5: -100.0, 15: -100.0}
    processor = NegativeLogitBiasProcessor(token_biases)

    # 2D scores tensor (batch_size=2, vocab_size=20)
    scores_2d = torch.zeros((2, 20))
    scores_2d[0, 5] = 10.0
    scores_2d[1, 15] = 10.0

    processed_2d = processor(None, scores_2d)
    assert processed_2d[0, 5] == -90.0
    assert processed_2d[1, 15] == -90.0
    assert processed_2d[0, 0] == 0.0
    assert processed_2d[1, 0] == 0.0

    # 1D scores tensor
    scores_1d = torch.zeros(20)
    scores_1d[5] = 10.0
    processed_1d = processor(None, scores_1d)
    assert processed_1d[5] == -90.0
    assert processed_1d[0] == 0.0


def test_build_logit_biases():
    strategy = GenerativeNamingStrategy()

    # Mock tokenizer with vocab
    mock_tokenizer = MagicMock()
    mock_tokenizer.get_vocab.return_value = {
        "apple": 1,
        "sure": 2,
        "-": 3,
        "finance": 4,
        "here": 5,
    }

    biases = strategy._build_logit_biases(mock_tokenizer)
    assert biases == {2: -100.0, 3: -100.0, 5: -100.0}


def test_get_cluster_keywords_truncation_and_formatting():
    strategy = GenerativeNamingStrategy()
    strategy.generator = MagicMock()
    strategy.task = "text-generation"
    strategy.token_biases = {2: -100.0}
    strategy._model_initialized = True

    # Output is long, conversational, and contains hyphen/punctuation
    strategy.generator.return_value = [
        {"generated_text": '  Folder-containing "Finance-Data: Alpha"!!!  '}
    ]

    documents = ["doc1.txt"] * 50
    # doc_text will be joined and truncated to 1000 characters
    result = strategy._get_cluster_keywords(documents)

    # Trimming hyphens, punctuation, multiple spaces, and word limit to 1-4 words:
    # "Folder-containing "Finance-Data: Alpha"!!!"
    # -> replace hyphen: "Folder containing "Finance Data: Alpha"!!!"
    # -> replace quote: "Folder containing Finance Data: Alpha!!!"
    # -> strip punctuation: "Folder containing Finance Data  Alpha" -> "Folder containing Finance Data Alpha"
    # -> truncate to 4 words: "Folder containing Finance Data"
    # -> final sanitize_name: "Folder containing Finance Data" (which passes OS safety and is valid)
    assert result == "Folder containing Finance Data"
    assert len(result.split()) <= 4
    assert "-" not in result
    assert "!" not in result
    assert is_valid_name(result)


def test_get_cluster_keywords_causal_vs_seq2seq():
    # Test Causal LM (text-generation)
    strategy_causal = GenerativeNamingStrategy()
    strategy_causal.generator = MagicMock()
    strategy_causal.task = "text-generation"
    strategy_causal.token_biases = {1: -100.0}
    strategy_causal._model_initialized = True

    strategy_causal.generator.return_value = [{"generated_text": "Finance Alpha"}]
    result_causal = strategy_causal._get_cluster_keywords(["doc.txt"])
    assert result_causal == "Finance Alpha"

    # Verify generator call parameters for Causal
    args, kwargs = strategy_causal.generator.call_args
    assert kwargs["max_new_tokens"] == 15
    assert kwargs["return_full_text"] is False
    assert "logits_processor" in kwargs

    # Test Seq2Seq LM (text2text-generation)
    strategy_seq2seq = GenerativeNamingStrategy()
    strategy_seq2seq.generator = MagicMock()
    strategy_seq2seq.task = "text2text-generation"
    strategy_seq2seq.token_biases = {2: -100.0}
    strategy_seq2seq._model_initialized = True

    strategy_seq2seq.generator.return_value = [{"generated_text": "Tech Support"}]
    result_seq2seq = strategy_seq2seq._get_cluster_keywords(["doc.txt"])
    assert result_seq2seq == "Tech Support"

    # Verify generator call parameters for Seq2Seq
    args, kwargs = strategy_seq2seq.generator.call_args
    assert kwargs["max_new_tokens"] == 15
    assert "return_full_text" not in kwargs
    assert "logits_processor" in kwargs


def test_fallback_to_keywords():
    strategy = GenerativeNamingStrategy()
    strategy.generator = MagicMock()
    strategy.task = "text-generation"
    strategy._model_initialized = True

    # Case 1: Generator returns empty name
    strategy.generator.return_value = [{"generated_text": ""}]
    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy._get_cluster_keywords",
        return_value="Deterministic Fallback",
    ) as mock_fallback:
        result = strategy._get_cluster_keywords(["doc.txt"])
        assert result == "Deterministic Fallback"
        mock_fallback.assert_called_once()

    # Case 2: Generator returns name shorter than 2 characters
    strategy.generator.return_value = [{"generated_text": "A"}]
    with patch(
        "app.core.analyzer_strategies.RecursiveKMeansStrategy._get_cluster_keywords",
        return_value="Deterministic Fallback",
    ) as mock_fallback:
        result = strategy._get_cluster_keywords(["doc.txt"])
        assert result == "Deterministic Fallback"
        mock_fallback.assert_called_once()


@patch("torch.set_num_threads")
def test_cpu_thread_limits(mock_set_threads):
    strategy = GenerativeNamingStrategy()
    strategy.generator = MagicMock()
    strategy.task = "text-generation"
    strategy._model_initialized = True
    strategy.generator.return_value = [{"generated_text": "Valid Name"}]

    strategy._get_cluster_keywords(["doc.txt"])
    # Verify that we set torch threads to 2 during generation
    mock_set_threads.assert_any_call(2)
