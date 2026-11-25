"""Unit and integration tests for WizardModal inline privacy card and accessibility announcements."""

import asyncio

import pytest
from textual.containers import Vertical
from textual.widgets import Label, Switch

from app.config import AppSettings
from app.ui.a11y import inspect_tui_component
from app.ui.modals.wizard import PRIVACY_GUARANTEE_ANNOUNCEMENT, WizardModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def mock_find_abandoned_sessions(monkeypatch):
    import app.core.session_catalog

    monkeypatch.setattr(
        app.core.session_catalog, "find_abandoned_sessions", lambda *a, **k: []
    )


def test_wizard_modal_privacy_card_structure_and_guarantees(tmp_path):
    """Verify WizardModal renders the inline privacy card directly above AI consent switch with all offline guarantees."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            # Requirement 1: Privacy card container exists
            card = modal.query_one("#privacy-card", Vertical)
            assert card is not None
            assert "privacy-card" in card.classes
            assert card.can_focus is True

            # Verify card is positioned directly above the AI consent switch row
            modal_box = modal.query_one(".modal-box", Vertical)
            children = list(modal_box.children)
            card_idx = children.index(card)
            # Find switch row or switch
            sw_consent = modal.query_one("#switch-consent", Switch)
            # Switch parent is LabeledSwitch, which is a child of modal_box
            labeled_switch = sw_consent.parent
            sw_row_idx = children.index(labeled_switch)
            assert card_idx == sw_row_idx - 1, (
                "Privacy card must be directly above AI consent toggle"
            )

            # Requirement 2 & 3: Badge and offline guarantees
            badge = card.query_one("#privacy-badge", Label)
            assert badge is not None
            assert "[100% On-Device & Offline]" in str(badge.render())

            card_text = " ".join(str(w.render()) for w in card.query(Label))

            # Guarantee 1: 100% local model execution
            assert "100% local model execution" in card_text
            # Guarantee 2: zero document cloud telemetry
            assert "zero document cloud telemetry" in card_text.lower()
            # Guarantee 3: encrypted local SQLCipher storage
            assert "encrypted local sqlcipher storage" in card_text.lower()

    asyncio.run(_test())


def test_wizard_modal_keyboard_navigation_highlights_privacy_card_and_switch(tmp_path):
    """Verify keyboard navigation clearly highlights both the privacy card and AI consent switch."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            card = modal.query_one("#privacy-card", Vertical)
            sw_consent = modal.query_one("#switch-consent", Switch)

            # Focus the privacy card
            card.focus()
            await pilot.pause(0.05)
            assert app.focused == card
            assert modal.focused == card

            # Advance focus using Tab to AI consent switch
            await pilot.press("tab")
            await pilot.pause(0.05)
            assert app.focused == sw_consent
            assert modal.focused == sw_consent

            # Shift-Tab navigates back to privacy card
            await pilot.press("shift+tab")
            await pilot.pause(0.05)
            assert app.focused == card

    asyncio.run(_test())


def test_wizard_modal_screen_reader_announcements_on_focus(tmp_path):
    """Verify screen reader accessibility announcements include privacy disclosure when focusing AI consent switch."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            card = modal.query_one("#privacy-card", Vertical)
            sw_consent = modal.query_one("#switch-consent", Switch)

            # Focus card and check announcement
            card.focus()
            await pilot.pause(0.05)
            messages = [a["message"] for a in modal.announcements]
            assert any(
                "privacy disclosure card focused" in m.lower()
                and "100% on-device & offline" in m.lower()
                for m in messages
            )

            # Focus switch and check announcement contains disclosure text
            sw_consent.focus()
            await pilot.pause(0.05)
            messages = [a["message"] for a in modal.announcements]
            assert any(
                "ai consent switch focused" in m.lower()
                and PRIVACY_GUARANTEE_ANNOUNCEMENT in m
                for m in messages
            )

    asyncio.run(_test())


def test_wizard_modal_screen_reader_announcements_on_toggle(tmp_path):
    """Verify screen reader announces state changes and offline guarantees when toggling AI consent switch."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings.AI_CONSENT_GRANTED = False
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            sw_consent = modal.query_one("#switch-consent", Switch)

            # Toggle enabled
            sw_consent.value = True
            await pilot.pause(0.05)

            # Toggle disabled
            sw_consent.value = False
            await pilot.pause(0.05)

            messages = [a["message"] for a in modal.announcements]
            assert "AI consent enabled." in messages
            assert "AI consent disabled." in messages
            assert any(
                "ai consent enabled" in m.lower()
                and "100% on-device & offline" in m.lower()
                for m in messages
            )
            assert any(
                "ai consent disabled" in m.lower()
                and "100% on-device & offline" in m.lower()
                for m in messages
            )

    asyncio.run(_test())


def test_wizard_modal_a11y_compliance(tmp_path):
    """Verify WizardModal passes full WCAG 2.1 accessibility compliance audits without violations."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            # Audit compliance
            audit_res = modal.audit_a11y_compliance()
            assert audit_res["compliant"] is True, (
                f"A11y violations found: {audit_res['violations']}"
            )

            violations = inspect_tui_component(modal)
            assert len(violations) == 0, f"Violations found: {violations}"

    asyncio.run(_test())


def test_wizard_modal_narrow_viewport_responsiveness(tmp_path):
    """Verify WizardModal adapts cleanly to narrow terminal viewports (<80 cols) without crashing or overflowing."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test(size=(60, 30)) as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.1)

            assert "narrow" in modal.classes
            card = modal.query_one("#privacy-card", Vertical)
            assert card is not None
            badge = card.query_one("#privacy-badge", Label)
            assert "[100% On-Device & Offline]" in str(badge.render())

    asyncio.run(_test())


def test_wizard_modal_reflects_real_local_model_status(tmp_path):
    """AC 1: Clean profile displays missing status instead of verified; verified and downloading states reflect reality."""
    import hashlib

    from app.config import get_app_dir
    from app.core.downloader import _VERIFICATION_CACHE, DownloadManager, DownloadState
    from app.core.shared_registry import SharedModelRegistry

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            # 1. Clean profile -> Missing local model weights
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            status_lbl = modal.query_one("#lbl-wizard-model-status", Label)
            rendered = str(status_lbl.render())
            assert "Missing local model weights" in rendered
            assert "verified." not in rendered.lower()
            app.pop_screen()
            await pilot.pause(0.05)

            # 2. Active download -> Downloading status
            dm = DownloadManager.get_instance()
            dm.transition_to(DownloadState.DOWNLOADING)
            dm.state["progress"] = 0.42
            modal_dl = WizardModal(settings)
            app.push_screen(modal_dl)
            await pilot.pause(0.05)
            rendered_dl = str(
                modal_dl.query_one("#lbl-wizard-model-status", Label).render()
            )
            assert "Downloading local AI model weights (42%)" in rendered_dl
            app.pop_screen()
            dm.reset_instance()
            await pilot.pause(0.05)

            # 3. Verified local bundle -> Verified status
            model_dir = get_app_dir() / "model"
            model_dir.mkdir(parents=True, exist_ok=True)
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
                (model_dir / fname).write_bytes(payload)
                expected_hashes[fname] = hashlib.sha256(payload).hexdigest()
            SharedModelRegistry.get_instance().register_expected_hashes(
                "model_download", expected_hashes
            )
            _VERIFICATION_CACHE.clear()

            # 3a. Verified local bundle WITHOUT onnxruntime -> [AI: Fallback] guidance
            from unittest.mock import patch

            with patch(
                "app.core.downloader.is_onnxruntime_available", return_value=False
            ):
                modal_no_onnx = WizardModal(settings)
                app.push_screen(modal_no_onnx)
                await pilot.pause(0.05)
                rendered_no_onnx = str(
                    modal_no_onnx.query_one("#lbl-wizard-model-status", Label).render()
                )
                assert "[AI: Fallback]" in rendered_no_onnx
                assert "smart-autosorter[ml]" in rendered_no_onnx
                app.pop_screen()
                await pilot.pause(0.05)

            # 3b. Verified local bundle WITH onnxruntime -> Verified status
            with patch(
                "app.core.downloader.is_onnxruntime_available", return_value=True
            ):
                modal_ok = WizardModal(settings)
                app.push_screen(modal_ok)
                await pilot.pause(0.05)
                rendered_ok = str(
                    modal_ok.query_one("#lbl-wizard-model-status", Label).render()
                )
                assert "Verified local AI model weights ready on disk." in rendered_ok

    asyncio.run(_test())


def test_wizard_finish_with_consent_provisions_or_falls_back_cleanly(tmp_path):
    """AC 2 & AC 3: Finishing onboarding with consent starts background download, scans immediately, and updates status badge."""
    import hashlib
    import shutil
    from unittest.mock import patch

    from textual.widgets import Button, Checkbox, Input, Static

    from app.config import get_app_dir
    from app.core.downloader import _VERIFICATION_CACHE, DownloadManager, DownloadState
    from app.core.shared_registry import SharedModelRegistry

    async def _test():
        workspace = tmp_path / "onboarding_ws"
        workspace.mkdir()
        (workspace / "invoice_2025.txt").write_text("Quarterly tax invoice $450.00")

        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(
            settings=settings, base_dir=str(workspace), skip_wizard=True
        )

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            modal.query_one("#input-dir", Input).value = str(workspace)
            modal.query_one("#switch-consent", Switch).value = True
            modal.query_one(
                "#input-wizard-proxy", Input
            ).value = "http://proxy.corp.local:8080"
            await pilot.pause(0.05)

            dm = DownloadManager.get_instance()

            def _fake_start_download(
                url, target_dir, progress_callback=None, proxy=None
            ):
                dm.transition_to(DownloadState.DOWNLOADING)
                dm.state["progress"] = 0.35

            with (
                patch.object(
                    dm, "start_download", side_effect=_fake_start_download
                ) as mock_dl,
                patch.object(app, "action_scan_directory") as mock_scan,
            ):
                modal.query_one("#btn-finish", Button).press()
                await pilot.pause(0.1)

                assert settings.AI_CONSENT_GRANTED is True
                assert settings.PROXY == "http://proxy.corp.local:8080"
                mock_dl.assert_called_once()
                assert (
                    mock_dl.call_args.kwargs["proxy"] == "http://proxy.corp.local:8080"
                )
                mock_scan.assert_called_once()

            sb = app.query_one("#status-bar", Static)
            assert "[AI: Downloading 35%]" in str(sb.render())

            # Simulate offline / failed download -> switches to [AI: Fallback]
            dm.transition_to(DownloadState.FAILED)
            dm.state["error"] = "Offline network error"
            app._poll_ai_provisioning_status()
            await pilot.pause(0.05)
            assert "[AI: Fallback]" in str(sb.render())

            # Simulate background download finishing with verified weights -> switches to [AI: Active]
            model_dir = get_app_dir() / "model"
            model_dir.mkdir(parents=True, exist_ok=True)
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
                (model_dir / fname).write_bytes(payload)
                expected_hashes[fname] = hashlib.sha256(payload).hexdigest()
            SharedModelRegistry.get_instance().register_expected_hashes(
                "model_download", expected_hashes
            )
            _VERIFICATION_CACHE.clear()
            dm.transition_to(DownloadState.DOWNLOADING)
            dm.transition_to(DownloadState.COMPLETED)
            dm.state["error"] = None

            # Without onnxruntime -> stays [AI: Fallback] even with verified weights
            with patch(
                "app.core.downloader.is_onnxruntime_available", return_value=False
            ):
                app._last_ai_poll_token = None
                app._poll_ai_provisioning_status()
                await pilot.pause(0.05)
                assert "[AI: Fallback]" in str(sb.render())

            # With onnxruntime available -> switches to [AI: Active]
            dm.transition_to(DownloadState.DOWNLOADING)
            dm.transition_to(DownloadState.COMPLETED)
            with patch(
                "app.core.downloader.is_onnxruntime_available", return_value=True
            ):
                app._last_ai_poll_token = None
                app._poll_ai_provisioning_status()
                await pilot.pause(0.05)
                assert "[AI: Active]" in str(sb.render())

            # Verify deferring download skips start_download and displays [AI: Fallback]
            shutil.rmtree(model_dir, ignore_errors=True)
            _VERIFICATION_CACHE.clear()
            dm.reset_instance()
            dm2 = DownloadManager.get_instance()

            modal2 = WizardModal(settings)
            app.push_screen(modal2)
            await pilot.pause(0.05)
            modal2.query_one("#switch-consent", Switch).value = True
            modal2.query_one("#chk-defer-download", Checkbox).value = True
            with patch.object(dm2, "start_download") as mock_dl2:
                modal2.query_one("#btn-finish", Button).press()
                await pilot.pause(0.1)
                mock_dl2.assert_not_called()
            assert "[AI: Fallback]" in str(sb.render())

    asyncio.run(_test())


def test_wizard_demo_preset_auto_enables_sample_corpus_and_renders_non_empty_plan(
    tmp_path, monkeypatch
):
    """AC 4: Choosing Demo Workspace enables sample generation automatically and produces a non-empty plan."""
    import os

    from textual.widgets import Button, Checkbox, Input, Tree

    monkeypatch.chdir(tmp_path)

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)

        async with app.run_test() as pilot:
            modal = WizardModal(settings)
            app.push_screen(modal)
            await pilot.pause(0.05)

            chk_sample = modal.query_one("#chk-sample-corpus", Checkbox)
            assert chk_sample.value is False

            # Choose Demo Workspace preset
            modal.query_one("#preset-demo", Button).press()
            await pilot.pause(0.05)

            assert chk_sample.value is True
            demo_dir = modal.query_one("#input-dir", Input).value
            assert demo_dir.endswith(os.path.join("sandbox", "demo_workspace"))

            # Finish setup and wait for workspace scan to populate non-empty plan tree
            modal.query_one("#btn-finish", Button).press()
            for _ in range(150):
                await pilot.pause(0.1)
                if app.plan:
                    break

            assert os.path.exists(os.path.join(demo_dir, "demo_finance.txt"))
            assert len(app.plan) > 0
            tree = app.query_one("#plan-tree", Tree)
            node_labels = [str(child.label) for child in tree.root.children]
            assert not any("Empty Workspace" in lbl for lbl in node_labels)

    asyncio.run(_test())


def test_wizard_80x24_unchecked_starter_packs_live_rule_counter_and_finish_toast(
    tmp_path,
):
    """AC 1 & AC 2: Opening WizardModal in 80x24 leaves starter packs unchecked, renders pinned rule counter, and emits Finish toast."""
    from unittest.mock import patch

    from textual.widgets import Button, Checkbox, Label

    from app.core.rule_templates import (
        BUILTIN_STARTER_TEMPLATES,
        get_starter_template_by_id,
    )

    async def _test():
        workspace = tmp_path / "compact_ws"
        workspace.mkdir()
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = None
        app = AutoSorterTUI(
            settings=settings, base_dir=str(workspace), skip_wizard=True
        )

        async with app.run_test(size=(80, 24)) as pilot:
            modal = WizardModal(settings, current_dir=str(workspace))
            app.push_screen(modal)
            await pilot.pause(0.1)

            assert modal.has_class("compact")
            for pack in BUILTIN_STARTER_TEMPLATES:
                assert (
                    modal.query_one(f"#chk-template-{pack.id}", Checkbox).value is False
                )

            counter = modal.query_one("#lbl-wizard-rule-summary", Label)
            finish_btn = modal.query_one("#btn-finish", Button)
            assert "Selected: 0 keyword rules, 0 policies" in str(counter.render())
            # Pinned counter sits inside .button-row directly above #btn-finish and within 80x24 viewport
            assert counter.region.y < finish_btn.region.y
            assert finish_btn.region.bottom <= 24

            # Check two starter packs -> live counter updates immediately
            modal.query_one("#chk-template-financial_tax", Checkbox).value = True
            modal.query_one("#chk-template-personal_admin", Checkbox).value = True
            await pilot.pause(0.05)

            fin_pack = get_starter_template_by_id("financial_tax")
            adm_pack = get_starter_template_by_id("personal_admin")
            assert fin_pack is not None and adm_pack is not None
            expected_kw = len(set(fin_pack.keyword_rules) | set(adm_pack.keyword_rules))
            expected_pol = len(fin_pack.policies) + len(adm_pack.policies)

            updated_counter = str(counter.render())
            assert (
                f"Selected: {expected_kw} keyword rules, {expected_pol} policies (2 packs)"
                in updated_counter
            )

            with (
                patch.object(app, "notify") as mock_notify,
                patch.object(app, "action_scan_directory"),
            ):
                finish_btn.press()
                await pilot.pause(0.1)

                mock_notify.assert_called_once()
                toast_msg = str(mock_notify.call_args.args[0])
                assert f"{expected_kw} keyword rule(s)" in toast_msg
                assert f"{expected_pol} policy rule(s)" in toast_msg

    asyncio.run(_test())
