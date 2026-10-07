"""Unit tests for DropZoneModal floating overlay and TUI integration."""

import pytest
from textual.app import App
from textual.events import Paste

from app.ui.tui import AutoSorterTUI, DropZoneModal


class DummySettings:
    """Dummy settings for testing DropZoneModal."""

    def __init__(self, tmp_path):
        self.MAX_QUEUE_CAPACITY = 100
        self.MAX_WORKERS = 2
        self.DEDUP_WINDOW = 0.1
        self.RECONCILIATION_INTERVAL = 60.0
        self.LOG_FILE = str(tmp_path / "test.log")
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.AI_CONSENT_GRANTED = False
        self.POLICIES = []
        self.PROTECTED_PATHS = [str(tmp_path / "protected")]
        self.IGNORED_EXTENSIONS = [".tmp"]
        self.SORTING_STRATEGY = "default"

    def load(self):
        pass


class ModalTestApp(App):
    """Test harness App for mounting DropZoneModal."""

    def __init__(self, settings, base_dir):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir

    def compose(self):
        from textual.widgets import Label

        yield Label("Base Screen")

    def on_mount(self) -> None:
        self.push_screen(DropZoneModal(settings=self.settings, base_dir=self.base_dir))


@pytest.mark.anyio
async def test_dropzone_modal_a11y_compliance(tmp_path):
    """Verify DropZoneModal satisfies WCAG 2.1 accessibility requirements."""
    settings = DummySettings(tmp_path)

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))
    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)
        audit = active_modal.audit_a11y_compliance()
        assert audit["compliant"] is True, (
            f"A11y violations found: {audit['violations']}"
        )


@pytest.mark.anyio
async def test_dropzone_modal_paste_and_trigger(tmp_path):
    """Test pasting file path into DropZoneModal and executing sorting worker."""
    settings = DummySettings(tmp_path)
    test_dir = tmp_path / "incoming"
    test_dir.mkdir()
    sample_file = test_dir / "invoice_123.txt"
    sample_file.write_text("Invoice details for customer 123")

    app = ModalTestApp(settings=settings, base_dir=str(test_dir))

    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Simulate paste event
        paste_evt = Paste(text=str(sample_file))
        active_modal.on_paste(paste_evt)
        await pilot.pause()

        # Trigger triage
        active_modal.trigger_drop_triage()

        # Wait for worker thread execution
        for _ in range(60):
            await pilot.pause(0.1)
            if active_modal.processed_result is not None:
                break

        assert active_modal.processed_result is not None
        assert active_modal.processed_result["status"] == "success"
        assert active_modal.processed_result["processed_count"] == 1


@pytest.mark.anyio
async def test_dropzone_modal_protected_path_rejection(tmp_path):
    """Test that DropZoneModal rejects protected paths."""
    settings = DummySettings(tmp_path)
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    prot_file = protected_dir / "secret.txt"
    prot_file.write_text("protected content")

    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        active_modal = app.screen
        assert isinstance(active_modal, DropZoneModal)

        # Input protected path
        inp = active_modal.query_one("#input-drop-paths")
        inp.value = str(prot_file)
        active_modal.trigger_drop_triage()
        await pilot.pause()

        status_lbl = active_modal.query_one("#dropzone-status")
        assert "Protected path blocked" in str(status_lbl.render())
        assert active_modal.processed_result is None


@pytest.mark.anyio
async def test_autosorter_tui_dropzone_action(tmp_path):
    """Test pressing ctrl+d or calling action_open_dropzone in AutoSorterTUI."""
    settings = DummySettings(tmp_path)
    tui = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with tui.run_test() as pilot:
        await pilot.pause()
        tui.action_open_dropzone()
        await pilot.pause()

        # Check that DropZoneModal is active on top of screen stack
        assert isinstance(tui.screen, DropZoneModal)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(tui.screen, DropZoneModal)
