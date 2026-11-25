"""Tests for dedicated interactive ModelManagerModal in Textual TUI."""

import asyncio
import os
import shutil
import tempfile
from unittest.mock import patch

import pytest
from textual.widgets import Input, Label, ProgressBar

from app.config import AppSettings
from app.core.downloader import DownloadManager
from app.ui.tui import (
    AutoSorterTUI,
    ModelManagerModal,
    SettingsModal,
    inspect_tui_component,
)

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def mock_find_abandoned_sessions(monkeypatch):
    import app.core.session_catalog

    monkeypatch.setattr(
        app.core.session_catalog, "find_abandoned_sessions", lambda *a, **k: []
    )


@pytest.fixture
def temp_workspace():
    tmp = tempfile.mkdtemp()
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_dm():
    DownloadManager.reset_instance()
    yield
    DownloadManager.reset_instance()


def test_model_manager_modal_accessibility_audit():
    """Verify ModelManagerModal passes WCAG 2.1 accessibility inspection."""
    settings = AppSettings()
    modal = ModelManagerModal(settings=settings)
    violations = inspect_tui_component(modal)
    assert len(violations) == 0, f"Accessibility violations found: {violations}"


def test_model_manager_open_via_hotkey(temp_workspace):
    """Verify Ctrl+M hotkey opens ModelManagerModal."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            title = modal.query_one("#modal-title")
            title_text = str(title.render())
            assert title_text == "Interactive Model Management"
            assert "[" not in title_text and "]" not in title_text

            # Close via Escape
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ModelManagerModal)

    asyncio.run(_test())


def test_model_manager_open_via_settings_button(temp_workspace):
    """Verify clicking Model Manager button in SettingsModal opens ModelManagerModal."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            settings_modal = app.screen
            assert isinstance(settings_modal, SettingsModal)

            settings_modal.action_open_model_manager()
            await pilot.pause(0.1)

            assert isinstance(app.screen, ModelManagerModal)

    asyncio.run(_test())


def test_model_manager_display_status_missing(temp_workspace):
    """Verify status labels when model file is missing."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            lbl_status = modal.query_one("#lbl-model-status", Label)
            assert "Missing" in str(lbl_status.content) or "Not Downloaded" in str(
                lbl_status.content
            )

    asyncio.run(_test())


def test_model_manager_start_download_and_progress(temp_workspace):
    """Verify starting download updates progress bar, throughput label, and screen reader announcements."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            dm = DownloadManager.get_instance()
            dm.state["is_downloading"] = True
            dm.state["progress"] = 0.45
            dm.state["status_text"] = "Downloaded 15.00MB of 33.33MB (45.0%)"

            modal._poll_download_status()
            await pilot.pause(0.1)

            pb = modal.query_one("#progress-bar", ProgressBar)
            assert pb.progress == pytest.approx(45.0)

            lbl_prog = modal.query_one("#lbl-progress-status", Label)
            assert "Downloaded 15.00MB" in str(lbl_prog.content)

            # Finish download
            dm.state["is_downloading"] = False
            dm.state["progress"] = 1.0
            dm.state["success"] = True
            dm.state["status_text"] = "Download complete!"

            modal._poll_download_status()
            await pilot.pause(0.1)

            assert (
                modal.get_last_announcement()
                == "Model download completed successfully."
            )

    asyncio.run(_test())


def test_model_manager_cancelled_download_never_announces_success(temp_workspace):
    """Verify that cancelled downloads never announce success even if success flag is set."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            dm = DownloadManager.get_instance()
            dm.state["is_downloading"] = True
            modal._poll_download_status()
            await pilot.pause(0.1)

            # Cancel the download
            dm.cancel_download()

            # Stale / race condition sets success flag
            dm.state["success"] = True

            modal._poll_download_status()
            await pilot.pause(0.1)

            # Announcement must be cancelled, NEVER success
            assert modal.get_last_announcement() == "Model download cancelled."
            assert (
                modal.get_last_announcement()
                != "Model download completed successfully."
            )

            lbl_prog = modal.query_one("#lbl-progress-status", Label)
            assert "cancelled" in str(lbl_prog.content).lower()

    asyncio.run(_test())


def test_model_manager_cancel_download(temp_workspace):
    """Verify clicking Cancel button stops background download."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            dm = DownloadManager.get_instance()
            dm.state["is_downloading"] = True
            modal._refresh_ui_state()

            modal.action_cancel_download()
            await pilot.pause(0.1)

            assert dm.state["is_downloading"] is False
            assert modal.get_last_announcement() == "Cancelled model download."

    asyncio.run(_test())


def test_model_manager_retry_download(temp_workspace):
    """Verify clicking Retry Download restarts model download process."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            with patch.object(DownloadManager, "start_download") as mock_start:
                modal.action_retry_download()
                await pilot.pause(0.1)

                mock_start.assert_called_once()
                assert modal.get_last_announcement() == "Retrying model download."

    asyncio.run(_test())


def test_model_manager_delete_model(temp_workspace):
    """Verify clicking Delete Model invokes delete_model_async thread-safely."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            model_dir = modal._resolve_model_dir()
            os.makedirs(model_dir, exist_ok=True)
            with open(os.path.join(model_dir, "model.onnx"), "wb") as f:
                f.write(b"dummy_weights")
            modal._refresh_ui_state()

            with patch.object(DownloadManager, "delete_model_async") as mock_delete:
                modal.action_delete_model()
                await pilot.pause(0.1)

                mock_delete.assert_called_once()
                assert "Deleting model weights" in modal.get_last_announcement()

    asyncio.run(_test())


def test_model_manager_proxy_setting_sync(temp_workspace):
    """Verify proxy input syncs with AppSettings and DownloadManager dynamically."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)

            inp_proxy = modal.query_one("#input-proxy", Input)
            inp_proxy.value = "http://127.0.0.1:8080"

            with patch.object(DownloadManager, "start_download"):
                modal.action_start_download()
                await pilot.pause(0.1)

                assert settings.PROXY == "http://127.0.0.1:8080"
                assert DownloadManager.get_instance()._proxy == "http://127.0.0.1:8080"

    asyncio.run(_test())


def test_model_manager_responsive_layout_narrow(temp_workspace):
    """Verify ModelManagerModal applies narrow class on narrow viewports (<80 width)."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test(size=(70, 24)) as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)
            assert "narrow" in modal.classes

    asyncio.run(_test())


def test_model_manager_verification_status_requires_complete_tokenizer_bundle(
    temp_workspace, monkeypatch
):
    """Removing vocab.txt or tokenizer_config.json changes verification label to an incomplete warning instead of Valid."""
    import hashlib

    from app.core.shared_registry import SharedModelRegistry

    app_dir = os.path.join(temp_workspace, "autosorter_app")
    monkeypatch.setenv("AUTOSORTER_APP_DIR", app_dir)

    bundle_payloads = {
        "model.onnx": b"onnx-weights-bytes",
        "config.json": b'{"model_type": "bert", "hidden_size": 384}',
        "tokenizer.json": b'{"version": "1.0"}',
        "tokenizer_config.json": b'{"do_lower_case": true}',
        "special_tokens_map.json": b'{"cls_token": "[CLS]"}',
        "vocab.txt": b"[PAD]\n[UNK]\n[CLS]\n[SEP]\n",
        "version.txt": b"1.0.0",
    }
    expected_hashes = {
        fname: hashlib.sha256(content).hexdigest()
        for fname, content in bundle_payloads.items()
    }
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", expected_hashes
    )

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test(size=(80, 24)) as pilot:
            app.action_open_model_manager()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, ModelManagerModal)
            model_dir = modal._resolve_model_dir()
            os.makedirs(model_dir, exist_ok=True)
            for fname, content in bundle_payloads.items():
                with open(os.path.join(model_dir, fname), "wb") as f:
                    f.write(content)

            modal._refresh_ui_state()
            await pilot.pause(0.05)

            lbl_hash = modal.query_one("#lbl-model-hash", Label)
            assert "Valid" in str(lbl_hash.content)

            # Remove vocab.txt -> Incomplete warning instead of Valid
            os.remove(os.path.join(model_dir, "vocab.txt"))
            modal._refresh_ui_state()
            await pilot.pause(0.05)
            hash_text_no_vocab = str(lbl_hash.content)
            assert "Valid" not in hash_text_no_vocab
            assert "Incomplete" in hash_text_no_vocab
            assert "vocab.txt" in hash_text_no_vocab

            # Restore vocab.txt and remove tokenizer_config.json -> Incomplete warning instead of Valid
            with open(os.path.join(model_dir, "vocab.txt"), "wb") as f:
                f.write(bundle_payloads["vocab.txt"])
            os.remove(os.path.join(model_dir, "tokenizer_config.json"))
            modal._refresh_ui_state()
            await pilot.pause(0.05)
            hash_text_no_tok_cfg = str(lbl_hash.content)
            assert "Valid" not in hash_text_no_tok_cfg
            assert "Incomplete" in hash_text_no_tok_cfg
            assert "tokenizer_config.json" in hash_text_no_tok_cfg

    asyncio.run(_test())


def test_model_manager_ml_guidance_and_80x24_button_containment(
    temp_workspace, monkeypatch
):
    """Verify ModelManagerModal shows ml install hint when onnxruntime is absent and wraps all 5 buttons inside the modal border on 80x24 terminals."""
    app_dir = os.path.join(temp_workspace, "autosorter_app")
    monkeypatch.setenv("AUTOSORTER_APP_DIR", app_dir)

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        with patch.dict("sys.modules", {"onnxruntime": None}):
            async with app.run_test(size=(80, 24)) as pilot:
                app.action_open_model_manager()
                await pilot.pause(0.1)

                modal = app.screen
                assert isinstance(modal, ModelManagerModal)

                lbl_guidance = modal.query_one("#lbl-ml-guidance", Label)
                assert lbl_guidance.display is True
                rendered_guidance = str(lbl_guidance.render())
                assert "smart-autosorter[ml]" in rendered_guidance
                assert "TF-IDF" in rendered_guidance

                modal_box = modal.query_one(".modal-box")
                box_region = modal_box.region
                assert box_region.width <= 80
                assert box_region.height <= 24

                for btn_id in (
                    "#btn-start",
                    "#btn-cancel-dl",
                    "#btn-retry",
                    "#btn-delete",
                    "#btn-close",
                ):
                    btn = modal.query_one(btn_id)
                    btn_region = btn.region
                    assert btn_region.width > 0 and btn_region.height > 0
                    assert btn_region.x >= box_region.x, (
                        f"{btn_id} left {btn_region.x} outside modal {box_region.x}"
                    )
                    assert btn_region.right <= box_region.right, (
                        f"{btn_id} right {btn_region.right} overflows modal {box_region.right}"
                    )
                    assert btn_region.y >= box_region.y, (
                        f"{btn_id} top {btn_region.y} outside modal {box_region.y}"
                    )
                    assert btn_region.bottom <= box_region.bottom, (
                        f"{btn_id} bottom {btn_region.bottom} overflows modal {box_region.bottom}"
                    )

                # Populate verified weights without onnxruntime -> literal ([AI: Fallback]) suffix renders intact
                import hashlib

                from app.core.downloader import _VERIFICATION_CACHE
                from app.core.shared_registry import SharedModelRegistry

                model_dir = os.path.join(app_dir, "model")
                os.makedirs(model_dir, exist_ok=True)
                bundle_payloads = {
                    "model.onnx": b"onnx-bytes",
                    "config.json": b'{"model_type": "bert"}',
                    "tokenizer.json": b'{"version": "1.0"}',
                    "tokenizer_config.json": b'{"do_lower_case": true}',
                    "special_tokens_map.json": b'{"cls_token": "[CLS]"}',
                    "vocab.txt": b"[PAD]\n[CLS]\n",
                    "version.txt": b"1.0.0",
                }
                expected_hashes = {}
                for fname, payload in bundle_payloads.items():
                    with open(os.path.join(model_dir, fname), "wb") as fh:
                        fh.write(payload)
                    expected_hashes[fname] = hashlib.sha256(payload).hexdigest()
                SharedModelRegistry.get_instance().register_expected_hashes(
                    "model_download", expected_hashes
                )
                _VERIFICATION_CACHE.clear()
                modal._refresh_ui_state()
                await pilot.pause(0.05)

                rendered_status = str(
                    modal.query_one("#lbl-model-status", Label).render()
                )
                assert (
                    "Status: Downloaded & Present ([AI: Fallback])" in rendered_status
                )

    asyncio.run(_test())
