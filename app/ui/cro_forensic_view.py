"""CRO Forensic Multi-Study Ingest View and Dialog for NiceGUI."""

import asyncio
import os
import webbrowser

from nicegui import ui

from app.core.cro_multi_study_pipeline import (
    CROMultiStudyPipeline,
    MasterPipelineResult,
)
from app.ui.dialog_helper import ask_directory_async, get_dialog_card_classes


class CROForensicView:
    """Provides a dedicated graphical interface for CRO multi-study forensic drive scanning."""

    def __init__(self, settings):
        self.settings = settings
        self.source_dir = ""
        self.target_dir = ""
        self.mode = "tmf"
        self.smart_renaming = True
        self._is_running = False
        self._cancel_flag = False

    def show_dialog(self):
        """Open the CRO Forensic Ingestion modal dialog."""
        with (
            ui.dialog() as dialog,
            ui.card().classes(
                get_dialog_card_classes("xl")
                + " p-6 max-h-[90vh] w-full max-w-4xl overflow-y-auto"
            ),
        ):
            dialog.props('aria-label="CRO Forensic Ingest Dialog"')

            with ui.row().classes("w-full justify-between items-center border-b pb-3"):
                with ui.column().classes("gap-0"):
                    ui.label("CRO Forensic Multi-Study Ingestion & Audit").classes(
                        "text-h6 font-bold text-slate-900"
                    )
                    ui.label(
                        "Scan raw drives/archives, disambiguate multiple clinical trial protocols, and build verified TMF binders."
                    ).classes("text-xs text-gray-500")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")

            # Source & Target Paths
            with ui.column().classes("w-full gap-3 mt-4"):
                # Source path
                with ui.row().classes("w-full items-center gap-2"):
                    self.source_input = (
                        ui.input(
                            label="Source Storage Drive / Archive Root (Read-Only)",
                            placeholder="Select drive or folder to scan...",
                            value=self.source_dir,
                        )
                        .classes("flex-grow")
                        .props('outlined dense aria-label="Source Drive Input"')
                    )

                    async def pick_source():
                        path = await ask_directory_async(
                            title="Select Source Storage Drive"
                        )
                        if path:
                            self.source_dir = path
                            self.source_input.set_value(path)

                    ui.button(
                        "Browse Source", on_click=pick_source, icon="folder_open"
                    ).props('color="primary" outline aria-label="Browse Source Button"')

                # Target path
                with ui.row().classes("w-full items-center gap-2"):
                    self.target_input = (
                        ui.input(
                            label="Target Clean Binders Destination",
                            placeholder="Select target output directory...",
                            value=self.target_dir,
                        )
                        .classes("flex-grow")
                        .props('outlined dense aria-label="Target Directory Input"')
                    )

                    async def pick_target():
                        path = await ask_directory_async(
                            title="Select Target Binders Directory"
                        )
                        if path:
                            self.target_dir = path
                            self.target_input.set_value(path)

                    ui.button(
                        "Browse Target", on_click=pick_target, icon="create_new_folder"
                    ).props(
                        'color="secondary" outline aria-label="Browse Target Button"'
                    )

                # Options row
                with ui.row().classes(
                    "w-full items-center gap-6 mt-2 bg-gray-50 p-3 rounded border"
                ):
                    ui.select(
                        {
                            "tmf": "Sponsor TMF (Zone/Section)",
                            "isf": "Site ISF Regulatory Binder",
                        },
                        value=self.mode,
                        label="Hierarchy Standard",
                        on_change=lambda e: setattr(self, "mode", e.value),
                    ).classes("w-full max-w-xs").props(
                        'outlined dense aria-label="Hierarchy Standard Selector"'
                    )

                    ui.switch(
                        "Smart Clinical Renaming",
                        value=self.smart_renaming,
                        on_change=lambda e: setattr(self, "smart_renaming", e.value),
                    ).props('aria-label="Smart Renaming Switch"')

            # Progress & Control Row
            with ui.column().classes("w-full items-center mt-4"):
                self.progress_bar = (
                    ui.linear_progress(value=0)
                    .classes("w-full")
                    .props('aria-label="Forensic Scan Progress"')
                )
                self.progress_bar.set_visibility(False)

                self.status_label = ui.label("").classes("text-sm text-gray-600 mt-1")
                self.status_label.set_visibility(False)

                with ui.row().classes("gap-3 mt-3"):
                    self.start_btn = (
                        ui.button(
                            "Start Forensic Ingest",
                            icon="search_insights",
                            on_click=self._execute_pipeline_task,
                        )
                        .classes("bg-blue-600 text-white font-bold")
                        .props('aria-label="Start Ingest Button"')
                    )

                    self.cancel_btn = (
                        ui.button(
                            "Cancel",
                            on_click=self._cancel_pipeline,
                        )
                        .classes("bg-red-500 text-white")
                        .props('aria-label="Cancel Ingest Button"')
                    )
                    self.cancel_btn.set_visibility(False)

            # Results Container
            self.results_container = ui.column().classes("w-full mt-4 gap-4")

        dialog.open()

    async def _execute_pipeline_task(self):
        """Execute the forensic multi-study pipeline asynchronously."""
        source = self.source_input.value.strip()
        target = self.target_input.value.strip()

        if not source or not os.path.exists(source):
            ui.notify(
                "Please select a valid source directory or drive.", type="warning"
            )
            return
        if not target:
            ui.notify("Please select a target destination directory.", type="warning")
            return

        self._is_running = True
        self._cancel_flag = False
        self.start_btn.disable()
        self.cancel_btn.set_visibility(True)
        self.progress_bar.set_visibility(True)
        self.status_label.set_visibility(True)
        self.results_container.clear()

        def update_progress(pct: int, msg: str):
            self.progress_bar.set_value(pct / 100.0)
            self.status_label.set_text(f"{pct}% - {msg}")

        pipeline = CROMultiStudyPipeline(
            mode=self.mode,
            smart_renaming=self.smart_renaming,
        )

        try:
            result: MasterPipelineResult = await asyncio.to_thread(
                pipeline.run_pipeline,
                source_root=source,
                target_root=target,
                progress_callback=update_progress,
                cancel_check=lambda: self._cancel_flag,
            )
            self._render_results(result)
            ui.notify("CRO Forensic Ingestion complete!", type="positive")
        except Exception as e:
            ui.notify(f"Error during ingestion: {e}", type="negative")
            self.status_label.set_text(f"Error: {e}")
        finally:
            self._is_running = False
            self.start_btn.enable()
            self.cancel_btn.set_visibility(False)

    def _cancel_pipeline(self):
        self._cancel_flag = True
        self.status_label.set_text("Cancelling scan...")

    def _render_results(self, result: MasterPipelineResult):
        """Display discovered studies, compliance metrics, and manifest export."""
        with self.results_container:
            # Summary Metrics Grid
            with ui.row().classes("w-full grid grid-cols-4 gap-3"):
                with ui.card().classes(
                    "p-3 items-center text-center bg-slate-50 border"
                ):
                    ui.label(str(result.total_scanned_files)).classes(
                        "text-2xl font-bold text-slate-800"
                    )
                    ui.label("Files Ingested").classes(
                        "text-xs text-gray-500 uppercase font-semibold"
                    )

                with ui.card().classes(
                    "p-3 items-center text-center bg-slate-50 border"
                ):
                    ui.label(str(result.total_unique_documents)).classes(
                        "text-2xl font-bold text-slate-800"
                    )
                    ui.label("Unique Docs").classes(
                        "text-xs text-gray-500 uppercase font-semibold"
                    )

                with ui.card().classes(
                    "p-3 items-center text-center bg-slate-50 border"
                ):
                    ui.label(str(result.total_duplicates_detected)).classes(
                        "text-2xl font-bold text-amber-600"
                    )
                    ui.label("Duplicates Deduplicated").classes(
                        "text-xs text-gray-500 uppercase font-semibold"
                    )

                with ui.card().classes(
                    "p-3 items-center text-center bg-slate-50 border"
                ):
                    ui.label(str(result.discovered_studies_count)).classes(
                        "text-2xl font-bold text-blue-600"
                    )
                    ui.label("Studies Discovered").classes(
                        "text-xs text-gray-500 uppercase font-semibold"
                    )

            # Discovered Studies Section
            ui.label("Discovered Study Binders & Regulatory Readiness").classes(
                "text-md font-bold text-slate-800 mt-2"
            )

            with ui.column().classes("w-full gap-3"):
                for study in result.studies_summary:
                    badge_color = (
                        "positive"
                        if study.compliance_score_percent >= 90
                        else "warning"
                        if study.compliance_score_percent >= 60
                        else "negative"
                    )
                    with ui.card().classes("w-full p-4 border rounded-lg"):
                        with ui.row().classes("w-full justify-between items-center"):
                            with ui.column().classes("gap-0"):
                                ui.label(study.study_id).classes(
                                    "text-lg font-bold text-blue-900"
                                )
                                ui.label(
                                    f"{study.total_documents} documents organized into regulatory binder"
                                ).classes("text-xs text-gray-500")

                            with ui.row().classes("items-center gap-3"):
                                ui.badge(
                                    f"{study.audit_readiness_status} ({study.compliance_score_percent}%)",
                                    color=badge_color,
                                ).classes("text-sm p-2")

                                def open_html(path=study.audit_report_html_path):
                                    if os.path.exists(path):
                                        webbrowser.open(f"file://{path}")

                                ui.button(
                                    "View Audit Dossier",
                                    on_click=open_html,
                                    icon="description",
                                ).props('size="sm" outline')

            # Actions Row
            with ui.row().classes(
                "w-full justify-end items-center gap-3 mt-4 border-t pt-3"
            ):
                if result.chain_of_custody_manifest_path and os.path.exists(
                    result.chain_of_custody_manifest_path
                ):

                    def open_manifest():
                        webbrowser.open(
                            f"file://{result.chain_of_custody_manifest_path}"
                        )

                    ui.button(
                        "Open Chain of Custody Manifest",
                        on_click=open_manifest,
                        icon="receipt_long",
                    ).props('color="primary"')
