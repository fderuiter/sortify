from app.config import AppSettings


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
