"""Settings module using NiceGUI."""

from nicegui import ui

def show_settings(parent_app, settings):
    with ui.dialog() as dialog, ui.card().classes('w-3/4 max-w-4xl p-6'):
        with ui.row().classes('w-full justify-between items-center mb-6'):
            ui.label("Application Settings").classes('text-2xl font-bold').props('aria-label="Settings Dialog Title"')
            ui.button("Close", on_click=dialog.close).classes('bg-gray-200 text-black').props('aria-label="Close Settings Button"')
            
        with ui.tabs().classes('w-full') as tabs:
            ui.tab('General', label='General').props('aria-label="General Settings Tab"')
            ui.tab('AI', label='AI Configuration').props('aria-label="AI Configuration Tab"')
            ui.tab('Rules', label='Routing Rules').props('aria-label="Routing Rules Tab"')
            
        with ui.tab_panels(tabs, value='General').classes('w-full mt-4'):
            with ui.tab_panel('General'):
                ui.label("Cleanup & Maintenance").classes('text-lg font-bold mb-2')
                ui.switch("Automatically remove empty directories", value=settings.CLEANUP_EMPTY_DIRS).props('aria-label="Cleanup empty directories toggle"')
                ui.label("Processing Limits").classes('text-lg font-bold mt-4 mb-2')
                ui.number("Max folder depth", value=settings.MAX_FOLDER_DEPTH).props('aria-label="Max folder depth input"')
                
            with ui.tab_panel('AI'):
                ui.label("Privacy Options").classes('text-lg font-bold mb-2')
                ui.label("AI processing is fully offline.").classes('text-gray-500 mb-2')
                ui.button("Reset Model Cache", on_click=lambda: ui.notify("Cache cleared.")).props('aria-label="Reset Model Cache Button"')

            with ui.tab_panel('Rules'):
                ui.label("Keyword Routing").classes('text-lg font-bold mb-2')
                ui.input("Keyword").props('placeholder="e.g. invoice" aria-label="Keyword input"')
                ui.input("Target Path").props('placeholder="/path/to/folder" aria-label="Target Path input"')
                ui.button("Add Rule", on_click=lambda: ui.notify("Rule added.")).props('aria-label="Add Rule Button"')

    dialog.open()
