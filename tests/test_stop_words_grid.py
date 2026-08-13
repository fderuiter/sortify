from unittest.mock import MagicMock, patch

from nicegui import Client
from nicegui.elements.button import Button
from nicegui.elements.dialog import Dialog
from nicegui.elements.input import Input
from nicegui.elements.label import Label

from app.config import AppSettings
from app.ui.settings import show_settings


def test_analyzer_stop_words_reload():
    """Verify that IncrementalAnalyzer supports reload_stop_words."""
    from app.core.analyzer import IncrementalAnalyzer

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


def test_ui_stop_words_rendering():
    """Verify that stop words elements are created and functional in show_settings UI."""
    buttons = []
    inputs = []
    labels = []
    dialogs = []

    original_btn_init = Button.__init__
    original_input_init = Input.__init__
    original_label_init = Label.__init__
    original_dialog_init = Dialog.__init__

    def tracking_btn_init(self, *args, **kwargs):
        buttons.append(self)
        original_btn_init(self, *args, **kwargs)

    def tracking_input_init(self, *args, **kwargs):
        inputs.append(self)
        original_input_init(self, *args, **kwargs)

    def tracking_label_init(self, *args, **kwargs):
        labels.append(self)
        original_label_init(self, *args, **kwargs)

    def tracking_dialog_init(self, *args, **kwargs):
        dialogs.append(self)
        original_dialog_init(self, *args, **kwargs)

    Button.__init__ = tracking_btn_init
    Input.__init__ = tracking_input_init
    Label.__init__ = tracking_label_init
    Dialog.__init__ = tracking_dialog_init

    try:
        with Client(None):
            settings = AppSettings()
            # Set a controlled subset of stop words for testing
            settings.STOP_WORDS = {"the", "and", "pdf"}

            # Setup a mocked parent app with session and analyzer
            parent_app = MagicMock()
            mock_analyzer = MagicMock()
            parent_app.app_session.analyzer = mock_analyzer

            # Show settings dialog
            show_settings(parent_app, settings)

            # 1. Assert stop words title label is rendered
            custom_stop_words_label = None
            for lbl in labels:
                if lbl.text == "Custom Stop Words":
                    custom_stop_words_label = lbl
                    break
            assert custom_stop_words_label is not None, (
                "Custom Stop Words label not found"
            )

            # 2. Assert language preset buttons exist (German, French, Spanish)
            german_btn = None
            french_btn = None
            spanish_btn = None
            for btn in buttons:
                if btn.text == "German":
                    german_btn = btn
                elif btn.text == "French":
                    french_btn = btn
                elif btn.text == "Spanish":
                    spanish_btn = btn

            assert german_btn is not None
            assert french_btn is not None
            assert spanish_btn is not None

            # 3. Assert stop word input is present
            stop_word_input = None
            for inp in inputs:
                if inp._props.get("label") == "Add Stop Word":
                    stop_word_input = inp
                    break
            assert stop_word_input is not None

            # Find 'Add' button for stop word input
            add_btn = None
            for btn in buttons:
                if btn._props.get("aria-label") == "Add Stop Word Button":
                    add_btn = btn
                    break
            assert add_btn is not None

            # Test adding a stop word
            stop_word_input.value = "  Und!  "

            # Trigger click
            for l in add_btn._event_listeners.values():
                if l.type == "click":
                    l.handler(None)

            # Word should be lowercased and stripped of spaces and punctuation ("und")
            assert "und" in settings.STOP_WORDS
            # Analyzer should be reloaded
            mock_analyzer.reload_stop_words.assert_called_with(settings.STOP_WORDS)

            # Input field should be cleared
            assert stop_word_input.value == ""

            # Test loading a preset (French)
            for l in french_btn._event_listeners.values():
                if l.type == "click":
                    l.handler(None)

            # French preset words like "les", "une" should now be in stop words
            assert "et" in settings.STOP_WORDS
            assert "les" in settings.STOP_WORDS
            assert "une" in settings.STOP_WORDS

            # Clean up the timer in settings
            if settings._save_timer:
                settings._save_timer.cancel()

    finally:
        Button.__init__ = original_btn_init
        Input.__init__ = original_input_init
        Label.__init__ = original_label_init
        Dialog.__init__ = original_dialog_init
