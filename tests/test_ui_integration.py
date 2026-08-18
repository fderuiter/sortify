import time
from unittest.mock import MagicMock, patch

from nicegui import Client
from nicegui.elements.button import Button
from nicegui.elements.dialog import Dialog
from nicegui.elements.timer import Timer

from app.config import AppSettings
from app.core.downloader import run_background_download
from app.ui.settings import show_settings
from app.ui.wizard import show_wizard


def test_wizard_timer_and_thread_cleanup():
    # Setup track list arrays
    timers = []
    buttons = []
    dialogs = []

    # Intercept element inits
    original_timer_init = Timer.__init__
    original_btn_init = Button.__init__
    original_dialog_init = Dialog.__init__

    def tracking_timer_init(self, *args, **kwargs):
        timers.append(self)
        original_timer_init(self, *args, **kwargs)

    def tracking_btn_init(self, *args, **kwargs):
        buttons.append(self)
        original_btn_init(self, *args, **kwargs)

    def tracking_dialog_init(self, *args, **kwargs):
        dialogs.append(self)
        original_dialog_init(self, *args, **kwargs)

    # Setup interceptor patches
    Timer.__init__ = tracking_timer_init
    Button.__init__ = tracking_btn_init
    Dialog.__init__ = tracking_dialog_init

    # Intercept run_background_download to get the cancel event and tracking thread
    captured_cancel_event = [None]
    captured_threads = []
    original_run_bg = run_background_download

    def tracking_run_bg(*args, **kwargs):
        cancel_ev = kwargs.get("cancel_event") or (args[6] if len(args) > 6 else None)
        captured_cancel_event[0] = cancel_ev
        thread = original_run_bg(*args, **kwargs)
        captured_threads.append(thread)
        return thread

    # Setup urllib.request mock to download extremely slowly and safely block
    class MockResponse:
        def info(self):
            return {"Content-Length": "1048576"}

        def read(self, amt=-1):
            if captured_cancel_event[0] and not captured_cancel_event[0].is_set():
                time.sleep(0.01)
                return b"a" * min(amt, 1024)
            return b""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockOpener:
        def open(self, *args, **kwargs):
            return MockResponse()

    try:
        active_timers_before_show = [
            t for t in timers if not getattr(t, "_is_canceled", False)
        ]

        with (
            Client(None),
            patch(
                "app.core.downloader.run_background_download",
                side_effect=tracking_run_bg,
            ),
            patch("urllib.request.build_opener", return_value=MockOpener()),
            patch("shutil.disk_usage", return_value=(10**12, 10**12, 10**12)),
        ):
            settings = AppSettings()
            parent_app = MagicMock()

            # Show Wizard
            show_wizard(parent_app, settings)

            # Assert dialog was created
            assert len(dialogs) == 1
            wizard_dialog = dialogs[0]

            # Find 'Accept & Download' button
            accept_btn = None
            for btn in buttons:
                if btn.text == "Accept & Download":
                    accept_btn = btn
                    break
            assert accept_btn is not None, "Accept & Download button not found"

            # Initially there should be no active download threads and no custom timer
            assert len(captured_threads) == 0

            # Verify that sync timer was registered
            active_timers_after_show = [
                t for t in timers if not getattr(t, "_is_canceled", False)
            ]
            assert len(active_timers_after_show) > len(active_timers_before_show)

            # Click Accept & Download programmatically
            for l in accept_btn._event_listeners.values():
                if l.type == "click":
                    l.handler(None)

            # Verify that download begins
            assert len(captured_threads) == 1
            download_thread = captured_threads[0]
            assert download_thread.is_alive()

            # Find 'Cancel' button to cancel download
            cancel_btn = None
            for btn in buttons:
                if btn.text == "Cancel":
                    cancel_btn = btn
                    break
            assert cancel_btn is not None, "Cancel Download button not found"

            # Click Cancel programmatically
            for l in cancel_btn._event_listeners.values():
                if l.type == "click":
                    l.handler(None)

            # Assert that the downloader background thread is cancelled and terminated
            download_thread.join(timeout=2.0)
            assert not download_thread.is_alive()

            # Close / Dismiss the dialog via the 'dismiss' event
            dismiss_handler = None
            for l in wizard_dialog._event_listeners.values():
                if l.type == "dismiss":
                    dismiss_handler = l.handler
                    break
            assert dismiss_handler is not None, "Dismiss handler not found"

            # Execute dismiss handler (simulating dialog.close() or outside click)
            try:
                dismiss_handler(None)
            except TypeError:
                dismiss_handler()

            # Assert that the active NiceGUI timer count drops back (and our timer is cancelled)
            active_timers_final = [
                t for t in timers if not getattr(t, "_is_canceled", False)
            ]
            assert len(active_timers_final) <= len(active_timers_before_show)

            # Specifically check that the created timer was cancelled
            assert any(getattr(t, "_is_canceled", False) is True for t in timers)

    finally:
        # Restore original classes/methods to avoid side-effects
        Timer.__init__ = original_timer_init
        Button.__init__ = original_btn_init
        Dialog.__init__ = original_dialog_init


def test_settings_timer_and_thread_cleanup():
    # Setup track list arrays
    timers = []
    buttons = []
    dialogs = []

    # Intercept element inits
    original_timer_init = Timer.__init__
    original_btn_init = Button.__init__
    original_dialog_init = Dialog.__init__

    def tracking_timer_init(self, *args, **kwargs):
        timers.append(self)
        original_timer_init(self, *args, **kwargs)

    def tracking_btn_init(self, *args, **kwargs):
        buttons.append(self)
        original_btn_init(self, *args, **kwargs)

    def tracking_dialog_init(self, *args, **kwargs):
        dialogs.append(self)
        original_dialog_init(self, *args, **kwargs)

    # Setup interceptor patches
    Timer.__init__ = tracking_timer_init
    Button.__init__ = tracking_btn_init
    Dialog.__init__ = tracking_dialog_init

    # Intercept run_background_download to get the cancel event and tracking thread
    captured_cancel_event = [None]
    captured_threads = []
    original_run_bg = run_background_download

    def tracking_run_bg(*args, **kwargs):
        cancel_ev = kwargs.get("cancel_event") or (args[6] if len(args) > 6 else None)
        captured_cancel_event[0] = cancel_ev
        thread = original_run_bg(*args, **kwargs)
        captured_threads.append(thread)
        return thread

    # Setup urllib.request mock to download extremely slowly and safely block
    class MockResponse:
        def info(self):
            return {"Content-Length": "1048576"}

        def read(self, amt=-1):
            if captured_cancel_event[0] and not captured_cancel_event[0].is_set():
                time.sleep(0.01)
                return b"a" * min(amt, 1024)
            return b""

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    class MockOpener:
        def open(self, *args, **kwargs):
            return MockResponse()

    try:
        active_timers_before_show = [
            t for t in timers if not getattr(t, "_is_canceled", False)
        ]

        with (
            Client(None),
            patch(
                "app.core.downloader.run_background_download",
                side_effect=tracking_run_bg,
            ),
            patch("urllib.request.build_opener", return_value=MockOpener()),
            patch("shutil.disk_usage", return_value=(10**12, 10**12, 10**12)),
        ):
            settings = AppSettings()
            parent_app = MagicMock()

            # Show Settings
            show_settings(parent_app, settings)

            # Assert dialog was created
            assert len(dialogs) == 1
            settings_dialog = dialogs[0]

            # Find 'Download AI Model' button
            download_btn = None
            for btn in buttons:
                if btn.text == "Download AI Model":
                    download_btn = btn
                    break
            assert download_btn is not None, "Download AI Model button not found"

            # Initially there should be no active download threads and no custom timer
            assert len(captured_threads) == 0

            # Verify that sync timer was registered
            active_timers_after_show = [
                t for t in timers if not getattr(t, "_is_canceled", False)
            ]
            assert len(active_timers_after_show) > len(active_timers_before_show)

            # Click Download AI Model programmatically
            for l in download_btn._event_listeners.values():
                if l.type == "click":
                    l.handler(None)

            # Verify that download begins
            assert len(captured_threads) == 1
            download_thread = captured_threads[0]
            assert download_thread.is_alive()

            # Cancel the download programmatically via DownloadManager as settings has no cancel button
            from app.core.downloader import DownloadManager

            DownloadManager.get_instance().cancel_download()

            # Assert that the downloader background thread is cancelled and terminated
            download_thread.join(timeout=2.0)
            assert not download_thread.is_alive()

            # Close / Dismiss the dialog via the 'dismiss' event
            dismiss_handler = None
            for l in settings_dialog._event_listeners.values():
                if l.type == "dismiss":
                    dismiss_handler = l.handler
                    break
            assert dismiss_handler is not None, "Dismiss handler not found"

            # Execute dismiss handler (simulating dialog.close() or outside click)
            try:
                dismiss_handler(None)
            except TypeError:
                dismiss_handler()

            # Assert that the active NiceGUI timer count drops back (and our timer is cancelled)
            active_timers_final = [
                t for t in timers if not getattr(t, "_is_canceled", False)
            ]
            assert len(active_timers_final) <= len(active_timers_before_show)

            # Specifically check that the created timer was cancelled
            assert any(getattr(t, "_is_canceled", False) is True for t in timers)

    finally:
        # Restore original classes/methods
        Timer.__init__ = original_timer_init
        Button.__init__ = original_btn_init
        Dialog.__init__ = original_dialog_init


def test_settings_offline_importer_ui():
    buttons = []
    dialogs = []

    original_btn_init = Button.__init__
    original_dialog_init = Dialog.__init__

    def tracking_btn_init(self, *args, **kwargs):
        buttons.append(self)
        original_btn_init(self, *args, **kwargs)

    def tracking_dialog_init(self, *args, **kwargs):
        dialogs.append(self)
        original_dialog_init(self, *args, **kwargs)

    Button.__init__ = tracking_btn_init
    Dialog.__init__ = tracking_dialog_init

    try:
        with Client(None):
            settings = AppSettings()
            parent_app = MagicMock()

            show_settings(parent_app, settings)

            assert len(dialogs) == 1

            btn_texts = [b.text for b in buttons]
            assert "Browse Zip..." in btn_texts
            assert "Import Zip" in btn_texts
            assert "Browse Folder..." in btn_texts
            assert "Link Folder" in btn_texts
    finally:
        Button.__init__ = original_btn_init
        Dialog.__init__ = original_dialog_init

