import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input, Label, Switch

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI, LabeledSwitch, SettingsModal, WizardModal


class LabeledSwitchTestApp(App):
    """Minimal test app for testing LabeledSwitch behavior."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.labeled_switch = LabeledSwitch(
            label="Enable Feature",
            value=False,
            switch_id="test-switch",
            tooltip="Test Tooltip",
        )

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Dummy Input", id="dummy-input")
        yield self.labeled_switch


def test_labeled_switch_initialization():
    """Verify LabeledSwitch initializes inner controls, tooltips, and property accessors correctly."""

    async def _test():
        app = LabeledSwitchTestApp()
        async with app.run_test():
            ls = LabeledSwitch(
                label="Test Label",
                value=True,
                switch_id="switch-test",
                tooltip="Tooltip Text",
            )
            assert ls.value is True
            assert ls.switch.value is True
            assert ls.switch.id == "switch-test"
            assert ls.tooltip == "Tooltip Text"
            assert ls.switch.tooltip == "Tooltip Text"
            assert "Test Label" in str(ls.label_widget.render())

            # Test setting value property
            ls.value = False
            assert ls.value is False
            assert ls.switch.value is False

    asyncio.run(_test())


def test_labeled_switch_click_label_toggles_and_focuses():
    """Verify clicking on the LabeledSwitch label toggles switch state and sets focus."""

    async def _test():
        app = LabeledSwitchTestApp()
        async with app.run_test() as pilot:
            ls = app.labeled_switch
            assert ls.value is False

            # Click the label widget
            await pilot.click(ls.label_widget)
            await pilot.pause(0.05)

            # Check that switch toggled to True and is now focused
            assert ls.value is True
            assert app.focused == ls.switch

            # Click label widget again
            await pilot.click(ls.label_widget)
            await pilot.pause(0.05)

            # Check that switch toggled back to False
            assert ls.value is False

    asyncio.run(_test())


def test_labeled_switch_click_switch_toggles():
    """Verify clicking directly on the Switch control toggles state cleanly without double toggling."""

    async def _test():
        app = LabeledSwitchTestApp()
        async with app.run_test() as pilot:
            ls = app.labeled_switch
            assert ls.value is False

            # Click the switch widget directly
            await pilot.click(ls.switch)
            await pilot.pause(0.05)

            # Check that switch toggled to True and is focused
            assert ls.value is True
            assert app.focused == ls.switch

    asyncio.run(_test())


def test_settings_modal_labeled_switch_click_label(tmp_path):
    """Verify clicking switch labels in SettingsModal toggles state and focuses switch."""

    async def _test():
        settings = AppSettings()
        settings.AI_CONSENT_GRANTED = False
        app = AutoSorterTUI(settings=settings, base_dir=tmp_path, skip_wizard=True)

        async with app.run_test(size=(100, 50)) as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            # Query the AI consent LabeledSwitch row and its components
            sw_ai = modal.query_one("#switch-ai-consent", Switch)
            ls_ai = sw_ai.parent
            assert isinstance(ls_ai, LabeledSwitch)
            assert sw_ai.value is False

            # Scroll visible and click the label text for AI Consent
            ls_ai.scroll_visible(animate=False)
            await pilot.pause(0.05)
            await pilot.click(ls_ai.label_widget)
            await pilot.pause(0.05)

            assert sw_ai.value is True
            assert app.focused == sw_ai

    asyncio.run(_test())


def test_wizard_modal_labeled_switch_click_label(tmp_path):
    """Verify clicking switch labels in WizardModal toggles state and focuses switch."""

    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=tmp_path)

        async with app.run_test() as pilot:
            app.push_screen(WizardModal(app.settings))
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, WizardModal)

            # Query switch and container in wizard
            sw_sample = modal.query_one("#switch-sample-corpus", Switch)
            ls_sample = sw_sample.parent
            assert isinstance(ls_sample, LabeledSwitch)
            assert sw_sample.value is False

            # Click the label text for sample documents
            await pilot.click(ls_sample.label_widget)
            await pilot.pause(0.05)

            assert sw_sample.value is True
            assert app.focused == sw_sample

    asyncio.run(_test())


def test_modal_tab_navigation_focuses_switches(tmp_path):
    """Verify Tab navigation focuses interactive Switch controls without stopping on static labels or escaping modal."""

    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=tmp_path, skip_wizard=True)

        async with app.run_test(size=(100, 50)) as pilot:
            app.action_open_settings()
            await pilot.pause(0.1)

            modal = app.screen
            assert isinstance(modal, SettingsModal)

            focused_widgets = []
            # Press Tab repeatedly to cycle through interactive controls in modal
            for _ in range(12):
                await pilot.press("tab")
                await pilot.pause(0.05)
                focused_widgets.append(app.focused)

            # Confirm focus stayed within modal interactive controls and visited Switch controls
            assert all(w is not None and w.screen is modal for w in focused_widgets)
            assert any(isinstance(w, Switch) for w in focused_widgets)
            assert not any(isinstance(w, Label) for w in focused_widgets)

    asyncio.run(_test())
