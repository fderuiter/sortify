from unittest.mock import MagicMock

from nicegui import Client
from nicegui.elements.button import Button
from nicegui.elements.input import Input
from nicegui.elements.label import Label

from app.config import AppSettings
from app.core.daemon import ContinuousWatchdogDaemon
from app.ui.settings import show_settings


def test_ui_ignored_extensions_rendering_and_interaction():
    """Verify that ignored extensions chip editor renders chips, handles dot normalization, duplicate checks, empty/invalid inputs, and chip removals."""
    buttons = []
    inputs = []
    labels = []

    original_btn_init = Button.__init__
    original_input_init = Input.__init__
    original_label_init = Label.__init__

    def tracking_btn_init(self, *args, **kwargs):
        buttons.append(self)
        original_btn_init(self, *args, **kwargs)

    def tracking_input_init(self, *args, **kwargs):
        inputs.append(self)
        original_input_init(self, *args, **kwargs)

    def tracking_label_init(self, *args, **kwargs):
        labels.append(self)
        original_label_init(self, *args, **kwargs)

    Button.__init__ = tracking_btn_init
    Input.__init__ = tracking_input_init
    Label.__init__ = tracking_label_init

    try:
        with Client(None):
            settings = AppSettings()
            settings.IGNORED_EXTENSIONS = [".crdownload", ".tmp", ".download"]

            parent_app = MagicMock()
            show_settings(parent_app, settings)

            # 1. Assert section header label is rendered
            section_label = None
            for lbl in labels:
                if lbl.text == "Ignored File Extensions":
                    section_label = lbl
                    break
            assert section_label is not None, "Ignored File Extensions label not found"

            # 2. Assert initial extension labels exist
            chip_labels = [
                lbl.text for lbl in labels if lbl.text in settings.IGNORED_EXTENSIONS
            ]
            assert ".crdownload" in chip_labels
            assert ".tmp" in chip_labels
            assert ".download" in chip_labels

            # 3. Assert input and Add button exist
            ext_input = None
            for inp in inputs:
                if inp._props.get("label") == "Add Ignored Extension":
                    ext_input = inp
                    break
            assert ext_input is not None, "Add Ignored Extension input not found"

            add_btn = None
            for btn in buttons:
                if btn._props.get("aria-label") == "Add Ignored Extension Button":
                    add_btn = btn
                    break
            assert add_btn is not None, "Add Ignored Extension Button not found"

            def click_btn(btn):
                for listener in list(btn._event_listeners.values()):
                    if listener.type == "click":
                        listener.handler(None)

            # 4. Test adding an extension without a leading dot ('part')
            ext_input.value = "part"
            click_btn(add_btn)

            assert ".part" in settings.IGNORED_EXTENSIONS
            assert ext_input.value == ""

            # 5. Test adding an extension with a leading dot ('.bak')
            ext_input.value = ".bak"
            click_btn(add_btn)

            assert ".bak" in settings.IGNORED_EXTENSIONS
            assert ext_input.value == ""

            # 6. Test duplicate detection ('PART' -> '.part')
            ext_input.value = "PART"
            initial_count = len(settings.IGNORED_EXTENSIONS)
            click_btn(add_btn)
            assert len(settings.IGNORED_EXTENSIONS) == initial_count

            # 7. Test invalid input handling (empty string)
            ext_input.value = "   "
            click_btn(add_btn)
            assert len(settings.IGNORED_EXTENSIONS) == initial_count

            # 8. Test invalid input handling (dots only)
            ext_input.value = "..."
            click_btn(add_btn)
            assert len(settings.IGNORED_EXTENSIONS) == initial_count

            # 9. Test removing an extension via close button
            tmp_delete_btns = [
                btn
                for btn in buttons
                if btn._props.get("aria-label") == "Remove extension .tmp"
            ]
            assert len(tmp_delete_btns) > 0, "Remove button for .tmp not found"
            tmp_delete_btn = tmp_delete_btns[-1]

            click_btn(tmp_delete_btn)
            assert ".tmp" not in settings.IGNORED_EXTENSIONS

            # Clean up settings timer
            if settings._save_timer:
                settings._save_timer.cancel()

    finally:
        Button.__init__ = original_btn_init
        Input.__init__ = original_input_init
        Label.__init__ = original_label_init


def test_daemon_immediate_filtering_with_updated_extensions(tmp_path):
    """Verify that updating settings.IGNORED_EXTENSIONS immediately affects watchdog filtering."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.IGNORED_EXTENSIONS = [".crdownload", ".tmp"]

    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))

    assert daemon.should_ignore_path(str(tmp_path / "download.crdownload")) is True
    assert daemon.should_ignore_path(str(tmp_path / "file.part")) is False

    # Dynamically update ignored extensions in settings
    settings.IGNORED_EXTENSIONS = [".crdownload", ".tmp", ".part"]

    assert daemon.should_ignore_path(str(tmp_path / "file.part")) is True
    assert daemon.should_ignore_path(str(tmp_path / "file.PART")) is True

    # Remove filter
    settings.IGNORED_EXTENSIONS = [".crdownload"]
    assert daemon.should_ignore_path(str(tmp_path / "file.part")) is False

    if settings._save_timer:
        settings._save_timer.cancel()
