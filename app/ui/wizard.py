"""Setup wizard module using NiceGUI."""

import threading

from nicegui import ui

from app.config import get_app_dir
from app.core.downloader import (
    DEFAULT_MODEL_URL,
    DiskSpaceError,
    DownloadManager,
    ModelVerificationError,
)
from app.ui.dialog_helper import get_dialog_card_classes


class ThreadSafeState:
    """A thread-safe state container.

    Provides synchronized dictionary-like access to internal state keys.
    """

    def __init__(self, **kwargs):
        self._lock = threading.Lock()
        self._state = kwargs

    def __getitem__(self, key):
        """Retrieve a value thread-safely."""
        with self._lock:
            return self._state[key]

    def __setitem__(self, key, value):
        """Store a value thread-safely."""
        with self._lock:
            self._state[key] = value


def show_wizard(parent_app, settings):
    """Show the initial setup wizard."""
    with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("md")):
        ui.label("AI Features Setup").classes("text-xl font-bold mb-4").props(
            'aria-label="Setup Wizard Title"'
        )

        # Welcome View
        welcome_container = ui.column().classes("w-full")
        with welcome_container:
            ui.label(
                "To use the Smart AutoSorter AI features, the application needs to initialize the local keyword clustering engine (TF-IDF & NMF)."
            ).classes("mb-2").props('aria-label="Setup Description"')
            ui.label(
                "Your privacy is important to us. All processing will happen entirely offline."
            ).classes("mb-4").props('aria-label="Privacy Description"')

        # Downloading View
        download_container = ui.column().classes("w-full")
        download_container.set_visibility(False)
        with download_container:
            progress_bar = (
                ui.linear_progress(value=0)
                .classes("w-full mb-2")
                .props('aria-label="Download Progress Bar"')
            )
            status_label = (
                ui.label("")
                .classes("text-sm text-gray-500 mb-4")
                .props('aria-label="Download Status"')
            )

        # Error / Diagnostic View
        error_container = ui.column().classes("w-full")
        error_container.set_visibility(False)
        with error_container:
            with ui.card().classes("bg-red-50 border-red-200 border p-4 mb-4 w-full"):
                with ui.row().classes("items-center gap-2 text-red-800"):
                    ui.icon("error", size="sm")
                    ui.label("Network and System Diagnostics").classes("font-bold")
                error_diagnostic_label = ui.label("").classes(
                    "text-red-900 text-sm mt-1"
                )

            ui.label(
                "Configure Proxy settings if behind a corporate firewall/VPN:"
            ).classes("text-xs text-gray-500 mb-1")
            proxy_input = (
                ui.input(
                    "Proxy Server (e.g. http://127.0.0.1:8080)",
                    value=getattr(settings, "PROXY", ""),
                    password=True,
                )
                .classes("w-full mb-4")
                .props('aria-label="Wizard Proxy Input"')
            )

        # State Variables

        # Periodic sync timer for wizard UI
        def sync_wizard_ui():
            dm = DownloadManager.get_instance()
            is_dl = dm.state["is_downloading"]

            if is_dl:
                if welcome_container.visible or error_container.visible:
                    welcome_container.set_visibility(False)
                    error_container.set_visibility(False)
                    download_container.set_visibility(True)
                    action_row_welcome.set_visibility(False)
                    action_row_error.set_visibility(False)
                    action_row_download.set_visibility(True)

                progress_bar.set_value(dm.state["progress"])
                status_label.set_text(dm.state["status_text"])
            else:
                if download_container.visible:
                    if dm.state["success"]:
                        settings.AI_CONSENT_GRANTED = True
                        ui.notify("Setup Complete.", type="positive")
                        if hasattr(parent_app, "update_ai_warning"):
                            parent_app.update_ai_warning()
                        dialog.close()
                    elif dm.state["error"]:
                        download_container.set_visibility(False)
                        welcome_container.set_visibility(False)
                        error_container.set_visibility(True)
                        action_row_welcome.set_visibility(False)
                        action_row_download.set_visibility(False)
                        action_row_error.set_visibility(True)

                        err = dm.state["error"]
                        if isinstance(err, DiskSpaceError):
                            error_diagnostic_label.set_text(
                                f"Diagnostic: Insufficient disk space on the target drive. Please clear some space and try again.\n(Details: {str(err)})"
                            )
                        elif isinstance(err, ModelVerificationError):
                            error_diagnostic_label.set_text(
                                f"Diagnostic: Model verification failed. The downloaded file has a mismatched cryptographic hash, indicating potential corruption or tampering.\n(Details: {str(err)})"
                            )
                        elif (
                            "PermissionError" in str(err)
                            or "denied" in str(err).lower()
                            or "blocked" in str(err).lower()
                        ):
                            error_diagnostic_label.set_text(
                                f"Diagnostic: External network connection was blocked. If a sandbox mode is active, please verify that downloaders can bypass sandbox restrictions.\n(Details: {str(err)})"
                            )
                        else:
                            error_diagnostic_label.set_text(
                                f"Diagnostic: Network download timed out or connection failed. This usually indicates VPN/firewall restrictions or an incorrect proxy setup.\n(Details: {str(err)})"
                            )

        sync_timer = ui.timer(0.1, sync_wizard_ui)

        def start_download():
            welcome_container.set_visibility(False)
            error_container.set_visibility(False)
            download_container.set_visibility(True)

            action_row_welcome.set_visibility(False)
            action_row_error.set_visibility(False)
            action_row_download.set_visibility(True)

            model_dir = str(get_app_dir() / "model")
            proxy_val = getattr(settings, "PROXY", "")

            try:
                DownloadManager.get_instance().start_download(
                    url=DEFAULT_MODEL_URL,
                    model_dir=model_dir,
                    proxy=proxy_val,
                )
            except Exception:
                pass

        def retry_download():
            # Apply edited proxy configuration back to active settings
            settings.PROXY = proxy_input.value
            start_download()

        def cancel_download():
            DownloadManager.get_instance().cancel_download()
            welcome_container.set_visibility(True)
            download_container.set_visibility(False)
            action_row_download.set_visibility(False)
            action_row_welcome.set_visibility(True)

        def accept():
            start_download()

        def decline():
            settings.AI_CONSENT_GRANTED = False
            ui.notify("Offline mode enabled.", type="info")
            if hasattr(parent_app, "update_ai_warning"):
                parent_app.update_ai_warning()
            dialog.close()

        # Welcome Buttons Layout
        action_row_welcome = ui.row().classes("w-full justify-between flex-wrap gap-2")
        with action_row_welcome:
            ui.button("Accept & Download", on_click=accept).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Accept and Download Button"')
            ui.button("Decline", on_click=decline).classes(
                "bg-gray-500 text-white"
            ).props('aria-label="Decline Button"')

        # Downloading Buttons Layout
        action_row_download = ui.row().classes("w-full justify-end flex-wrap gap-2")
        action_row_download.set_visibility(False)
        with action_row_download:
            ui.button("Cancel", on_click=cancel_download).classes(
                "bg-gray-500 text-white"
            ).props('aria-label="Cancel Download Button"')

        # Error Buttons Layout
        action_row_error = ui.row().classes("w-full justify-between flex-wrap gap-2")
        action_row_error.set_visibility(False)
        with action_row_error:
            ui.button("Retry", on_click=retry_download).classes(
                "bg-green-500 text-white"
            ).props('aria-label="Retry Download Button"')
            ui.button("Decline", on_click=decline).classes(
                "bg-gray-500 text-white"
            ).props('aria-label="Decline Button"')

        def handle_dismiss():
            sync_timer.cancel()

        dialog.on("dismiss", handle_dismiss)

    dialog.open()
