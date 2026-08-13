"""Settings module using NiceGUI."""

from nicegui import ui

from app.ui.dialog_helper import get_dialog_card_classes


def show_settings(parent_app, settings):
    """Show the settings dialog."""

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

        with ui.tab_panels(tabs, value="General").classes("w-full mt-4"):
            with ui.tab_panel("General"):
                if getattr(settings, "_has_validation_errors", False):
                    with ui.card().classes(
                        "bg-red-50 border-red-200 border p-4 mb-4 w-full"
                    ):
                        with ui.row().classes("items-center gap-2 text-red-800"):
                            ui.icon("error", size="sm")
                            ui.label("Configuration Warning").classes("font-bold")
                        ui.label(
                            "One or more settings in your configuration file were invalid. "
                            "Default values are being used temporarily to prevent app crash, and saving is suspended "
                            "until the configuration file is fixed or reset."
                        ).classes("text-red-900 text-sm mt-1").props(
                            'aria-label="Configuration Warning Label"'
                        )

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
                    import shutil

                    from nicegui import run

                    from app.config import get_app_dir

                    model_dir = get_app_dir() / "model"

                    ui.notify("Clearing model cache in background...")

                    async def do_reset():
                        await run.io_bound(shutil.rmtree, model_dir, ignore_errors=True)
                        ui.notify("Model cache cleared successfully.", type="positive")

                    import asyncio

                    asyncio.create_task(do_reset())

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
                        password_toggle_button=True,
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

                import threading

                cancel_event = threading.Event()
                timer_ref = [None]

                def update_settings_timer_tick(state):
                    if state["success"]:
                        if timer_ref[0]:
                            timer_ref[0].cancel()
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
                        return

                    if state["error"]:
                        if timer_ref[0]:
                            timer_ref[0].cancel()
                        progress_container.set_visibility(False)
                        ui.notify(
                            f"Download failed: {str(state['error'])}", type="negative"
                        )
                        return

                    settings_progress_bar.set_value(state["progress"])
                    settings_status_label.set_text(state["status_text"])

                def trigger_on_demand_download():
                    # Save proxy setting first
                    settings.PROXY = proxy_input.value

                    progress_container.set_visibility(True)
                    cancel_event.clear()

                    state = {
                        "progress": 0.0,
                        "status_text": "Starting background download...",
                        "error": None,
                        "success": False,
                    }

                    def progress_cb(downloaded, total):
                        if total > 0:
                            state["progress"] = downloaded / total
                            state["status_text"] = (
                                f"Downloaded {downloaded / (1024 * 1024):.2f}MB of {total / (1024 * 1024):.2f}MB"
                            )
                        else:
                            state["progress"] = 0.0
                            state["status_text"] = (
                                f"Downloaded {downloaded / (1024 * 1024):.2f}MB..."
                            )

                    def on_success():
                        state["success"] = True

                    def on_failure(err):
                        state["error"] = err

                    from app.config import get_app_dir
                    from app.core.downloader import (
                        DEFAULT_MODEL_URL,
                        run_background_download,
                    )

                    model_dir = str(get_app_dir() / "model")
                    proxy_val = getattr(settings, "PROXY", "")

                    run_background_download(
                        url=DEFAULT_MODEL_URL,
                        model_dir=model_dir,
                        proxy=proxy_val,
                        progress_callback=progress_cb,
                        on_success=on_success,
                        on_failure=on_failure,
                        cancel_event=cancel_event,
                    )

                    timer_ref[0] = ui.timer(
                        0.1, lambda: update_settings_timer_tick(state)
                    )

                ui.button(
                    "Download AI Model", on_click=trigger_on_demand_download
                ).props('aria-label="Download AI Model Button"')

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

                ui.label("Unified Policies").classes("text-lg font-bold mt-6 mb-2")

                policies_container = ui.column().classes("w-full mb-4")

                def render_policies():
                    policies_container.clear()
                    with policies_container:
                        policies_list = list(getattr(settings, "POLICIES", []))
                        if not policies_list:
                            ui.label("No active policies configured.").classes(
                                "text-sm text-gray-400 italic"
                            )
                        else:
                            for idx, policy in enumerate(policies_list):
                                with ui.row().classes(
                                    "w-full items-center justify-between border-b pb-2 mb-2 flex-wrap gap-2"
                                ):
                                    ui.label(
                                        f"[{policy.get('type', '').upper()}]"
                                    ).classes("w-20 font-bold")
                                    ui.label(policy.get("expression", "")).classes(
                                        "w-32 font-mono truncate"
                                    )
                                    ui.label(policy.get("target_path", "")).classes(
                                        "w-40 font-mono text-gray-500 truncate"
                                    )
                                    ui.label(
                                        f"Priority: {policy.get('priority', 0)}"
                                    ).classes("w-24 text-sm")

                                    # Halting toggle checkbox!
                                    halting_val = policy.get("halting", False)

                                    def on_halt_toggle(e, index=idx):
                                        current_policies = list(
                                            getattr(settings, "POLICIES", [])
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

                                    ui.checkbox(
                                        "Halt on mismatch",
                                        value=halting_val,
                                        on_change=on_halt_toggle,
                                    ).props('aria-label="Halt toggle checkbox"')

                                    def delete_policy(idx_to_del=idx):
                                        current_policies = list(
                                            getattr(settings, "POLICIES", [])
                                        )
                                        if 0 <= idx_to_del < len(current_policies):
                                            removed = current_policies.pop(idx_to_del)
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
                                        "Delete", on_click=delete_policy, color="red"
                                    ).props("size=sm")

                render_policies()

                ui.label("Add New Policy").classes("text-md font-bold mt-4 mb-2")
                with ui.row().classes("w-full items-center gap-4 flex-wrap"):
                    p_type_select = ui.select(
                        label="Type",
                        options=["keyword", "pattern", "override"],
                        value="keyword",
                    ).classes("w-32")
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
                    p_priority_input = ui.number(
                        label="Priority", value=10, step=1
                    ).classes("w-20")
                    p_halting_checkbox = ui.checkbox("Halt on mismatch", value=False)

                    def add_policy():
                        p_type = p_type_select.value
                        p_expr = p_expr_input.value
                        p_target = p_target_input.value
                        p_priority = p_priority_input.value
                        p_halting = p_halting_checkbox.value

                        if not p_expr or not p_target:
                            ui.notify(
                                "Expression and target path are required.",
                                type="warning",
                            )
                            return
                        if p_priority is None:
                            ui.notify("Priority is required.", type="warning")
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

    dialog.open()
