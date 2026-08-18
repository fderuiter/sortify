"""Standalone Component Catalog module.

Provides isolated rendering of user interface components independently from
production application routes, supporting local interactive visual inspection
and responsive viewport stress testing.
"""

import argparse
import sys
from typing import Any, Dict, List

from nicegui import ui

from app.ui.a11y_runner import (
    run_all_catalog_scans,
)
from app.ui.dialog_helper import get_dialog_card_classes

# --- COMPONENT CATALOG RENDERERS ---


def render_header_bar(container, state="default", viewport_width=1280):
    """Render application top navigation header bar in isolation."""
    with ui.row().classes(
        "w-full bg-slate-900 text-white px-4 py-3 items-center justify-between shadow-md flex-wrap gap-2"
    ):
        with ui.row().classes("items-center gap-3 flex-wrap"):
            ui.icon("folder_special", size="md", color="blue-4").props(
                'aria-label="Sortify Logo Icon"'
            )
            ui.label("Sortify AI Pro").classes(
                "text-xl font-bold tracking-tight truncate break-words"
            )
        with ui.row().classes("items-center gap-2 flex-wrap"):
            ui.button("CRO Forensic Ingest", icon="security").classes(
                "bg-blue-600 text-white"
            ).props('size="sm" unelevated aria-label="CRO Forensic Ingest Button"')
            ui.button("Settings", icon="tune").props(
                'flat text-color="white" size="sm" aria-label="Settings Button"'
            )
            ui.button("Help", icon="help_outline").props(
                'flat text-color="white" size="sm" aria-label="Help Button"'
            )


def render_directory_selection_card(container, state="default", viewport_width=1280):
    """Render directory selection and preset strategy card in isolation."""
    with ui.card().classes(
        "w-full max-w-5xl mx-auto p-5 bg-white rounded-xl shadow-sm border border-slate-200"
    ):
        with ui.row().classes("w-full items-center justify-between mb-3 flex-wrap gap-2"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.icon("folder", color="primary", size="sm").props(
                    'aria-label="Folder Icon"'
                )
                ui.label("Target Folder & Preset Strategy").classes(
                    "text-lg font-bold text-slate-800 break-words"
                )

        with ui.column().classes("w-full gap-4"):
            with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                path_val = "/tmp/test_documents" if state != "overflow" else "/tmp/very_long_path_that_stretches_across_the_entire_screen_width_for_testing_label_overflow_and_text_truncation_handling_in_narrow_viewports"
                ui.input(
                    label="Target Directory Path",
                    placeholder="Select root directory or file...",
                    value=path_val,
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense aria-label="Target Directory Input"'
                )
                ui.button("Browse", icon="folder_open").classes(
                    "bg-slate-800 text-white"
                ).props('unelevated size="md" aria-label="Browse Directory Button"')

            with ui.row().classes("w-full items-center justify-between flex-wrap gap-3"):
                ui.select(
                    options=[
                        "Standard AutoSorter",
                        "Clinical Trial TMF Binders",
                        "Legal Discovery Binders",
                        "Custom Rules",
                    ],
                    value="Standard AutoSorter",
                    label="Preset Strategy",
                ).classes("w-64 min-w-[180px]").props(
                    'outlined dense aria-label="Preset Strategy Select"'
                )

                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.button("Clean & Organize", icon="auto_fix_high").classes(
                        "bg-blue-600 text-white"
                    ).props('color="primary" unelevated size="md" aria-label="Start Organization Button"')
                    ui.button("Cancel", icon="cancel").props(
                        'flat color="negative" size="md" aria-label="Cancel Organization Button"'
                    )
                    ui.button("Re-analyze", icon="refresh").props(
                        'outline color="primary" size="md" aria-label="Re-analyze Directory Button"'
                    )


def render_plan_treeview_card(container, state="default", viewport_width=1280):
    """Render file sorting plan treeview preview component in isolation."""
    with ui.card().classes(
        "w-full max-w-5xl mx-auto p-5 bg-white rounded-xl shadow-sm border border-slate-200"
    ):
        with ui.row().classes("w-full justify-between items-center mb-4 flex-wrap gap-2"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.icon("account_tree", color="primary", size="sm").props(
                    'aria-label="Treeview Icon"'
                )
                ui.label("Proposed File Reorganization Plan").classes(
                    "text-lg font-bold text-slate-800 break-words"
                )

            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.button("Apply Organization Plan", icon="check_circle").classes(
                    "bg-green-600 text-white"
                ).props('unelevated size="sm" aria-label="Apply Organization Plan Button"')

        if state == "error":
            with ui.card().classes(
                "w-full bg-red-50 border border-red-200 p-3 mb-3 rounded-lg"
            ):
                with ui.row().classes("items-center gap-2 text-red-800 flex-wrap"):
                    ui.icon("warning", size="sm").props('aria-label="Warning Icon"')
                    ui.label(
                        "2 locked files detected in target directory. Re-analysis recommended."
                    ).classes("text-sm font-semibold break-words")

        with ui.column().classes("w-full border rounded-lg p-3 bg-slate-50 gap-2"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.icon("folder", color="amber").props('aria-label="Folder Icon"')
                ui.label("Financial_Reports_2026/").classes(
                    "font-bold text-slate-900 break-words"
                )
            with ui.column().classes("pl-6 gap-1 w-full"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.icon("description", color="blue-6").props(
                        'aria-label="Document Icon"'
                    )
                    ui.label("Q1_Audit_Summary.pdf -> Q1_Audit_Final.pdf").classes(
                        "text-sm text-slate-700 truncate break-words max-w-full"
                    )


def render_settings_modal_card(container, state="default", viewport_width=1280):
    """Render settings dialog view in isolation."""
    with ui.card().classes(
        get_dialog_card_classes("xl", "w-full p-6 bg-white rounded-xl shadow-lg border border-slate-200")
    ):
        with ui.row().classes("w-full justify-between items-center mb-4 flex-wrap gap-2"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.icon("settings", color="primary", size="md").props(
                    'aria-label="Settings Header Icon"'
                )
                ui.label("Application Settings").classes(
                    "text-xl font-bold text-slate-900 break-words"
                )
            ui.button("Close", icon="close").props(
                'flat round dense aria-label="Close Settings Dialog Button"'
            )

        with ui.column().classes("w-full gap-4"):
            with ui.row().classes("w-full border-b pb-2 flex-wrap gap-2"):
                ui.button("General", icon="tune").props(
                    'flat text-color="primary" aria-label="General Settings Tab"'
                )
                ui.button("Keyword Rules", icon="label").props(
                    'flat text-color="grey-7" aria-label="Keyword Rules Tab"'
                )
                ui.button("Policies", icon="gavel").props(
                    'flat text-color="grey-7" aria-label="Policies Tab"'
                )
                ui.button("Security & AI Consent", icon="shield").props(
                    'flat text-color="grey-7" aria-label="Security Tab"'
                )

            with ui.column().classes("w-full p-2 gap-3"):
                ui.switch(
                    "Enable Contextual Smart Renaming", value=True
                ).props('aria-label="Contextual Smart Renaming Switch"')
                ui.switch(
                    "Preserve Original Folder Hierarchy", value=False
                ).props('aria-label="Preserve Hierarchy Switch"')

            with ui.row().classes("w-full justify-end gap-2 mt-4 flex-wrap"):
                ui.button("Save Settings", icon="save").classes(
                    "bg-blue-600 text-white"
                ).props('unelevated aria-label="Save Settings Button"')


def render_setup_wizard_card(container, state="default", viewport_width=1280):
    """Render initial AI setup wizard modal in isolation."""
    with ui.card().classes(
        get_dialog_card_classes("md", "w-full p-6 bg-white rounded-xl shadow-lg border border-slate-200")
    ):
        ui.label("AI Features & Model Initialization").classes(
            "text-xl font-bold text-slate-900 mb-3 break-words"
        ).props('aria-label="Setup Wizard Title"')

        ui.label(
            "To enable local keyword clustering and automated taxonomy mapping, Smart AutoSorter initializes the local offline engine."
        ).classes("text-sm text-slate-600 mb-4 break-words").props(
            'aria-label="Wizard Description Text"'
        )

        if state == "loading":
            ui.linear_progress(value=0.45).classes("w-full mb-2").props(
                'aria-label="Model Download Progress Bar"'
            )
            ui.label("Downloading keyword classification models... (45%)").classes(
                "text-xs text-slate-500 mb-4 truncate break-words"
            ).props('aria-label="Download Progress Label"')

        with ui.row().classes("w-full justify-end gap-2 mt-4 flex-wrap"):
            ui.button("Accept & Initialize", icon="check").classes(
                "bg-blue-600 text-white"
            ).props('unelevated aria-label="Accept Wizard Setup Button"')
            ui.button("Decline", icon="close").props(
                'flat color="grey-7" aria-label="Decline Wizard Setup Button"'
            )


def render_cro_forensic_card(container, state="default", viewport_width=1280):
    """Render CRO Multi-Study Forensic drive scanning dialog card in isolation."""
    with ui.card().classes(
        get_dialog_card_classes("xl", "w-full p-6 bg-white rounded-xl shadow-lg border border-slate-200")
    ):
        with ui.row().classes("w-full justify-between items-center border-b pb-3 flex-wrap gap-2"):
            with ui.column().classes("gap-0"):
                ui.label("CRO Forensic Multi-Study Ingestion & Audit").classes(
                    "text-lg font-bold text-slate-900 break-words"
                )
                ui.label(
                    "Scan raw storage drives, disambiguate multiple clinical trial protocols, and compile TMF binders."
                ).classes("text-xs text-gray-500 break-words")
            ui.button(icon="close").props(
                'flat round dense aria-label="Close CRO Forensic Dialog Button"'
            )

        with ui.column().classes("w-full gap-4 mt-4"):
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.input(
                    label="Source Storage Drive / Archive Root",
                    placeholder="Select drive or folder to scan...",
                    value="/volumes/forensic_drive_01",
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense aria-label="Source Storage Drive Input"'
                )
                ui.button("Browse Source", icon="folder_open").props(
                    'color="primary" outline aria-label="Browse Source Button"'
                )

            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.input(
                    label="Target Clean Binders Destination",
                    placeholder="Select target output directory...",
                    value="/output/tmf_binders",
                ).classes("flex-grow min-w-[200px]").props(
                    'outlined dense aria-label="Target Binders Output Input"'
                )
                ui.button("Browse Target", icon="create_new_folder").props(
                    'color="secondary" outline aria-label="Browse Target Button"'
                )

            with ui.row().classes("w-full justify-end gap-2 mt-2 flex-wrap"):
                ui.button("Execute Multi-Study Ingest", icon="play_arrow").classes(
                    "bg-blue-600 text-white"
                ).props('unelevated aria-label="Start CRO Ingest Button"')


def render_help_modal_card(container, state="default", viewport_width=1280):
    """Render user documentation guide modal in isolation."""
    with ui.card().classes(
        get_dialog_card_classes("xl", "w-full p-6 bg-white rounded-xl shadow-lg border border-slate-200")
    ):
        with ui.row().classes("w-full justify-between items-center mb-4 flex-wrap gap-2"):
            ui.label("User Guide & Documentation").classes(
                "text-2xl font-bold text-slate-900 break-words"
            ).props('aria-label="Help Dialog Title"')
            ui.button("Close", icon="close").classes(
                "bg-gray-200 text-black shrink-0"
            ).props('aria-label="Close Help Dialog Button"')

        with ui.scroll_area().classes("w-full h-48 border rounded p-4 overflow-y-auto"):
            ui.markdown(
                "# Smart AutoSorter Guide\n\n- Select target directory\n- Choose classification preset\n- Review reorganization plan before applying."
            ).classes("w-full break-words")


def render_status_progress_panel(container, state="default", viewport_width=1280):
    """Render live sorting progress panel in isolation."""
    with ui.card().classes(
        "w-full max-w-5xl mx-auto p-5 bg-white rounded-xl shadow-sm border border-slate-200"
    ):
        with ui.row().classes("w-full justify-between items-center mb-2 flex-wrap gap-2"):
            ui.label("Processing Operations in Progress").classes(
                "text-md font-bold text-slate-800 break-words"
            )
            ui.label("42 / 100 Files Processed (42%)").classes(
                "text-sm font-semibold text-blue-600 truncate break-words"
            )

        ui.linear_progress(value=0.42).classes("w-full mb-3").props(
            'aria-label="Processing Progress Bar"'
        )

        with ui.row().classes("w-full justify-between items-center flex-wrap gap-2"):
            ui.label("Current file: /documents/clinical/study_01/consent.pdf").classes(
                "text-xs text-slate-500 truncate break-words max-w-md"
            )
            ui.button("Cancel Operation", icon="stop").props(
                'flat color="negative" size="sm" aria-label="Cancel Operation Button"'
            )


# --- CATALOG REGISTRY DEFINITION ---

CATALOG_REGISTRY: List[Dict[str, Any]] = [
    {
        "id": "header_bar",
        "name": "Application Header Bar",
        "description": "Top bar navigation containing title, logo, and action buttons.",
        "render_func": render_header_bar,
        "sample_states": ["default"],
    },
    {
        "id": "directory_selection",
        "name": "Directory Selection & Presets Card",
        "description": "Path picker, strategy dropdown selector, and primary process buttons.",
        "render_func": render_directory_selection_card,
        "sample_states": ["default", "overflow"],
    },
    {
        "id": "plan_treeview",
        "name": "Proposed Organization Tree View",
        "description": "Treeview hierarchy showing original vs proposed target paths.",
        "render_func": render_plan_treeview_card,
        "sample_states": ["default", "error"],
    },
    {
        "id": "settings_modal",
        "name": "Application Settings View",
        "description": "Settings modal card with tabbed controls for rules, policies, and AI options.",
        "render_func": render_settings_modal_card,
        "sample_states": ["default"],
    },
    {
        "id": "setup_wizard",
        "name": "AI Model Setup Wizard",
        "description": "Modal wizard for initializing offline keyword and taxonomy models.",
        "render_func": render_setup_wizard_card,
        "sample_states": ["default", "loading"],
    },
    {
        "id": "cro_forensic_dialog",
        "name": "CRO Forensic Multi-Study View",
        "description": "Drive ingestion modal for clinical trial protocol disambiguation.",
        "render_func": render_cro_forensic_card,
        "sample_states": ["default"],
    },
    {
        "id": "help_modal",
        "name": "User Guide & Help View",
        "description": "Documentation viewer card with markdown text.",
        "render_func": render_help_modal_card,
        "sample_states": ["default"],
    },
    {
        "id": "status_progress_panel",
        "name": "Status & Progress Panel",
        "description": "Real-time file organization progress bar, ETA, and cancellation controls.",
        "render_func": render_status_progress_panel,
        "sample_states": ["default"],
    },
]


# --- INTERACTIVE WORKBENCH GUI ---


def build_catalog_ui():
    """Build interactive local catalog workbench GUI."""
    ui.add_head_html("""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
body { font-family: 'Inter', sans-serif; background-color: #f8fafc; color: #0f172a; }
.preview-viewport-frame { transition: all 0.2s ease-in-out; border: 2px dashed #94a3b8; background: #ffffff; }
</style>
""")

    selected_comp_id = CATALOG_REGISTRY[0]["id"]
    selected_viewport_width = 1280
    selected_state = "default"

    comp_map = {c["id"]: c for c in CATALOG_REGISTRY}

    with ui.header().classes("bg-slate-900 text-white px-6 py-3 items-center justify-between shadow-md"):
        with ui.row().classes("items-center gap-3"):
            ui.icon("view_in_ar", size="md", color="blue-4")
            ui.label("Component Catalog & A11y Workbench").classes("text-xl font-bold tracking-tight")

    with ui.column().classes("w-full max-w-7xl mx-auto p-6 gap-6"):
        # Control Bar
        with ui.card().classes("w-full p-4 bg-white rounded-xl shadow-sm border border-slate-200"):
            with ui.row().classes("w-full items-center justify-between flex-wrap gap-4"):
                ui.select(
                    options={c["id"]: c["name"] for c in CATALOG_REGISTRY},
                    value=selected_comp_id,
                    label="Select UI Component",
                ).classes("w-72").props('outlined dense aria-label="Component Selector"')

                ui.select(
                    options={
                        1280: "Desktop (1280px)",
                        768: "Tablet (768px)",
                        375: "Mobile (375px)",
                        320: "Narrow Mobile (320px)",
                    },
                    value=selected_viewport_width,
                    label="Viewport Size",
                ).classes("w-56").props('outlined dense aria-label="Viewport Selector"')

                ui.select(
                    options=["default", "overflow", "loading", "error"],
                    value=selected_state,
                    label="State Variant",
                ).classes("w-40").props('outlined dense aria-label="State Selector"')

        # Preview Container
        with ui.column().classes("w-full items-center justify-center p-4 bg-slate-100 rounded-xl min-h-[400px]"):
            preview_container = ui.element("div").classes(
                "preview-viewport-frame w-full rounded-xl p-4 shadow-sm"
            )
            preview_container.style(f"max-width: {selected_viewport_width}px;")

            with preview_container:
                comp_entry = comp_map.get(selected_comp_id, CATALOG_REGISTRY[0])
                comp_entry["render_func"](
                    preview_container,
                    state=selected_state,
                    viewport_width=selected_viewport_width,
                )


def main():
    """CLI launcher for standalone component catalog entry point."""
    parser = argparse.ArgumentParser(
        description="Standalone Component Catalog and Accessibility Gate Launcher"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to serve interactive catalog GUI on (default: 8080)",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help="Execute headless accessibility scans against all components and exit",
    )
    args = parser.parse_args()

    if args.audit_only:
        total_scans, violations = run_all_catalog_scans(CATALOG_REGISTRY)
        if violations:
            print(f"FAILED: Found {len(violations)} accessibility violations across {total_scans} scans.", file=sys.stderr)
            for v in violations:
                print(f"  [{v.rule_id}] Component '{v.component_id}' ({v.viewport_name}): {v.message} @ {v.locator}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"SUCCESS: All {total_scans} catalog component-viewport accessibility scans passed.")
            sys.exit(0)

    @ui.page("/")
    def catalog_page():
        build_catalog_ui()

    ui.run(port=args.port, title="Component Catalog Workbench", show=False)


if __name__ == "__main__":
    main()
