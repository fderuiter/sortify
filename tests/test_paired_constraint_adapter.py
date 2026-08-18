from unittest.mock import MagicMock, patch

from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    NamingConstraint,
    NegativeLogitBiasProcessor,
    PairedConstraintAdapter,
)
from app.core.path_utils import is_valid_name


def test_paired_constraint_adapter_structure():
    """Verify PairedConstraintAdapter encapsulates GBNF grammar and token bias rules."""
    constraint_en = PairedConstraintAdapter(ocr_languages="en")
    assert "word ::= [a-zA-Z0-9]+" in constraint_en.gbnf_grammar
    assert constraint_en.ocr_languages == "en"

    # NamingConstraint alias check
    constraint_de = NamingConstraint(ocr_languages="de")
    assert "word ::= [" in constraint_de.gbnf_grammar
    for char in "äöüÄÖÜß":
        assert char in constraint_de.gbnf_grammar

    processor = constraint_de.get_logits_processor()
    assert isinstance(processor, NegativeLogitBiasProcessor)


def test_tokenizer_vocabulary_scanning_character_ranges():
    """Verify vocabulary scanning correctly maps active language character ranges and penalties into token biases."""
    # 1. English scanning
    constraint_en = PairedConstraintAdapter(ocr_languages="en")
    mock_tokenizer = MagicMock()
    mock_tokenizer.get_vocab.return_value = {
        "apple": 1,
        "sure": 2,
        "-": 3,
        "Einkäufe": 4,
        "金融": 5,
        "documents": 6,
    }

    biases_en = constraint_en.build_logit_biases(mock_tokenizer)
    # Under English: "sure" (fluff), "-" (punct), "Einkäufe" (German ä/ü), "金融" (Chinese), "documents" (fluff) penalized
    assert 1 not in biases_en  # "apple" allowed
    assert biases_en[2] == -100.0  # "sure"
    assert biases_en[3] == -100.0  # "-"
    assert biases_en[4] == -100.0  # "Einkäufe"
    assert biases_en[5] == -100.0  # "金融"
    assert biases_en[6] == -100.0  # "documents"

    # 2. German scanning
    constraint_de = PairedConstraintAdapter(ocr_languages="de")
    biases_de = constraint_de.build_logit_biases(mock_tokenizer)
    # Under German: "Einkäufe" and "apple" allowed
    assert 1 not in biases_de  # "apple"
    assert 4 not in biases_de  # "Einkäufe"
    assert biases_de[2] == -100.0  # "sure"
    assert biases_de[3] == -100.0  # "-"
    assert biases_de[5] == -100.0  # "金融"

    # 3. Chinese Sim scanning
    constraint_ch = PairedConstraintAdapter(ocr_languages="ch_sim")
    biases_ch = constraint_ch.build_logit_biases(mock_tokenizer)
    # Under Simplified Chinese: "金融" and "apple" allowed, "Einkäufe" penalized
    assert 1 not in biases_ch  # "apple"
    assert 5 not in biases_ch  # "金融"
    assert biases_ch[4] == -100.0  # "Einkäufe"


def test_pytorch_fallback_applies_paired_constraint():
    """Verify PyTorch fallback execution applies NegativeLogitBiasProcessor with paired token biases."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy._gguf_active = False
    strategy._gguf_failed = True

    mock_generator = MagicMock()
    mock_generator.return_value = [{"generated_text": "Financial Audit Report"}]
    strategy.generator = mock_generator
    strategy.task = "text-generation"

    mock_tokenizer = MagicMock()
    mock_tokenizer.get_vocab.return_value = {
        "Financial": 10,
        "Audit": 11,
        "Report": 12,
        "sure": 13,
        "folder": 14,
    }
    strategy.tokenizer = mock_tokenizer

    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "en"
        mock_settings_cls.return_value = mock_settings

        name = strategy._get_cluster_keywords(["financial statement doc"])
        assert name == "Financial Audit Report"

        # Verify generator call received logits_processor with negative biases for fluff
        mock_generator.assert_called_once()
        _, kwargs = mock_generator.call_args
        assert "logits_processor" in kwargs
        processors = kwargs["logits_processor"]
        assert len(processors) >= 1
        bias_processor = processors[0]
        assert isinstance(bias_processor, NegativeLogitBiasProcessor)
        assert bias_processor.token_biases[13] == -100.0  # "sure"
        assert bias_processor.token_biases[14] == -100.0  # "folder"


def test_paired_constraint_word_count_and_sanitization():
    """Verify clean_and_truncate_name enforces 1 to 4 word limit and OS path sanitization."""
    constraint = PairedConstraintAdapter("en")

    # Excessive words, quotes, and hyphens
    raw_name = '  "Project-Alpha: Finance-Quarterly Overview Summary Analysis"  '
    cleaned = constraint.clean_and_truncate_name(raw_name)

    assert cleaned == "Project Alpha_ Finance Quarterly"
    assert len(cleaned.split()) == 4
    assert "-" not in cleaned
    assert '"' not in cleaned
    assert is_valid_name(cleaned)


def test_gguf_and_pytorch_constraint_consistency():
    """Verify both GGUF grammar and PyTorch logit biases are generated from the shared constraint adapter."""
    strategy = GenerativeNamingStrategy()
    strategy._model_initialized = True
    strategy.generator = MagicMock()

    with patch("app.config.AppSettings") as mock_settings_cls:
        mock_settings = MagicMock()
        mock_settings.OCR_LANGUAGES = "de"
        mock_settings_cls.return_value = mock_settings

        mock_tokenizer = MagicMock()
        mock_tokenizer.get_vocab.return_value = {
            "Einkäufe": 100,
            "sure": 101,
        }
        strategy.tokenizer = mock_tokenizer

        captured_grammar = []

        def mock_run_prompt(prompt, max_tokens, grammar=None):
            captured_grammar.append(grammar)
            return "Einkäufe Liste"

        strategy._run_prompt = mock_run_prompt

        name = strategy._get_cluster_keywords(["doc content"])
        assert name == "Einkäufe Liste"

        # Verify shared constraint instance was assigned and updated
        assert strategy.active_constraint is not None
        assert strategy.active_constraint.ocr_languages == "de"
        for char in "äöüÄÖÜß":
            assert char in captured_grammar[0]
        assert strategy.active_constraint.token_biases[101] == -100.0  # "sure"
        assert 100 not in strategy.active_constraint.token_biases  # "Einkäufe"
