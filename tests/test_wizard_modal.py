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
