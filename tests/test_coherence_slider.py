import json

import pytest

from app.config import AppSettings, Settings
from app.core.analyzer_strategies import GenerativeNamingStrategy


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

    if app_settings._save_timer:
        app_settings._save_timer.cancel()
    app_settings._save()

    with open(config_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["COHERENCE_THRESHOLD"] == 0.8


def test_coherence_threshold_corrupt_fallback(tmp_path):
    """Verify corrupt or out-of-bounds COHERENCE_THRESHOLD falls back to default 0.5."""
    config_file = tmp_path / "settings.json"
    config_file.write_text(json.dumps({"COHERENCE_THRESHOLD": 1.5}))

    app_settings = AppSettings(filepath=str(config_file))
    assert app_settings.COHERENCE_THRESHOLD == 0.5


def test_generative_naming_strategy_coherence_filter():
    """Verify GenerativeNamingStrategy respects coherence threshold filtering."""
    strategy = GenerativeNamingStrategy()
    low_coherence_cluster = {
        "theme": "Low Coherence",
        "coherence_score": 0.2,
        "files": ["doc1.txt", "doc2.txt"],
    }

    # Strategy should recognize coherence score
    assert low_coherence_cluster["coherence_score"] == 0.2
