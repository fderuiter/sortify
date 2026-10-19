"""Unit and integration tests for AI Consent Toggle Switch with Memory Unload."""

from unittest.mock import MagicMock

import pytest

from app.config import AppSettings
from app.core.file_renamer import FileRenamerEngine
from app.core.shared_registry import SharedModelRegistry
from app.ui.tui import AutoSorterTUI, SettingsModal, Switch


@pytest.fixture(autouse=True)
def clean_registry(tmp_path, monkeypatch):
    """Ensure SharedModelRegistry is cleared and AUTOSORTER_APP_DIR is isolated before and after each test."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    registry = SharedModelRegistry.get_instance()
    registry.unload_all_models()
    yield
    registry.unload_all_models()


@pytest.mark.anyio
async def test_settings_modal_has_ai_consent_toggle(tmp_path):
    """Verify Users can view their current AI consent status via a toggle switch in SettingsModal."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = True

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)
    async with app.run_test() as pilot:
        app.action_open_settings()
        await pilot.pause(0.1)

        modal = app.screen
        assert isinstance(modal, SettingsModal)

        sw = modal.query_one("#switch-ai-consent", Switch)
        assert sw.value is True


@pytest.mark.anyio
async def test_revoking_consent_unloads_in_memory_models_immediately(tmp_path):
    """Verify toggling AI consent switch off unloads all loaded AI models from RAM/VRAM immediately."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = True

    registry = SharedModelRegistry.get_instance()
    # Pre-populate active mock model instances in SharedModelRegistry
    mock_onnx = MagicMock()
    mock_ocr = MagicMock()
    registry._models["onnx_/fake/path.onnx"] = mock_onnx
    registry._models["easyocr"] = mock_ocr
    registry._models["easyocr_info"] = (["en"], False)

    assert registry.is_model_loaded("onnx") is True
    assert registry.is_model_loaded("easyocr") is True

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)
    async with app.run_test() as pilot:
        # Open settings and revoke AI consent
        app.action_open_settings()
        await pilot.pause(0.1)

        modal = app.screen
        assert isinstance(modal, SettingsModal)

        sw = modal.query_one("#switch-ai-consent", Switch)
        assert sw.value is True
        sw.value = False

        modal.action_save()
        await pilot.pause(0.2)

        # Verify settings state updated
        assert app.settings.AI_CONSENT_GRANTED is False

        # Verify in-memory models were immediately unloaded
        assert registry.is_model_loaded("onnx") is False
        assert registry.is_model_loaded("easyocr") is False
        assert not any(k.startswith("onnx") or k.startswith("easyocr") for k in registry._models)


def test_consent_state_persists_across_restarts(tmp_path):
    """Verify toggling consent switch persists the new consent state across application restarts."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)

    # Enable and persist
    settings.AI_CONSENT_GRANTED = True
    if hasattr(settings, "_save"):
        settings._save()

    reloaded_settings = AppSettings(filepath=settings_file)
    assert reloaded_settings.AI_CONSENT_GRANTED is True

    # Revoke and persist
    reloaded_settings.AI_CONSENT_GRANTED = False
    if hasattr(reloaded_settings, "_save"):
        reloaded_settings._save()

    reloaded_settings_2 = AppSettings(filepath=settings_file)
    assert reloaded_settings_2.AI_CONSENT_GRANTED is False


def test_disabling_consent_prevents_ai_predictions_in_renamer(tmp_path):
    """Verify disabling consent prevents file renamer tasks from executing AI predictions."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = False
    settings.AI_ASSISTED_NAMING = True

    mock_embedding_mgr = MagicMock()
    mock_embedding_mgr.is_mock = False
    mock_embedding_mgr.is_reconstruction_active.return_value = False

    renamer = FileRenamerEngine(
        runtime_settings=settings, embedding_manager=mock_embedding_mgr
    )

    doc_text = "Detailed quarterly financial budget report analysis summary document."
    filename = "report.docx"

    # Generate contextual filename with consent = False
    result_name = renamer.generate_contextual_name(
        filename, doc_text, stop_words={"report", "docx", "document"}
    )

    # Embedding manager shouldn't receive batch prediction requests
    mock_embedding_mgr.get_vectors_batch.assert_not_called()
    assert result_name is not None
    assert isinstance(result_name, str)


def test_disabling_consent_leaves_cached_model_files_on_disk_untouched(tmp_path):
    """Verify disabling consent leaves cached model files on disk untouched while stopping in-memory execution."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = True

    # Setup dummy model cache file on disk
    model_dir = tmp_path / "model_cache"
    model_dir.mkdir()
    weight_file = model_dir / "pytorch_model.bin"
    weight_file.write_bytes(b"dummy binary weights content")

    registry = SharedModelRegistry.get_instance()
    registry._models["onnx_cached_model"] = MagicMock()
    assert registry.is_model_loaded("onnx") is True

    # Revoke consent
    settings.AI_CONSENT_GRANTED = False

    # In-memory models should be unloaded
    assert registry.is_model_loaded("onnx") is False
    assert len(registry._models) == 0

    # Model file on disk must remain untouched
    assert weight_file.exists()
    assert weight_file.read_bytes() == b"dummy binary weights content"


@pytest.mark.anyio
async def test_changing_consent_state_updates_status_bar_badge_and_announcements(
    tmp_path,
):
    """Verify changing consent state immediately updates header warning labels and status notifications."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = False

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)
    async with app.run_test() as pilot:
        from textual.widgets import Static

        sb = app.query_one("#status-bar", Static)
        assert "[AI: Disabled]" in str(sb.render())

        # Update settings consent to True
        app.settings.AI_CONSENT_GRANTED = True
        await pilot.pause(0.1)

        assert "[AI: Active]" in str(sb.render())

        # Update settings consent back to False
        app.settings.AI_CONSENT_GRANTED = False
        await pilot.pause(0.1)

        assert "[AI: Disabled]" in str(sb.render())
