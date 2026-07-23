"""Settings module using NiceGUI."""

from nicegui import ui


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

    with ui.dialog() as dialog, ui.card().classes("w-3/4 max-w-4xl p-6"):
        with ui.row().classes("w-full justify-between items-center mb-6"):
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

            with ui.tab_panel("AI"):
                ui.label("Privacy Options").classes("text-lg font-bold mb-2")
                ui.label("AI processing is fully offline.").classes(
                    "text-gray-500 mb-2"
                )

                def reset_model_cache():
                    import shutil

                    from nicegui import run

                    from app.config import get_app_dir

                    ui.notify("Clearing model cache in background...")

                    async def do_reset():
                        await run.io_bound(shutil.rmtree, model_dir, ignore_errors=True)
                        ui.notify("Model cache cleared successfully.", type="positive")

                    import asyncio

                    asyncio.create_task(do_reset())

                ui.button("Reset Model Cache", on_click=reset_model_cache).props(
                    'aria-label="Reset Model Cache Button"'
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
                with ui.row().classes("w-full items-center gap-4"):
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

    dialog.open()
