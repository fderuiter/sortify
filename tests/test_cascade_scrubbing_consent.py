"""Unit and integration tests for cascade scrubbing and interactive TUI consent controls."""

import pytest

from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.shared_registry import SharedModelRegistry
from app.ui.tui import (
    AutoSorterTUI,
    Button,
    ConsentRevocationModal,
    ScrubArtifactsModal,
    SettingsModal,
    Switch,
    _get_model_cache_footprint_mb,
    _get_vector_count,
)


@pytest.fixture(autouse=True)
def clean_registry_and_env(tmp_path, monkeypatch):
    """Ensure SharedModelRegistry is cleared and AUTOSORTER_APP_DIR is isolated before each test."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    registry = SharedModelRegistry.get_instance()
    registry.unload_all_models()
    yield
    registry.unload_all_models()


def test_db_get_document_vectors_count_and_clear_all(tmp_path):
    """Verify get_document_vectors_count and clear_all_document_vectors work properly."""
    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    try:
        db = Database(db_path, worker)
        base_dir = str(tmp_path)

        assert db.get_document_vectors_count() == 0

        vectors_data = [
            ("doc1.txt", [0.1, 0.2, 0.3]),
            ("doc2.txt", [0.4, 0.5, 0.6]),
            ("doc3.txt", [0.7, 0.8, 0.9]),
        ]
        db.upsert_document_vectors(base_dir, vectors_data)

        assert db.get_document_vectors_count() == 3

        db.clear_all_document_vectors()

        assert db.get_document_vectors_count() == 0
    finally:
        worker.stop()


def test_revoking_consent_triggers_database_vector_scrubbing(tmp_path):
    """Verify revoking AI consent in configuration triggers Database.scrub_all_vectors and clear_all_document_vectors."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = True

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    try:
        db = Database(db_path, worker)
        base_dir = str(tmp_path)

        # Upsert test vector records
        vectors_data = [
            ("docA.txt", [0.1, 0.2, 0.3]),
            ("docB.txt", [0.4, 0.5, 0.6]),
        ]
        db.upsert_document_vectors(base_dir, vectors_data)

        assert db.get_document_vectors_count() == 2

        # Revoke AI consent via AppSettings
        settings.AI_CONSENT_GRANTED = False

        # Verify document_vectors table is scrubbed completely
        assert db.get_document_vectors_count() == 0
    finally:
        worker.stop()


def test_get_vector_count_and_model_cache_footprint_helpers(tmp_path, monkeypatch):
    """Verify _get_vector_count and _get_model_cache_footprint_mb helper functions."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    try:
        db = Database(db_path, worker)
        db.upsert_document_vectors(str(tmp_path), [("doc1.pdf", [1.0] * 384)])

        assert _get_vector_count(db) == 1

        model_dir = tmp_path / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        model_file = model_dir / "model.onnx"
        model_file.write_bytes(b"X" * (1024 * 1024 * 2))  # 2 MB

        footprint = _get_model_cache_footprint_mb()
        assert abs(footprint - 2.0) < 0.1
    finally:
        worker.stop()


@pytest.mark.anyio
async def test_tui_scrub_artifacts_button_and_confirmation_modal(tmp_path, monkeypatch):
    """Verify clicking Scrub AI Artifacts opens ScrubArtifactsModal and removes database vectors and model cache files."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))

    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = True

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    try:
        db = Database(db_path, worker)
        db.upsert_document_vectors(str(tmp_path), [("doc_scrub.txt", [0.5] * 128)])

        model_dir = tmp_path / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "weights.onnx").write_bytes(b"sample weights")

        assert db.get_document_vectors_count() == 1
        assert (model_dir / "weights.onnx").exists()

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True, db=db)
        async with app.run_test() as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            # Switch to AI & Privacy Settings tab
            from textual.widgets import TabbedContent
            modal.query_one("#settings-tabbed-content", TabbedContent).active = "tab-ai-privacy"
            await pilot.pause(0.1)

            # Press Scrub AI Artifacts button
            btn_scrub = modal.query_one("#btn-scrub-artifacts", Button)
            btn_scrub.press()
            await pilot.pause(0.1)

            scrub_modal = app.screen
            assert isinstance(scrub_modal, ScrubArtifactsModal)
            assert scrub_modal.vector_count == 1

            # Confirm scrub
            await pilot.click("#btn-confirm-scrub")
            await pilot.pause(0.3)

            assert db.get_document_vectors_count() == 0
            assert not (model_dir / "weights.onnx").exists()
    finally:
        worker.stop()


@pytest.mark.anyio
async def test_tui_toggling_consent_off_presents_revocation_prompt(tmp_path, monkeypatch):
    """Verify toggling off AI consent switch in TUI Settings presents ConsentRevocationModal and scrub options."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))

    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings.AI_CONSENT_GRANTED = True

    db_path = tmp_path / "autosorter.db"
    worker = DBWorker()
    try:
        db = Database(db_path, worker)
        db.upsert_document_vectors(str(tmp_path), [("doc_toggle.txt", [0.2] * 64)])

        model_dir = tmp_path / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "cached_model.bin").write_bytes(b"cached bytes")

        assert db.get_document_vectors_count() == 1

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True, db=db)
        async with app.run_test() as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            from textual.widgets import TabbedContent
            modal.query_one("#settings-tabbed-content", TabbedContent).active = "tab-ai-privacy"
            await pilot.pause(0.1)

            sw = modal.query_one("#switch-ai-consent", Switch)
            assert sw.value is True

            # Toggle switch off
            sw.value = False
            await pilot.pause(0.1)

            revoke_modal = app.screen
            assert isinstance(revoke_modal, ConsentRevocationModal)
            assert revoke_modal.vector_count == 1

            # Confirm consent revocation
            await pilot.click("#btn-confirm-revoke")
            await pilot.pause(0.3)

            assert app.settings.AI_CONSENT_GRANTED is False
            assert db.get_document_vectors_count() == 0
            assert not (model_dir / "cached_model.bin").exists()
    finally:
        worker.stop()


@pytest.mark.anyio
async def test_tui_cancelling_consent_revocation_reverts_switch(tmp_path, monkeypatch):
    """Verify cancelling consent revocation modal reverts the switch state to True."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))

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

        # Toggle switch off
        sw.value = False
        await pilot.pause(0.1)

        revoke_modal = app.screen
        assert isinstance(revoke_modal, ConsentRevocationModal)

        # Cancel revocation
        await pilot.click("#btn-cancel-revoke")
        await pilot.pause(0.1)

        # Switch should revert back to True and consent should remain True
        assert sw.value is True
        assert app.settings.AI_CONSENT_GRANTED is True
