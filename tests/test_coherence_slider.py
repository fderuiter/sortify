import json
import time
from unittest.mock import MagicMock

import pytest

from app.config import AppSettings, Settings
from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.ui.settings import show_settings


def test_coherence_threshold_default_and_bounds(tmp_path):
    """Verify coherence threshold default and bounds enforcement."""
    s = Settings()
    assert s.COHERENCE_THRESHOLD == 0.5

    s_valid = Settings(COHERENCE_THRESHOLD=0.75)
    assert s_valid.COHERENCE_THRESHOLD == 0.75

    with pytest.raises(Exception):
        Settings(COHERENCE_THRESHOLD=-0.1)

    with pytest.raises(Exception):
        Settings(COHERENCE_THRESHOLD=1.1)


def test_coherence_threshold_persistence(tmp_path):
    """Verify changes to COHERENCE_THRESHOLD persist to disk via debounced background writes."""
    config_file = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(config_file))
    assert app_settings.COHERENCE_THRESHOLD == 0.5

    app_settings.COHERENCE_THRESHOLD = 0.8
    assert app_settings.COHERENCE_THRESHOLD == 0.8

    # Wait for debounced timer to persist
    time.sleep(0.7)

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["COHERENCE_THRESHOLD"] == 0.8

    if app_settings._save_timer:
        app_settings._save_timer.cancel()


def test_coherence_threshold_corrupt_fallback(tmp_path):
    """Verify corrupt or out-of-bounds COHERENCE_THRESHOLD falls back to default 0.5."""
    config_file = tmp_path / "settings.json"
    config_file.write_text(json.dumps({"COHERENCE_THRESHOLD": 1.5}))

    app_settings = AppSettings(filepath=str(config_file))
    assert app_settings.COHERENCE_THRESHOLD == 0.5

    if app_settings._save_timer:
        app_settings._save_timer.cancel()


def test_generative_naming_strategy_uses_coherence_threshold():
    """Verify document analysis applies updated COHERENCE_THRESHOLD to document grouping."""
    strategy = GenerativeNamingStrategy()
    mock_settings = Settings(COHERENCE_THRESHOLD=0.99)
    strategy.settings = mock_settings

    filenames = ["doc1.txt", "doc2.txt"]
    documents = ["Content 1", "Content 2"]
    vec1 = [1.0, 0.0, 0.0]
    vec2 = [0.5, 0.866, 0.0]  # Cosine similarity = 0.5 < 0.99

    strategy._vector_map = {"doc1.txt": vec1, "doc2.txt": vec2}
    plan = {"Folder A": {"doc1.txt": None, "doc2.txt": None}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.core.analyzer_strategies.RecursiveKMeansStrategy.generate_plan",
            lambda self, *args, **kwargs: (plan, 0.0),
        )
        res_plan, err = strategy.generate_plan(filenames, documents, max_folders=5, stop_words=set())
        assert "Review Required" in res_plan


def test_ui_coherence_threshold_slider_rendering():
    """Verify that Coherence Threshold slider control renders correctly in settings UI."""
    from nicegui import Client
    from nicegui.elements.label import Label
    from nicegui.elements.slider import Slider

    sliders = []
    labels = []

    orig_slider_init = Slider.__init__
    orig_label_init = Label.__init__

    def track_slider_init(self, *args, **kwargs):
        sliders.append(self)
        orig_slider_init(self, *args, **kwargs)

    def track_label_init(self, *args, **kwargs):
        labels.append(self)
        orig_label_init(self, *args, **kwargs)

    Slider.__init__ = track_slider_init
    Label.__init__ = track_label_init

    try:
        with Client(None):
            settings = AppSettings()
            parent_app = MagicMock()

            show_settings(parent_app, settings)

            # Check that Coherence Threshold label is present
            coherence_lbl = None
            for lbl in labels:
                if lbl.text == "Coherence Threshold":
                    coherence_lbl = lbl
                    break
            assert coherence_lbl is not None, "Coherence Threshold label not found"

            # Check that slider with min=0.0 and max=1.0 is present
            coherence_slider = None
            for s in sliders:
                if getattr(s, "_props", {}).get("aria-label") == "Coherence Threshold":
                    coherence_slider = s
                    break
            assert coherence_slider is not None, "Coherence Threshold slider not found"
            assert coherence_slider._props["min"] == 0.0
            assert coherence_slider._props["max"] == 1.0
            assert coherence_slider.value == 0.5

            if settings._save_timer:
                settings._save_timer.cancel()
    finally:
        Slider.__init__ = orig_slider_init
        Label.__init__ = orig_label_init

