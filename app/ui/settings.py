"""Settings module using NiceGUI."""

import threading

from nicegui import ui

from app.core.path_utils import validate_target_path
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


def get_shadowed_policies(policies: list[dict]) -> list[bool]:
    """Determine which policies are shadowed by a higher-priority policy."""
    indexed_policies = list(enumerate(policies))
    sorted_indexed = sorted(
        indexed_policies, key=lambda item: item[1].get("priority", 0), reverse=True
    )
    shadowed_indices = set()

    def is_masked_by(higher_rule, lower_rule) -> bool:
        ha_type = higher_rule.get("type", "").lower()
        lo_type = lower_rule.get("type", "").lower()
        ha_expr = higher_rule.get("expression", "").lower()
        lo_expr = lower_rule.get("expression", "").lower()

        if not ha_expr or not lo_expr:
            return False

        if ha_expr in lo_expr:
            if ha_type == "keyword":
                return True
            if ha_type == "pattern":
                if lo_type in ("pattern", "override"):
                    return True
            if ha_type == "override" and lo_type == "override":
                return True
        return False

    for i, (orig_idx, lower_rule) in enumerate(sorted_indexed):
        for higher_orig_idx, higher_rule in sorted_indexed[:i]:
            if is_masked_by(higher_rule, lower_rule):
                shadowed_indices.add(orig_idx)
                break

    return [idx in shadowed_indices for idx in range(len(policies))]


def render_validation_warning_banner(settings):
    """Render an interactive configuration warning banner with contextual tooltips and recovery links."""
    banner_card = ui.card().classes("bg-red-50 border-red-200 border p-4 mb-4 w-full")
    banner_timer = None

    def refresh():
        if getattr(banner_card, "is_deleted", False) is True:
            if banner_timer:
                try:
                    banner_timer.cancel()
                except Exception:
                    pass
            return
        banner_card.clear()
        has_errors = (
            getattr(settings, "_has_validation_errors", False) is True
            or bool(getattr(settings, "_validation_errors", None))
        )
        if not has_errors:
            banner_card.set_visibility(False)
            return

        banner_card.set_visibility(True)
        with banner_card:
            with ui.row().classes("items-center justify-between w-full flex-wrap gap-2"):
                with ui.row().classes("items-center gap-2 text-red-800"):
                    ui.icon("error", size="sm")
                    ui.label("Configuration Saves Suspended").classes("font-bold")

                def on_revalidate():
                    if hasattr(settings, "revalidate"):
                        is_valid = settings.revalidate()
                        if is_valid:
                            ui.notify(
                                "Configuration re-validated successfully! Auto-save unlocked.",
                                type="positive",
                            )
                        else:
                            ui.notify(
                                "Validation errors remain in configuration.",
                                type="warning",
                            )
                    refresh()

                ui.button("Re-validate", on_click=on_revalidate).props(
                    'size=sm color=negative aria-label="Re-validate Settings Button"'
                ).classes("font-bold")

            ui.label(
                "Automatic saving is locked because your settings file contains invalid values or errors. "
                "The system is temporarily using healthy default values to keep the application running."
            ).classes("text-red-900 text-sm mt-1").props(
                'aria-label="Configuration Warning Label"'
            )

            # Display specific errors and add a tooltip to each
            errs = getattr(settings, "_validation_errors", [])
            if errs:
                ui.label("Validation Errors Found:").classes(
                    "text-xs font-bold text-red-800 mt-2"
                )
                for err in errs:
                    err_row = ui.row().classes(
                        "items-center gap-1 text-xs text-red-700 ml-4"
                    )
                    with err_row:
                        ui.icon("arrow_right", size="xs")
                        lbl = ui.label(
                            f"Field '{err.get('field', '')}': {err.get('message', '')}"
                        ).classes("font-mono")

                        # Tooltip in plain language explaining the error
                        field_name = str(err.get("field", "")).lower()
                        msg = str(err.get("message", "")).lower()
                        tip = f"The value for '{err.get('field')}' is not allowed. "
                        if (
                            "path" in field_name
                            or "directory" in field_name
                            or "invalid path" in msg
                        ):
                            tip += "Make sure the directory path is relative, does not use '..', and has no invalid characters like :, *, ?, or |."
                        elif "empty" in msg or "required" in msg:
                            tip += (
                                "This field cannot be left blank. Please specify a value."
                            )
                        else:
                            tip += "Please ensure the value matches the requested format or number limits."
                        lbl.tooltip(tip)

            # Recovery/Troubleshooting links
            with ui.row().classes("items-center gap-2 mt-3 flex-wrap"):
                ui.icon("help", size="xs", color="primary")
                ui.link(
                    "Open Troubleshooting Guide (Online)",
                    "https://docs.smartautosorter.com/admin_guide/#configuration-recovery-troubleshooting",
                    new_tab=True,
                ).classes("text-blue-600 hover:underline text-sm").props(
                    'aria-label="Troubleshooting Guide Link"'
                )

                ui.label("|").classes("text-gray-300 text-sm")

                def show_offline_admin_guide():
                    import sys
                    from pathlib import Path

                    from app.core.path_utils import get_base_path, is_packaged
                    from app.ui.dialog_helper import get_dialog_card_classes

                    if is_packaged() and hasattr(sys, "_MEIPASS"):
                        base_dir = Path(sys._MEIPASS)
                    else:
                        base_dir = Path(get_base_path(__file__)).parent.parent

                    path = base_dir / "docs" / "admin_guide.md"
                    try:
                        if path.exists():
                            content = path.read_text(encoding="utf-8")
                        else:
                            content = f"Error: Admin guide not found at `{path}`."
                    except Exception as e:
                        content = f"Error reading admin guide: {e}"

                    with ui.dialog() as d:
                        with ui.card().classes(
                            get_dialog_card_classes("xl", "h-[80vh] flex flex-col")
                        ):
                            with ui.row().classes(
                                "w-full justify-between items-center mb-4"
                            ):
                                ui.label("Admin Guide & Troubleshooting").classes(
                                    "text-2xl font-bold"
                                )
                                ui.button("Close", on_click=d.close).classes(
                                    "bg-gray-200 text-black"
                                )
                            with ui.scroll_area().classes(
                                "w-full flex-grow border rounded p-4 overflow-y-auto"
                            ):
                                ui.markdown(content).classes("w-full")
                        d.open()

                ui.button("View Guide (Offline)", on_click=show_offline_admin_guide).props(
                    "flat dense size=sm color=primary"
                ).classes("hover:underline").props(
                    'aria-label="Offline Troubleshooting Guide Link"'
                )

    refresh()
    try:
        banner_timer = ui.timer(0.5, refresh)
        banner_card.on(
            "delete", lambda *_: banner_timer.cancel() if banner_timer else None
        )
        banner_card.timer = banner_timer
    except Exception:
        pass
    return banner_card


def show_settings(parent_app, settings):
    """Show the settings dialog."""
    timer_ref = [None]
    banner_cards = []

    def on_explorer_integration_change(e):
        import sys

        if sys.platform != "win32":
            ui.notify(
                "Context menu integration is only available on Windows.", type="warning"
            )
            e.sender.value = False
            return

        try:
            from app.core.integration import register_context_menu

            register_context_menu(e.value)
            settings.EXPLORER_INTEGRATION = e.value
            ui.notify("Explorer integration updated successfully.", type="positive")
        except Exception as ex:
            e.sender.value = not e.value
            ui.notify(f"Failed to update Explorer integration: {ex}", type="negative")

    with ui.dialog() as dialog, ui.card().classes(get_dialog_card_classes("xl")):
        with ui.row().classes(
            "w-full justify-between items-center mb-6 flex-wrap gap-2"
        ):
            ui.label("Application Settings").classes("text-2xl font-bold").props(
                'aria-label="Settings Dialog Title"'
            )
            ui.button("Close", on_click=dialog.close).classes(
                "bg-gray-200 text-black"
            ).props('aria-label="Close Settings Button"')

        with ui.tabs().classes("w-full") as tabs:
            ui.tab("General", label="General").props(
                'aria-label="General Settings Tab"'
            )
            ui.tab("AI", label="AI Configuration").props(
                'aria-label="AI Configuration Tab"'
            )
            ui.tab("Rules", label="Routing Rules").props(
                'aria-label="Routing Rules Tab"'
            )
            ui.tab("Learned Rules", label="Learned Rules").props(
                'aria-label="Learned Rules Tab"'
            )
            ui.tab("Policies", label="Policies").props('aria-label="Policies Tab"')

        with ui.tab_panels(tabs, value="General").classes("w-full mt-4"):
            with ui.tab_panel("General"):
                b1 = render_validation_warning_banner(settings)
                if b1:
                    banner_cards.append(b1)

                ui.label("System Integration").classes("text-lg font-bold mb-2")
                ui.switch(
                    "Enable Windows Explorer Context Menu",
                    value=getattr(settings, "EXPLORER_INTEGRATION", False),
                    on_change=on_explorer_integration_change,
                ).props('aria-label="Explorer integration toggle"')

                ui.label("Cleanup & Maintenance").classes("text-lg font-bold mt-4 mb-2")

                def on_cleanup_change(e):
                    try:
                        settings.CLEANUP_EMPTY_FOLDERS = e.value
                    except Exception as ex:
                        e.sender.value = settings.CLEANUP_EMPTY_FOLDERS
                        ui.notify(
                            f"Failed to update cleanup setting: {ex}", type="negative"
                        )

                ui.switch(
                    "Automatically remove empty directories",
                    value=settings.CLEANUP_EMPTY_FOLDERS,
                    on_change=on_cleanup_change,
                ).props('aria-label="Cleanup empty directories toggle"')

                ui.label("Protected Directories").classes("text-md font-bold mt-4 mb-1")
                ui.label(
                    "Never delete these empty directories (absolute paths only):"
                ).classes("text-sm text-gray-500 mb-2")

                protected_container = ui.column().classes("w-full mb-4")

                def render_protected_paths():
                    protected_container.clear()
                    paths = getattr(settings, "PROTECTED_PATHS", [])
                    with protected_container:
                        if not paths:
                            ui.label("No protected directories configured.").classes(
                                "text-sm text-gray-400 italic"
                            )
                        else:
                            for idx, path in enumerate(paths):
                                with ui.row().classes(
                                    "w-full items-center justify-between border-b pb-2 mb-2"
                                ):
                                    ui.label(path).classes("font-mono text-sm")

                                    def delete_path(idx_to_del=idx):
                                        current_paths = list(
                                            getattr(settings, "PROTECTED_PATHS", [])
                                        )
                                        if 0 <= idx_to_del < len(current_paths):
                                            removed = current_paths.pop(idx_to_del)
                                            try:
                                                settings.PROTECTED_PATHS = current_paths
                                                ui.notify(
                                                    f"Removed protection for '{removed}'.",
                                                    type="positive",
                                                )
                                                render_protected_paths()
                                            except Exception as ex:
                                                ui.notify(
                                                    f"Failed to remove path: {ex}",
                                                    type="negative",
                                                )

                                    ui.button(
                                        "Remove", on_click=delete_path, color="red"
                                    ).props("size=sm")

                render_protected_paths()

                with ui.row().classes("w-full items-center gap-4 mt-2 flex-wrap"):
                    new_path_input = ui.input("Add Protected Directory Path").props(
                        'placeholder="e.g. /absolute/path" aria-label="Add Protected Directory Path input" class="w-2/3"'
                    )

                    def add_protected_path():
                        val = new_path_input.value
                        if not val or not val.strip():
                            ui.notify("Path cannot be empty.", type="warning")
                            return
                        val = val.strip()
                        current_paths = list(getattr(settings, "PROTECTED_PATHS", []))
                        if val in current_paths:
                            ui.notify("Path is already protected.", type="warning")
                            return

                        updated_paths = current_paths + [val]
                        try:
                            settings.PROTECTED_PATHS = updated_paths
                            ui.notify(f"Protected path added: {val}", type="positive")
                            new_path_input.value = ""
                            render_protected_paths()
                        except Exception as ex:
                            ui.notify(f"Invalid absolute path: {ex}", type="negative")

                    ui.button("Add", on_click=add_protected_path).props(
                        'aria-label="Add Protected Path Button"'
                    )

                    def clear_all_protected():
                        try:
                            settings.PROTECTED_PATHS = []
                            ui.notify("Cleared all protected paths.", type="positive")
                            render_protected_paths()
                        except Exception as ex:
                            ui.notify(f"Failed to clear paths: {ex}", type="negative")

                    ui.button(
                        "Clear All", on_click=clear_all_protected, color="red"
                    ).props('aria-label="Clear All Protected Paths Button"')

                ui.label("Ignored File Extensions").classes(
                    "text-md font-bold mt-4 mb-1"
                )
                ui.label(
                    "Files matching these extensions will be skipped during scanning and file watching:"
                ).classes("text-sm text-gray-500 mb-2")

                ignored_exts_container = ui.column().classes("w-full mb-4")

                def render_ignored_extensions():
                    ignored_exts_container.clear()
                    exts = getattr(
                        settings,
                        "IGNORED_EXTENSIONS",
                        [".crdownload", ".tmp", ".download"],
                    )
                    with ignored_exts_container:
                        if not exts:
                            ui.label("No ignored extensions configured.").classes(
                                "text-sm text-gray-400 italic"
                            )
                        else:
                            for idx, ext in enumerate(exts):
                                with ui.row().classes(
                                    "w-full items-center justify-between border-b pb-2 mb-2"
                                ):
                                    ui.label(ext).classes("font-mono text-sm")

                                    def delete_ext(idx_to_del=idx):
                                        current_exts = list(
                                            getattr(settings, "IGNORED_EXTENSIONS", [])
                                        )
                                        if 0 <= idx_to_del < len(current_exts):
                                            removed = current_exts.pop(idx_to_del)
                                            try:
                                                settings.IGNORED_EXTENSIONS = (
                                                    current_exts
                                                )
                                                ui.notify(
                                                    f"Removed ignored extension '{removed}'.",
                                                    type="positive",
                                                )
                                                render_ignored_extensions()
                                            except Exception as ex:
                                                ui.notify(
                                                    f"Failed to remove extension: {ex}",
                                                    type="negative",
                                                )

                                    ui.button(
                                        "Remove", on_click=delete_ext, color="red"
                                    ).props("size=sm")

                render_ignored_extensions()

                with ui.row().classes("w-full items-center gap-4 mt-2 flex-wrap"):
                    new_ext_input = ui.input("Add Ignored Extension").props(
                        'placeholder="e.g. .tmp or tmp" aria-label="Add Ignored Extension input" class="w-2/3"'
                    )

                    def add_ignored_extension():
                        val = new_ext_input.value
                        if val is None or not str(val).strip():
                            ui.notify(
                                "Extension cannot be empty or whitespace-only.",
                                type="negative",
                            )
                            return
                        val_str = str(val).strip()
                        if not val_str or val_str == ".":
                            ui.notify(
                                "Extension cannot be empty or whitespace-only.",
                                type="negative",
                            )
                            return
                        if not val_str.startswith("."):
                            val_str = f".{val_str}"

                        current_exts = list(getattr(settings, "IGNORED_EXTENSIONS", []))
                        if val_str in current_exts:
                            ui.notify("Extension is already ignored.", type="warning")
                            return

                        updated_exts = current_exts + [val_str]
                        try:
                            settings.IGNORED_EXTENSIONS = updated_exts
                            ui.notify(
                                f"Ignored extension added: {val_str}",
                                type="positive",
                            )
                            new_ext_input.value = ""
                            render_ignored_extensions()
                        except Exception as ex:
                            ui.notify(
                                f"Failed to add extension: {ex}",
                                type="negative",
                            )

                    ui.button("Add", on_click=add_ignored_extension).props(
                        'aria-label="Add Ignored Extension Button"'
                    )

                ui.label("Processing Limits").classes("text-lg font-bold mt-4 mb-2")

                def on_max_depth_change(e):
                    try:
                        settings.MAX_DEPTH = e.value
                    except Exception as ex:
                        e.sender.value = settings.MAX_DEPTH
                        ui.notify(f"Invalid depth: {ex}", type="negative")

                ui.number(
                    "Max folder depth",
                    value=settings.MAX_DEPTH,
                    on_change=on_max_depth_change,
                ).props('aria-label="Max folder depth input"')

                def on_max_folders_change(e):
                    try:
                        settings.MAX_FOLDERS = e.value
                    except Exception as ex:
                        e.sender.value = settings.MAX_FOLDERS
                        ui.notify(f"Invalid folder limit: {ex}", type="negative")

                ui.number(
                    "Max folders",
                    value=settings.MAX_FOLDERS,
                    on_change=on_max_folders_change,
                ).props('aria-label="Max folders input"')

                with ui.expansion("Advanced Settings", icon="settings").classes(
                    "w-full mt-4"
                ):
                    ui.label("Ingestion Performance & Timeouts").classes(
                        "text-md font-bold mb-2"
                    )

                    def on_worker_change(e):
                        val = (
                            int(e.value)
                            if e.value is not None
                            else settings.MAX_WORKERS
                        )
                        if val == settings.MAX_WORKERS:
                            return
                        try:
                            settings.MAX_WORKERS = val
                        except Exception as ex:
                            e.sender.value = settings.MAX_WORKERS
                            ui.notify(f"Invalid workers: {ex}", type="negative")

                    ui.label("Worker Concurrency Limit").classes(
                        "text-sm text-gray-700 mt-2"
                    )
                    with ui.row().classes("w-full items-center gap-4"):
                        worker_slider = (
                            ui.slider(
                                min=1,
                                max=64,
                                value=settings.MAX_WORKERS,
                                step=1,
                                on_change=on_worker_change,
                            )
                            .props('aria-label="Worker Concurrency Limit" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            worker_slider, "value", backward=lambda v: f"{int(v)}"
                        )

                    def on_timeout_change(e):
                        val = (
                            int(e.value)
                            if e.value is not None
                            else settings.VISUAL_TIMEOUT
                        )
                        if val == settings.VISUAL_TIMEOUT:
                            return
                        try:
                            settings.VISUAL_TIMEOUT = val
                        except Exception as ex:
                            e.sender.value = settings.VISUAL_TIMEOUT
                            ui.notify(f"Invalid timeout: {ex}", type="negative")

                    ui.label("Visual Layout Timeout (seconds)").classes(
                        "text-sm text-gray-700 mt-4"
                    )
                    with ui.row().classes("w-full items-center gap-4"):
                        timeout_slider = (
                            ui.slider(
                                min=1,
                                max=300,
                                value=settings.VISUAL_TIMEOUT,
                                step=1,
                                on_change=on_timeout_change,
                            )
                            .props('aria-label="Visual Layout Timeout" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            timeout_slider, "value", backward=lambda v: f"{int(v)}"
                        )

                    def on_debounce_delay_change(e):
                        val = (
                            float(e.value)
                            if e.value is not None
                            else settings.DEBOUNCE_DELAY
                        )
                        val = round(val, 2)
                        if val == settings.DEBOUNCE_DELAY:
                            return
                        try:
                            if val > settings.MAX_DEBOUNCE_DELAY:
                                settings.MAX_DEBOUNCE_DELAY = val
                                try:
                                    max_debounce_slider.value = val
                                except NameError:
                                    pass
                            settings.DEBOUNCE_DELAY = val
                        except Exception as ex:
                            e.sender.value = settings.DEBOUNCE_DELAY
                            ui.notify(f"Invalid debounce delay: {ex}", type="negative")

                    ui.label("Min Debounce Delay (seconds)").classes(
                        "text-sm text-gray-700 mt-4"
                    )
                    with ui.row().classes("w-full items-center gap-4"):
                        debounce_slider = (
                            ui.slider(
                                min=0.1,
                                max=10.0,
                                value=settings.DEBOUNCE_DELAY,
                                step=0.1,
                                on_change=on_debounce_delay_change,
                            )
                            .props('aria-label="Min Debounce Delay" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            debounce_slider,
                            "value",
                            backward=lambda v: f"{float(v):.1f}",
                        )

                    def on_max_debounce_delay_change(e):
                        val = (
                            float(e.value)
                            if e.value is not None
                            else settings.MAX_DEBOUNCE_DELAY
                        )
                        val = round(val, 2)
                        if val == settings.MAX_DEBOUNCE_DELAY:
                            return
                        try:
                            if val < settings.DEBOUNCE_DELAY:
                                settings.DEBOUNCE_DELAY = val
                                try:
                                    debounce_slider.value = val
                                except NameError:
                                    pass
                            settings.MAX_DEBOUNCE_DELAY = val
                        except Exception as ex:
                            e.sender.value = settings.MAX_DEBOUNCE_DELAY
                            ui.notify(
                                f"Invalid max debounce delay: {ex}", type="negative"
                            )

                    ui.label("Max Debounce Delay (seconds)").classes(
                        "text-sm text-gray-700 mt-4"
                    )
                    with ui.row().classes("w-full items-center gap-4"):
                        max_debounce_slider = (
                            ui.slider(
                                min=0.5,
                                max=30.0,
                                value=settings.MAX_DEBOUNCE_DELAY,
                                step=0.5,
                                on_change=on_max_debounce_delay_change,
                            )
                            .props('aria-label="Max Debounce Delay" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            max_debounce_slider,
                            "value",
                            backward=lambda v: f"{float(v):.1f}",
                        )

            with ui.tab_panel("AI"):
                ui.label("Privacy Options").classes("text-lg font-bold mb-2")
                ui.label("AI processing is fully offline.").classes(
                    "text-gray-500 mb-2"
                )

                # Status Warning section
                from app.core.verifier import check_ai_status

                is_healthy, warn_msg = check_ai_status(settings)
                if not is_healthy:
                    with ui.card().classes(
                        "bg-amber-50 border-amber-200 border p-4 mb-4 w-full"
                    ):
                        with ui.row().classes(
                            "items-center gap-2 text-amber-800 flex-wrap"
                        ):
                            ui.icon("warning", size="sm")
                            ui.label("AI System Warning").classes("font-bold")
                        ui.label(
                            warn_msg or "AI models are corrupt or missing."
                        ).classes("text-amber-900 text-sm mt-1").props(
                            'aria-label="AI Offline Warning Label"'
                        )
                else:
                    with ui.card().classes(
                        "bg-green-50 border-green-200 border p-4 mb-4 w-full"
                    ):
                        with ui.row().classes(
                            "items-center gap-2 text-green-800 flex-wrap"
                        ):
                            ui.icon("check_circle", size="sm")
                            ui.label("AI System Status").classes("font-bold")
                        ui.label(
                            "AI models are loaded and healthy (Offline mode)."
                        ).classes("text-green-900 text-sm mt-1")

                def reset_model_cache():
                    import asyncio

                    from nicegui.slot import Slot

                    from app.config import get_app_dir
                    from app.core.downloader import DownloadManager
                    from app.core.shared_registry import SharedModelRegistry

                    try:
                        SharedModelRegistry.get_instance().unload_all_models()
                    except Exception:
                        pass

                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    try:
                        stack = Slot.get_stack()
                    except Exception:
                        stack = None

                    model_dir = str(get_app_dir() / "model")
                    ui.notify("Clearing model cache in background...")

                    def on_done(success, err):
                        def _notify():
                            tid = (
                                id(asyncio.current_task())
                                if asyncio.current_task()
                                else 0
                            )
                            if stack is not None:
                                Slot.stacks[tid] = stack
                            try:
                                if success:
                                    ui.notify(
                                        "Model cache cleared successfully.",
                                        type="positive",
                                    )
                                    if hasattr(parent_app, "update_ai_warning"):
                                        parent_app.update_ai_warning()
                                else:
                                    ui.notify(
                                        f"Failed to clear model cache: {err}",
                                        type="negative",
                                    )
                            finally:
                                try:
                                    if tid in Slot.stacks:
                                        del Slot.stacks[tid]
                                except Exception:
                                    pass

                        if loop:
                            loop.call_soon_threadsafe(_notify)
                        else:
                            _notify()

                    DownloadManager.get_instance().delete_model_async(
                        model_dir, on_done=on_done
                    )

                ui.button("Reset Model Cache", on_click=reset_model_cache).props(
                    'aria-label="Reset Model Cache Button"'
                )

                # Network Configuration Section
                ui.label("Network Configuration").classes("text-lg font-bold mt-4 mb-2")

                proxy_input = (
                    ui.input(
                        "Proxy Server (e.g. http://127.0.0.1:8080)",
                        value=getattr(settings, "PROXY", ""),
                        password=True,
                    )
                    .classes("w-full mb-2")
                    .props(
                        'aria-label="Proxy Input" placeholder="e.g. http://username:password@host:port"'
                    )
                )

                def save_proxy_settings():
                    settings.PROXY = proxy_input.value
                    ui.notify("Proxy settings updated.", type="positive")

                ui.button("Save Proxy Settings", on_click=save_proxy_settings).props(
                    'aria-label="Save Proxy Settings Button"'
                )

                # AI Model Acquisition Section
                ui.label("AI Model Acquisition").classes("text-lg font-bold mt-4 mb-2")

                progress_container = ui.column().classes("w-full mt-2")
                progress_container.set_visibility(False)
                with progress_container:
                    settings_progress_bar = ui.linear_progress(value=0).classes(
                        "w-full mb-1"
                    )
                    settings_status_label = ui.label("").classes(
                        "text-sm text-gray-500 mb-2"
                    )

                def trigger_on_demand_download():
                    # Save proxy setting first
                    settings.PROXY = proxy_input.value

                    from app.config import get_app_dir
                    from app.core.downloader import (
                        DEFAULT_MODEL_URL,
                        DownloadManager,
                    )

                    model_dir = str(get_app_dir() / "model")
                    proxy_val = getattr(settings, "PROXY", "")

                    try:
                        DownloadManager.get_instance().start_download(
                            url=DEFAULT_MODEL_URL,
                            model_dir=model_dir,
                            proxy=proxy_val,
                        )
                    except Exception as e:
                        ui.notify(f"Cannot start download: {e}", type="negative")

                download_button = ui.button(
                    "Download AI Model", on_click=trigger_on_demand_download
                ).props('aria-label="Download AI Model Button"')

                from app.core.downloader import DownloadManager

                def sync_settings_ui():
                    dm = DownloadManager.get_instance()
                    is_dl = dm.state["is_downloading"]

                    if is_dl:
                        download_button.disable()
                        progress_container.set_visibility(True)
                        settings_progress_bar.set_value(dm.state["progress"])
                        settings_status_label.set_text(dm.state["status_text"])
                    else:
                        if progress_container.visible:
                            # Download finished
                            if dm.state["success"]:
                                progress_container.set_visibility(False)
                                settings.AI_CONSENT_GRANTED = True
                                ui.notify(
                                    "Model download completed and verified successfully!",
                                    type="positive",
                                )
                                if hasattr(parent_app, "update_ai_warning"):
                                    parent_app.update_ai_warning()
                                # Close setting dialog to refresh the parent UI
                                dialog.close()
                            elif dm.state["error"]:
                                progress_container.set_visibility(False)
                                ui.notify(
                                    f"Download failed: {str(dm.state['error'])}",
                                    type="negative",
                                )
                                dm.state["error"] = None

                        from app.core.verifier import check_ai_status

                        is_healthy, _ = check_ai_status(settings)
                        if is_healthy:
                            download_button.disable()
                            download_button.set_text("AI Model Downloaded")
                        else:
                            download_button.enable()
                            download_button.set_text("Download AI Model")

                sync_timer = ui.timer(0.1, sync_settings_ui)

                with ui.expansion("Advanced AI Settings", icon="psychology").classes(
                    "w-full mt-4"
                ):
                    ui.label("Resource & Ingestion Thresholds").classes(
                        "text-md font-bold mb-2"
                    )

                    def on_threads_change(e):
                        val = (
                            int(e.value)
                            if e.value is not None
                            else settings.MODEL_THREADS
                        )
                        if val == settings.MODEL_THREADS:
                            return
                        try:
                            settings.MODEL_THREADS = val
                        except Exception as ex:
                            e.sender.value = settings.MODEL_THREADS
                            ui.notify(f"Invalid threads: {ex}", type="negative")

                    ui.label("ML Thread Count").classes("text-sm text-gray-700 mt-2")
                    with ui.row().classes("w-full items-center gap-4"):
                        threads_slider = (
                            ui.slider(
                                min=1,
                                max=32,
                                value=settings.MODEL_THREADS,
                                step=1,
                                on_change=on_threads_change,
                            )
                            .props('aria-label="ML Thread Count" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            threads_slider, "value", backward=lambda v: f"{int(v)}"
                        )

                    def on_img_dim_change(e):
                        val = (
                            int(e.value)
                            if e.value is not None
                            else settings.IMAGE_MAX_DIMENSION
                        )
                        if val == settings.IMAGE_MAX_DIMENSION:
                            return
                        try:
                            settings.IMAGE_MAX_DIMENSION = val
                        except Exception as ex:
                            e.sender.value = settings.IMAGE_MAX_DIMENSION
                            ui.notify(f"Invalid image dimension: {ex}", type="negative")

                    ui.label("Image Max Dimension (pixels)").classes(
                        "text-sm text-gray-700 mt-4"
                    )
                    with ui.row().classes("w-full items-center gap-4"):
                        img_dim_slider = (
                            ui.slider(
                                min=1,
                                max=5000,
                                value=settings.IMAGE_MAX_DIMENSION,
                                step=1,
                                on_change=on_img_dim_change,
                            )
                            .props('aria-label="Image Max Dimension" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            img_dim_slider, "value", backward=lambda v: f"{int(v)}"
                        )

                    def on_img_skip_change(e):
                        val = (
                            int(e.value)
                            if e.value is not None
                            else settings.IMAGE_SKIP_THRESHOLD
                        )
                        if val == settings.IMAGE_SKIP_THRESHOLD:
                            return
                        try:
                            settings.IMAGE_SKIP_THRESHOLD = val
                        except Exception as ex:
                            e.sender.value = settings.IMAGE_SKIP_THRESHOLD
                            ui.notify(
                                f"Invalid image skip threshold: {ex}", type="negative"
                            )

                    ui.label("Image Skip Threshold").classes(
                        "text-sm text-gray-700 mt-4"
                    )
                    with ui.row().classes("w-full items-center gap-4"):
                        img_skip_slider = (
                            ui.slider(
                                min=1,
                                max=10000,
                                value=settings.IMAGE_SKIP_THRESHOLD,
                                step=1,
                                on_change=on_img_skip_change,
                            )
                            .props('aria-label="Image Skip Threshold" label')
                            .classes("flex-grow")
                        )
                        ui.label().bind_text_from(
                            img_skip_slider, "value", backward=lambda v: f"{int(v)}"
                        )

                    def on_coherence_change(e):
                        val = (
                            float(e.value)
                            if e.value is not None
                            else getattr(settings, "COHERENCE_THRESHOLD", 0.5)
                        )
                        current = getattr(settings, "COHERENCE_THRESHOLD", 0.5)
                        if abs(val - current) < 1e-6:
                            return
                        try:
                            settings.COHERENCE_THRESHOLD = val
                        except Exception as ex:
                            e.sender.value = getattr(settings, "COHERENCE_THRESHOLD", 0.5)
                            ui.notify(
                                f"Invalid coherence threshold: {ex}", type="negative"
                            )

                    coherence_tooltip = (
                        "Adjust semantic clustering sensitivity. Higher values increase grouping strictness "
                        "(flagging loosely related documents for review), while lower values relax grouping strictness."
                    )

                    coherence_lbl = ui.label("Coherence Threshold").classes(
                        "text-sm text-gray-700 mt-4"
                    )
                    coherence_lbl.tooltip(coherence_tooltip)

                    with ui.row().classes("w-full items-center gap-4"):
                        coherence_slider = (
                            ui.slider(
                                min=0.0,
                                max=1.0,
                                value=getattr(settings, "COHERENCE_THRESHOLD", 0.5),
                                step=0.01,
                                on_change=on_coherence_change,
                            )
                            .props('aria-label="Coherence Threshold" label')
                            .classes("flex-grow")
                        )
                        coherence_slider.tooltip(coherence_tooltip)
                        coherence_val_lbl = ui.label().bind_text_from(
                            coherence_slider,
                            "value",
                            backward=lambda v: f"{float(v):.2f}"
                            if v is not None
                            else "0.50",
                        )
                        coherence_val_lbl.tooltip(coherence_tooltip)

                    ui.label("Hardware Acceleration & Language Support").classes(
                        "text-md font-bold mt-4 mb-2"
                    )

                    from app.core.env_helper import is_cuda_available, is_mps_available

                    cuda_ok = is_cuda_available()
                    mps_ok = is_mps_available()

                    ui.label(
                        f"CUDA Hardware Acceleration: {'Available' if cuda_ok else 'Unavailable'}"
                    ).classes(
                        "text-sm font-semibold "
                        + ("text-green-600" if cuda_ok else "text-gray-500")
                    ).props('aria-label="CUDA status label"')
                    ui.label(
                        f"MPS Hardware Acceleration: {'Available' if mps_ok else 'Unavailable'}"
                    ).classes(
                        "text-sm font-semibold "
                        + ("text-green-600" if mps_ok else "text-gray-500")
                    ).props('aria-label="MPS status label"')

                    def on_ocr_gpu_change(e):
                        try:
                            settings.OCR_GPU_ENABLED = e.value
                        except Exception as ex:
                            e.sender.value = settings.OCR_GPU_ENABLED
                            ui.notify(
                                f"Failed to update OCR GPU setting: {ex}",
                                type="negative",
                            )

                    ui.switch(
                        "Enable GPU Acceleration for OCR",
                        value=getattr(settings, "OCR_GPU_ENABLED", False),
                        on_change=on_ocr_gpu_change,
                    ).props('aria-label="OCR GPU acceleration toggle"')

                    def on_audio_gpu_change(e):
                        try:
                            settings.AUDIO_GPU_ENABLED = e.value
                        except Exception as ex:
                            e.sender.value = settings.AUDIO_GPU_ENABLED
                            ui.notify(
                                f"Failed to update Audio GPU setting: {ex}",
                                type="negative",
                            )

                    ui.switch(
                        "Enable GPU Acceleration for Audio Transcription",
                        value=getattr(settings, "AUDIO_GPU_ENABLED", False),
                        on_change=on_audio_gpu_change,
                    ).props('aria-label="Audio GPU acceleration toggle"')

                    ocr_langs_input = (
                        ui.input(
                            "OCR Target Languages (comma-separated, e.g. en,de)",
                            value=getattr(settings, "OCR_LANGUAGES", "en"),
                        )
                        .classes("w-full mb-2")
                        .props(
                            'aria-label="OCR target languages input" placeholder="e.g. en,de"'
                        )
                    )

                    def save_ocr_languages():
                        val = ocr_langs_input.value
                        if val is None:
                            val = ""
                        try:
                            settings.OCR_LANGUAGES = val
                            ui.notify(
                                "OCR target languages updated successfully.",
                                type="positive",
                            )
                        except Exception as ex:
                            ocr_langs_input.value = settings.OCR_LANGUAGES
                            error_msg = str(ex)
                            if "Value error," in error_msg:
                                error_msg = error_msg.split("Value error,")[-1].strip()
                            ui.notify(
                                f"Invalid language configuration: {error_msg}",
                                type="negative",
                            )

                    ui.button("Save OCR Languages", on_click=save_ocr_languages).props(
                        'aria-label="Save OCR Languages Button"'
                    )

                    ui.label("Custom Stop Words").classes("text-md font-bold mt-6 mb-2")
                    ui.label(
                        "Configure words that should be excluded from semantic folder and topic analysis."
                    ).classes("text-sm text-gray-500 mb-2")

                    # Language presets buttons
                    with ui.row().classes("w-full items-center gap-2 mb-4"):
                        ui.label("Load Presets:").classes(
                            "text-sm font-semibold text-gray-700"
                        )

                        def make_preset_handler(lang):
                            return lambda: load_preset(lang)

                        ui.button(
                            "German", on_click=make_preset_handler("German")
                        ).props("outline size=sm")
                        ui.button(
                            "French", on_click=make_preset_handler("French")
                        ).props("outline size=sm")
                        ui.button(
                            "Spanish", on_click=make_preset_handler("Spanish")
                        ).props("outline size=sm")

                    stopwords_container = ui.row().classes(
                        "flex-wrap gap-2 w-full max-h-60 overflow-y-auto border p-2 rounded mb-4"
                    )

                    import string

                    def render_stopwords():
                        stopwords_container.clear()
                        words = sorted(list(settings.STOP_WORDS))
                        with stopwords_container:
                            if not words:
                                ui.label("No stop words configured.").classes(
                                    "text-sm text-gray-400 italic"
                                )
                            else:
                                for word in words:
                                    with ui.row().classes(
                                        "items-center gap-1 bg-gray-100 dark:bg-gray-800 px-3 py-1 rounded-full border border-gray-200 dark:border-gray-700"
                                    ):
                                        ui.label(word).classes(
                                            "text-sm text-gray-800 dark:text-gray-200 font-medium"
                                        )

                                        def make_delete_handler(w=word):
                                            return lambda: delete_stopword(w)

                                        ui.button(
                                            icon="close", on_click=make_delete_handler()
                                        ).props(
                                            "flat round dense size=xs color=grey"
                                        ).classes("hover:text-red-500")

                    def delete_stopword(word: str):
                        new_words = set(settings.STOP_WORDS)
                        new_words.discard(word)
                        settings.STOP_WORDS = new_words

                        if (
                            parent_app
                            and hasattr(parent_app, "app_session")
                            and parent_app.app_session
                        ):
                            if (
                                hasattr(parent_app.app_session, "analyzer")
                                and parent_app.app_session.analyzer
                            ):
                                parent_app.app_session.analyzer.reload_stop_words(
                                    settings.STOP_WORDS
                                )

                        ui.notify(f"Removed stop word: {word}", type="positive")
                        render_stopwords()

                    def load_preset(lang: str):
                        presets = {
                            "German": {
                                "und",
                                "der",
                                "die",
                                "das",
                                "ist",
                                "ein",
                                "eine",
                                "ich",
                                "mit",
                                "auf",
                                "zu",
                                "den",
                                "dem",
                            },
                            "French": {
                                "et",
                                "le",
                                "la",
                                "les",
                                "est",
                                "un",
                                "une",
                                "je",
                                "avec",
                                "dans",
                                "pour",
                                "par",
                                "sur",
                            },
                            "Spanish": {
                                "y",
                                "el",
                                "la",
                                "los",
                                "es",
                                "un",
                                "una",
                                "con",
                                "en",
                                "para",
                                "por",
                                "del",
                            },
                        }

                        words_to_add = presets.get(lang, set())
                        if not words_to_add:
                            return

                        current_words = set(settings.STOP_WORDS)
                        newly_added = words_to_add - current_words

                        if not newly_added:
                            ui.notify(
                                f"All {lang} preset words are already in the list.",
                                type="warning",
                            )
                            return

                        settings.STOP_WORDS = current_words.union(newly_added)

                        if (
                            parent_app
                            and hasattr(parent_app, "app_session")
                            and parent_app.app_session
                        ):
                            if (
                                hasattr(parent_app.app_session, "analyzer")
                                and parent_app.app_session.analyzer
                            ):
                                parent_app.app_session.analyzer.reload_stop_words(
                                    settings.STOP_WORDS
                                )

                        ui.notify(
                            f"Appended {len(newly_added)} {lang} stop words.",
                            type="positive",
                        )
                        render_stopwords()

                    render_stopwords()

                    with ui.row().classes("w-full items-center gap-4 mt-2 flex-wrap"):
                        new_word_input = ui.input("Add Stop Word").props(
                            'placeholder="e.g. und" aria-label="Add Stop Word input" class="w-1/2"'
                        )

                        def add_stopword():
                            val = new_word_input.value
                            if not val or not val.strip():
                                ui.notify("Please enter a word to add.", type="warning")
                                return

                            cleaned_input = val.lower().translate(
                                str.maketrans("", "", string.punctuation)
                            )
                            added_words = [w for w in cleaned_input.split() if w]

                            if not added_words:
                                ui.notify(
                                    "Input must contain valid alphanumeric characters.",
                                    type="warning",
                                )
                                return

                            current_words = set(settings.STOP_WORDS)
                            newly_added = []
                            for w in added_words:
                                if w not in current_words:
                                    current_words.add(w)
                                    newly_added.append(w)

                            if not newly_added:
                                ui.notify(
                                    "Word(s) already in the list.", type="warning"
                                )
                                return

                            settings.STOP_WORDS = current_words

                            if (
                                parent_app
                                and hasattr(parent_app, "app_session")
                                and parent_app.app_session
                            ):
                                if (
                                    hasattr(parent_app.app_session, "analyzer")
                                    and parent_app.app_session.analyzer
                                ):
                                    parent_app.app_session.analyzer.reload_stop_words(
                                        settings.STOP_WORDS
                                    )

                            if len(newly_added) == 1:
                                ui.notify(
                                    f"Added stop word: {newly_added[0]}",
                                    type="positive",
                                )
                            else:
                                ui.notify(
                                    f"Added {len(newly_added)} stop words.",
                                    type="positive",
                                )

                            new_word_input.value = ""
                            render_stopwords()

                        ui.button("Add", on_click=add_stopword).props(
                            'aria-label="Add Stop Word Button"'
                        )

            with ui.tab_panel("Rules"):
                ui.label("Keyword Routing").classes("text-lg font-bold mb-2")

                rules_container = ui.column().classes("w-full mb-4")

                def render_rules():
                    rules_container.clear()
                    with rules_container:
                        for kw, target in settings.KEYWORD_RULES.items():
                            with ui.row().classes(
                                "w-full items-center justify-between border-b pb-2 mb-2"
                            ):
                                ui.label(kw).classes("w-1/4 font-mono")
                                ui.label(target).classes(
                                    "w-1/2 font-mono text-gray-500"
                                )

                                def delete_rule(k=kw):
                                    updated_rules = dict(settings.KEYWORD_RULES)
                                    if k in updated_rules:
                                        del updated_rules[k]
                                        settings.KEYWORD_RULES = updated_rules
                                        ui.notify(
                                            f"Rule for '{k}' deleted.", type="positive"
                                        )
                                        render_rules()

                                ui.button(
                                    "Delete", on_click=delete_rule, color="red"
                                ).props("size=sm")

                render_rules()

                ui.label("Add New Rule").classes("text-md font-bold mt-4 mb-2")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    kw_input = ui.input("Keyword").props(
                        'placeholder="e.g. invoice" aria-label="Keyword input"'
                    )
                    target_input = ui.input("Target Path").props(
                        'placeholder="Folder name" aria-label="Target Path input"'
                    )

                    def add_rule():
                        kw = kw_input.value
                        target = target_input.value
                        if not kw or not target:
                            ui.notify(
                                "Both keyword and target path are required.",
                                type="warning",
                            )
                            return

                        updated_rules = dict(settings.KEYWORD_RULES)
                        updated_rules[kw] = target
                        try:
                            settings.KEYWORD_RULES = updated_rules
                            ui.notify(f"Rule for '{kw}' added.", type="positive")
                            kw_input.value = ""
                            target_input.value = ""
                            render_rules()
                        except Exception as ex:
                            ui.notify(f"Invalid rule: {ex}", type="negative")

                    ui.button("Add Rule", on_click=add_rule).props(
                        'aria-label="Add Rule Button"'
                    )

            with ui.tab_panel("Learned Rules"):
                if getattr(settings, "_has_validation_errors", False):
                    render_validation_warning_banner(settings)

                ui.label("Learned Rules").classes("text-lg font-bold mb-1").props(
                    'aria-label="Learned Rules Section Title"'
                )
                ui.label(
                    "Inspect, search, edit, or delete automatically learned keyword-to-path associations."
                ).classes("text-sm text-gray-500 mb-4")

                search_input = (
                    ui.input(
                        "Search learned rules",
                        placeholder="Filter by keyword or path...",
                        on_change=lambda _: render_learned_rules(),
                    )
                    .classes("w-full mb-4")
                    .props('aria-label="Search learned rules" clearable dense icon="search"')
                )

                learned_rules_container = ui.column().classes("w-full mb-4")

                def render_learned_rules():
                    learned_rules_container.clear()
                    current_rules = dict(getattr(settings, "LEARNED_RULES", {}))
                    raw_query = getattr(search_input, "value", "") or ""
                    if not isinstance(raw_query, str):
                        raw_query = ""
                    query = raw_query.strip().lower()

                    if query:
                        filtered_rules = {
                            k: v
                            for k, v in current_rules.items()
                            if query in k.lower() or query in v.lower()
                        }
                    else:
                        filtered_rules = current_rules

                    with learned_rules_container:
                        if not current_rules:
                            ui.label("No active learned rules.").classes(
                                "text-sm text-gray-400 italic py-2"
                            )
                        elif not filtered_rules:
                            ui.label("No matching learned rules found.").classes(
                                "text-sm text-gray-400 italic py-2"
                            )
                        else:
                            with ui.row().classes(
                                "w-full items-center font-bold border-b pb-2 mb-2 text-sm text-gray-700 gap-2 flex-nowrap"
                            ):
                                ui.label("Keyword Pattern").classes("w-5/12")
                                ui.label("Destination Path").classes("w-5/12")
                                ui.label("Actions").classes("w-2/12 text-right")

                            for kw, target_path in list(filtered_rules.items()):
                                with ui.row().classes(
                                    "w-full items-center border-b pb-2 mb-2 gap-2 flex-nowrap"
                                ):
                                    kw_input = (
                                        ui.input(value=kw)
                                        .classes("w-5/12 font-mono text-sm")
                                        .props(
                                            f'aria-label="Keyword pattern input for {kw}" dense outline'
                                        )
                                    )
                                    path_input = (
                                        ui.input(value=target_path)
                                        .classes("w-5/12 font-mono text-sm text-gray-700")
                                        .props(
                                            f'aria-label="Destination path input for {kw}" dense outline'
                                        )
                                    )

                                    def make_kw_handler(old_k=kw, p_inp=path_input, k_inp=kw_input):
                                        def on_kw_change(e=None):
                                            val = k_inp.value if k_inp else (e.value if hasattr(e, "value") else None)
                                            new_k = (val or "").strip()
                                            if new_k == old_k:
                                                return
                                            if not new_k:
                                                ui.notify(
                                                    "Keyword pattern cannot be empty.",
                                                    type="negative",
                                                )
                                                k_inp.value = old_k
                                                return
                                            rules_copy = dict(getattr(settings, "LEARNED_RULES", {}))
                                            rules_copy.pop(old_k, None)
                                            rules_copy[new_k] = (p_inp.value or "").strip()
                                            try:
                                                settings.LEARNED_RULES = rules_copy
                                                ui.notify(
                                                    f"Updated keyword pattern to '{new_k}'.",
                                                    type="positive",
                                                )
                                                render_learned_rules()
                                            except Exception as ex:
                                                error_msg = str(ex)
                                                if "Value error," in error_msg:
                                                    error_msg = error_msg.split("Value error,")[-1].strip()
                                                ui.notify(
                                                    f"Invalid keyword update: {error_msg}",
                                                    type="negative",
                                                )
                                                k_inp.value = old_k
                                        return on_kw_change

                                    def make_path_handler(k=kw, old_p=target_path, p_inp=path_input):
                                        def on_path_change(e=None):
                                            val = p_inp.value if p_inp else (e.value if hasattr(e, "value") else None)
                                            new_p = (val or "").strip()
                                            current_rules_dict = dict(getattr(settings, "LEARNED_RULES", {}))
                                            actual_old_p = current_rules_dict.get(k, old_p)
                                            if new_p == actual_old_p:
                                                return
                                            if not new_p:
                                                ui.notify(
                                                    "Destination path cannot be empty.",
                                                    type="negative",
                                                )
                                                p_inp.value = actual_old_p
                                                return
                                            try:
                                                validate_target_path(new_p, keyword=k)
                                            except ValueError as ve:
                                                ui.notify(
                                                    f"Invalid target path: {ve}",
                                                    type="negative",
                                                )
                                                p_inp.value = actual_old_p
                                                return
                                            rules_copy = dict(current_rules_dict)
                                            rules_copy[k] = new_p
                                            try:
                                                settings.LEARNED_RULES = rules_copy
                                                ui.notify(
                                                    f"Updated destination path for '{k}'.",
                                                    type="positive",
                                                )
                                                render_learned_rules()
                                            except Exception as ex:
                                                error_msg = str(ex)
                                                if "Value error," in error_msg:
                                                    error_msg = error_msg.split("Value error,")[-1].strip()
                                                ui.notify(
                                                    f"Invalid destination path: {error_msg}",
                                                    type="negative",
                                                )
                                                p_inp.value = actual_old_p
                                        return on_path_change

                                    def make_delete_rule(k=kw):
                                        def delete_rule():
                                            rules_copy = dict(getattr(settings, "LEARNED_RULES", {}))
                                            if k in rules_copy:
                                                del rules_copy[k]
                                                try:
                                                    settings.LEARNED_RULES = rules_copy
                                                    ui.notify(
                                                        f"Learned rule for '{k}' deleted.",
                                                        type="positive",
                                                    )
                                                    render_learned_rules()
                                                except Exception as ex:
                                                    ui.notify(
                                                        f"Failed to delete rule: {ex}",
                                                        type="negative",
                                                    )
                                        return delete_rule

                                    kw_handler = make_kw_handler(kw, path_input, kw_input)
                                    path_handler = make_path_handler(kw, target_path, path_input)

                                    kw_input.on_value_change(kw_handler)
                                    kw_input.on("change", kw_handler)
                                    path_input.on_value_change(path_handler)
                                    path_input.on("change", path_handler)

                                    with ui.row().classes("w-2/12 justify-end"):
                                        ui.button(
                                            "Delete",
                                            on_click=make_delete_rule(kw),
                                            color="red",
                                            icon="delete",
                                        ).props(f'size=sm aria-label="Delete learned rule for {kw}"')

                render_learned_rules()

            with ui.tab_panel("Policies"):
                b2 = render_validation_warning_banner(settings)
                if b2:
                    banner_cards.append(b2)

                ui.label("Unified Policies").classes("text-lg font-bold mb-2")

                policies_container = ui.column().classes("w-full mb-4")

                OPTIONS = {
                    "keyword": "Contains the name",
                    "pattern": "Matches text pattern",
                    "override": "Matches text exactly",
                }

                def move_policy_up(index: int):
                    current_policies = list(getattr(settings, "POLICIES", []))
                    current_policies = sorted(
                        current_policies,
                        key=lambda x: x.get("priority", 0),
                        reverse=True,
                    )
                    if 0 < index < len(current_policies):
                        current_policies[index], current_policies[index - 1] = (
                            current_policies[index - 1],
                            current_policies[index],
                        )
                        for i, p in enumerate(current_policies):
                            p["priority"] = (len(current_policies) - i) * 10
                        try:
                            settings.POLICIES = current_policies
                            ui.notify("Policy moved up.", type="positive")
                            render_policies()
                        except Exception as ex:
                            ui.notify(
                                f"Failed to re-order policies: {ex}", type="negative"
                            )

                def move_policy_down(index: int):
                    current_policies = list(getattr(settings, "POLICIES", []))
                    current_policies = sorted(
                        current_policies,
                        key=lambda x: x.get("priority", 0),
                        reverse=True,
                    )
                    if 0 <= index < len(current_policies) - 1:
                        current_policies[index], current_policies[index + 1] = (
                            current_policies[index + 1],
                            current_policies[index],
                        )
                        for i, p in enumerate(current_policies):
                            p["priority"] = (len(current_policies) - i) * 10
                        try:
                            settings.POLICIES = current_policies
                            ui.notify("Policy moved down.", type="positive")
                            render_policies()
                        except Exception as ex:
                            ui.notify(
                                f"Failed to re-order policies: {ex}", type="negative"
                            )

                def render_policies():
                    policies_container.clear()
                    with policies_container:
                        policies_list = list(getattr(settings, "POLICIES", []))
                        policies_list = sorted(
                            policies_list,
                            key=lambda x: x.get("priority", 0),
                            reverse=True,
                        )
                        if not policies_list:
                            ui.label("No active policies configured.").classes(
                                "text-sm text-gray-400 italic"
                            )
                        else:
                            shadowed_statuses = get_shadowed_policies(policies_list)
                            for idx, policy in enumerate(policies_list):
                                with ui.row().classes(
                                    "w-full items-center justify-between border-b pb-2 mb-2 flex-wrap gap-2"
                                ):
                                    with ui.row().classes(
                                        "items-center gap-2 flex-wrap"
                                    ):
                                        friendly_type = OPTIONS.get(
                                            policy.get("type", ""),
                                            policy.get("type", "").upper(),
                                        )
                                        type_lbl = ui.label(friendly_type).classes(
                                            "w-36 font-bold"
                                        )
                                        p_type = policy.get("type", "").lower()
                                        if p_type == "keyword":
                                            type_lbl.tooltip(
                                                "Keyword rules find files containing a specific word or phrase in their text."
                                            )
                                        elif p_type == "pattern":
                                            type_lbl.tooltip(
                                                "Pattern rules find files matching a particular sequence or format of text."
                                            )
                                        elif p_type == "override":
                                            type_lbl.tooltip(
                                                "Override rules find files containing an exact match and bypass standard routing."
                                            )
                                        else:
                                            type_lbl.tooltip(
                                                "Sequential rules evaluate documents in order to determine compliance."
                                            )

                                        ui.label(policy.get("expression", "")).classes(
                                            "w-32 font-mono truncate"
                                        )
                                        ui.label(policy.get("target_path", "")).classes(
                                            "w-40 font-mono text-gray-500 truncate"
                                        )

                                        with ui.row().classes("items-center gap-1"):

                                            def make_move_up(index=idx):
                                                return lambda: move_policy_up(index)

                                            def make_move_down(index=idx):
                                                return lambda: move_policy_down(index)

                                            up_btn = ui.button(
                                                icon="arrow_upward",
                                                on_click=make_move_up(),
                                            ).props(
                                                "flat round dense size=sm aria-label='Move Up Policy Button'"
                                            )
                                            if idx == 0:
                                                up_btn.disable()

                                            down_btn = ui.button(
                                                icon="arrow_downward",
                                                on_click=make_move_down(),
                                            ).props(
                                                "flat round dense size=sm aria-label='Move Down Policy Button'"
                                            )
                                            if idx == len(policies_list) - 1:
                                                down_btn.disable()

                                        # Shadow indicator warning badge
                                        if shadowed_statuses[idx]:
                                            with ui.row().classes(
                                                "items-center gap-1 bg-amber-50 text-amber-800 px-2 py-1 rounded border border-amber-200"
                                            ):
                                                ui.icon("warning", size="xs")
                                                ui.label(
                                                    "Shadowed: A higher-priority rule matches the same criteria."
                                                ).classes("text-xs font-semibold")

                                                # Dedicated explanation help badge/icon
                                                info_icon = ui.icon(
                                                    "help_outline", size="xs"
                                                ).classes(
                                                    "cursor-pointer text-amber-800"
                                                )
                                                info_icon.tooltip(
                                                    "This rule is overridden by a higher-priority policy and will never run. "
                                                    "Click for details."
                                                )

                                                def show_shadow_help():
                                                    with ui.dialog() as d, ui.card():
                                                        ui.label(
                                                            "Overridden (Shadowed) Policy"
                                                        ).classes("text-lg font-bold")
                                                        ui.label(
                                                            "A rule is 'shadowed' when a higher-priority rule matches the "
                                                            "same text patterns or keywords. Since rules are evaluated "
                                                            "sequentially from top to bottom, the higher-priority rule "
                                                            "will always process the matching files first, preventing "
                                                            "this rule from ever executing. To fix this, change the order "
                                                            "using priority values or make your matching criteria more specific."
                                                        ).classes(
                                                            "text-sm text-gray-700"
                                                        )
                                                        ui.button(
                                                            "Close", on_click=d.close
                                                        ).classes("mt-4")
                                                    d.open()

                                                info_icon.on("click", show_shadow_help)

                                    # Halting toggle checkbox!
                                    halting_val = policy.get("halting", False)

                                    def on_halt_toggle(e, index=idx):
                                        current_policies = list(
                                            getattr(settings, "POLICIES", [])
                                        )
                                        current_policies = sorted(
                                            current_policies,
                                            key=lambda x: x.get("priority", 0),
                                            reverse=True,
                                        )
                                        if 0 <= index < len(current_policies):
                                            current_policies[index]["halting"] = e.value
                                            try:
                                                settings.POLICIES = current_policies
                                                ui.notify(
                                                    f"Halting setting updated for policy {index}.",
                                                    type="positive",
                                                )
                                            except Exception as ex:
                                                ui.notify(
                                                    f"Failed to update halting setting: {ex}",
                                                    type="negative",
                                                )
                                                render_policies()

                                    with ui.expansion(
                                        "Advanced Settings", icon="settings"
                                    ).classes("text-xs"):
                                        halt_chk = ui.checkbox(
                                            "Halt on mismatch",
                                            value=halting_val,
                                            on_change=on_halt_toggle,
                                        ).props('aria-label="Halt toggle checkbox"')
                                        halt_chk.tooltip(
                                            "Stops checking subsequent rules if this rule criteria is not met, ensuring strict order."
                                        )

                                    def delete_policy(idx_to_del=idx):
                                        current_policies = list(
                                            getattr(settings, "POLICIES", [])
                                        )
                                        current_policies = sorted(
                                            current_policies,
                                            key=lambda x: x.get("priority", 0),
                                            reverse=True,
                                        )
                                        if 0 <= idx_to_del < len(current_policies):
                                            removed = current_policies.pop(idx_to_del)
                                            for i, p in enumerate(current_policies):
                                                p["priority"] = (
                                                    len(current_policies) - i
                                                ) * 10
                                            try:
                                                settings.POLICIES = current_policies
                                                ui.notify(
                                                    f"Policy deleted: '{removed.get('expression')}'",
                                                    type="positive",
                                                )
                                                render_policies()
                                            except Exception as ex:
                                                ui.notify(
                                                    f"Failed to delete policy: {ex}",
                                                    type="negative",
                                                )

                                    ui.button(
                                        on_click=delete_policy,
                                        color="red",
                                        icon="delete",
                                    ).props('size=sm aria-label="Delete Policy Button"')

                render_policies()

                ui.label("Add New Policy").classes("text-md font-bold mt-4 mb-2")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    p_type_select = ui.select(
                        label="Type",
                        options=OPTIONS,
                        value="keyword",
                    ).classes("w-48")
                    p_type_select.tooltip(
                        "Select a rule type: Keyword (word search), Pattern (text sequences), or Override (exact match)."
                    )
                    p_expr_input = (
                        ui.input("Expression")
                        .props(
                            'placeholder="e.g. invoice" aria-label="Policy Expression input"'
                        )
                        .classes("w-40")
                    )
                    p_target_input = (
                        ui.input("Target Path")
                        .props(
                            'placeholder="Folder name" aria-label="Policy Target Path input"'
                        )
                        .classes("w-40")
                    )
                    with ui.expansion("Advanced Settings", icon="settings").classes(
                        "w-full text-xs"
                    ):
                        p_priority_input = ui.number(
                            label="Priority", value=10, step=1
                        ).classes("w-20")
                        p_priority_input.set_visibility(False)
                        p_halting_checkbox = ui.checkbox(
                            "Halt on mismatch", value=False
                        )
                        p_halting_checkbox.tooltip(
                            "Stops evaluating subsequent rules if this rule criteria is not met."
                        )

                    def add_policy():
                        p_type = p_type_select.value
                        p_expr = p_expr_input.value
                        p_target = p_target_input.value
                        p_priority = p_priority_input.value
                        p_halting = p_halting_checkbox.value

                        # 1. Block entries that do not supply a valid type, expression, path, and priority
                        if not p_type or p_type not in (
                            "keyword",
                            "pattern",
                            "override",
                        ):
                            ui.notify(
                                "Type is required and must be keyword, pattern, or override.",
                                type="warning",
                            )
                            return

                        if not p_expr or not p_expr.strip():
                            ui.notify("Expression is required.", type="warning")
                            return
                        p_expr = p_expr.strip()

                        if not p_target or not p_target.strip():
                            ui.notify("Target Path is required.", type="warning")
                            return
                        p_target = p_target.strip()

                        if p_priority is None:
                            ui.notify("Priority is required.", type="warning")
                            return
                        try:
                            p_priority = int(p_priority)
                        except ValueError:
                            ui.notify(
                                "Priority must be a valid integer.", type="warning"
                            )
                            return

                        # 2. Path validation: Reject illegal characters, absolute paths, or traversal segments
                        if any(char in '<>:"|?*' for char in p_target):
                            ui.notify(
                                "Target Path contains illegal characters.",
                                type="negative",
                            )
                            return

                        if p_target.startswith("/") or p_target.startswith("\\"):
                            ui.notify(
                                "Target Path cannot be an absolute path.",
                                type="negative",
                            )
                            return

                        segments = p_target.replace("\\", "/").split("/")
                        if ".." in segments:
                            ui.notify(
                                "Target Path cannot contain directory traversal segments (..).",
                                type="negative",
                            )
                            return

                        new_p = {
                            "type": p_type,
                            "expression": p_expr,
                            "target_path": p_target,
                            "priority": int(p_priority),
                            "halting": p_halting,
                        }

                        current_policies = list(getattr(settings, "POLICIES", []))
                        current_policies.append(new_p)
                        current_policies = sorted(
                            current_policies,
                            key=lambda x: x.get("priority", 0),
                            reverse=True,
                        )
                        for i, p in enumerate(current_policies):
                            p["priority"] = (len(current_policies) - i) * 10
                        try:
                            settings.POLICIES = current_policies
                            ui.notify(f"Policy for '{p_expr}' added.", type="positive")
                            p_expr_input.value = ""
                            p_target_input.value = ""
                            p_priority_input.value = 10
                            p_halting_checkbox.value = False
                            render_policies()
                        except Exception as ex:
                            ui.notify(f"Invalid policy: {ex}", type="negative")

                    ui.button("Add Policy", on_click=add_policy).props(
                        'aria-label="Add Policy Button"'
                    )

    def handle_dismiss():
        sync_timer.cancel()
        if timer_ref[0]:
            try:
                timer_ref[0].cancel()
            except Exception:
                pass
        for b in banner_cards:
            if hasattr(b, "timer") and b.timer:
                try:
                    b.timer.cancel()
                except Exception:
                    pass

    dialog.on("dismiss", handle_dismiss)

    dialog.open()
