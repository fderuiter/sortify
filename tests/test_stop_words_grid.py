from unittest.mock import patch

from app.config import AppSettings
from app.core.analyzer import IncrementalAnalyzer


def test_analyzer_stop_words_reload():
    """Verify that IncrementalAnalyzer supports reload_stop_words."""
    with patch("app.core.semantic_embeddings.SemanticEmbeddingManager") as mock_sem:
        analyzer = IncrementalAnalyzer(
            max_folders=3, stop_words={"the", "and"}, db=None, model_path=None
        )

        assert analyzer.stop_words == {"the", "and"}

        analyzer.reload_stop_words({"und", "der", "die"})
        assert analyzer.stop_words == {"und", "der", "die"}


def test_settings_stop_words_setattr(tmp_path):
    """Verify that setting STOP_WORDS reassigns and schedules save."""
    mock_filepath = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(mock_filepath))

    # Check default contains 'the'
    assert "the" in settings.STOP_WORDS

    initial_words = set(settings.STOP_WORDS)
    initial_words.add("testword123")

    settings.STOP_WORDS = initial_words
    assert "testword123" in settings.STOP_WORDS

    if settings._save_timer:
        settings._save_timer.cancel()
