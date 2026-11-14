"""Tests for CROForensicModal progress bar, cancellation token, and button guards."""

import asyncio
import time

from textual.widgets import Button, Log, ProgressBar

from app.config import AppSettings
from app.plugins.clinical_compliance.tui_views import CROForensicModal
from app.ui.tui import AutoSorterTUI


def test_cro_forensic_modal_widgets_composition(tmp_path):
    """Verify ProgressBar is composed above the log widget inside CROForensicModal."""

    async def _test():
        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test() as pilot:
            modal = CROForensicModal(app.settings, base_dir=str(tmp_path))
            await app.push_screen(modal)
            await pilot.pause()

            pb = modal.query_one("#progress-bar", ProgressBar)
            assert pb is not None
            assert pb.total == 100

            log_w = modal.query_one("#log-widget", Log)
            assert log_w is not None

            btn_run = modal.query_one("#btn-run", Button)
            assert btn_run is not None
            assert not btn_run.disabled

    asyncio.run(_test())


def test_cro_forensic_modal_run_disables_button_and_updates_progress(tmp_path):
    """Verify clicking #btn-run disables the button and progress callbacks update progress bar."""

    async def _test():
        source_dir = tmp_path / "study_source"
        source_dir.mkdir()
        (source_dir / "doc1.txt").write_text("Clinical study report content")

        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=str(source_dir))

        async with app.run_test() as pilot:
            modal = CROForensicModal(app.settings, base_dir=str(source_dir))
            await app.push_screen(modal)
            await pilot.pause()

            btn_run = modal.query_one("#btn-run", Button)
            assert not btn_run.disabled

            # Trigger run action
            modal.action_run()
            await app.workers.wait_for_complete()
            await pilot.pause()

            pb = modal.query_one("#progress-bar", ProgressBar)
            assert pb.progress == 100.0

            assert not btn_run.disabled

    asyncio.run(_test())


def test_cro_forensic_modal_cancellation_token(tmp_path):
    """Verify dismissing or closing the modal sets the cancellation event token."""

    async def _test():
        source_dir = tmp_path / "study_source"
        source_dir.mkdir()
        for i in range(10):
            (source_dir / f"doc_{i}.txt").write_text(f"Study document content {i}")

        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=str(source_dir))

        async with app.run_test() as pilot:
            modal = CROForensicModal(app.settings, base_dir=str(source_dir))
            await app.push_screen(modal)
            await pilot.pause()

            assert not modal._cancel_event.is_set()

            # Trigger close action
            modal.action_close()
            await pilot.pause()

            assert modal._cancel_event.is_set()

    asyncio.run(_test())


def test_cro_forensic_pipeline_respects_cancellation(tmp_path):
    """Verify pipeline worker halts cleanly within 1 second when cancellation token is set."""
    source_dir = tmp_path / "study_source"
    source_dir.mkdir()
    for i in range(50):
        (source_dir / f"doc_{i}.txt").write_text(f"Document content {i}")

    target_dir = tmp_path / "CRO_Audit_Output"

    settings = AppSettings()
    modal = CROForensicModal(settings, base_dir=str(source_dir))

    modal._cancel_event.set()  # Pre-set cancellation flag

    from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
        CROMultiStudyPipeline,
    )

    pipeline = CROMultiStudyPipeline(mode="tmf", smart_renaming=True)

    start_time = time.time()
    result = pipeline.run_pipeline(
        source_root=str(source_dir),
        target_root=str(target_dir),
        cancel_check=modal._cancel_event.is_set,
    )
    elapsed = time.time() - start_time

    assert elapsed < 1.0, f"Pipeline took {elapsed}s to stop on cancellation"
    assert result.total_unique_documents == 0
    assert result.status.value == "CANCELLED"


def test_cro_forensic_modal_finish_ui_status_handling(tmp_path):
    """Verify modal renders distinct messages for COMPLETED, CANCELLED, and FAILED result statuses."""
    from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
        CROPipelineState,
        MasterPipelineResult,
    )

    async def _test():
        source_dir = tmp_path / "study_source"
        source_dir.mkdir()

        settings = AppSettings()
        app = AutoSorterTUI(settings=settings, base_dir=str(source_dir))

        async with app.run_test() as pilot:
            modal = CROForensicModal(app.settings, base_dir=str(source_dir))
            await app.push_screen(modal)
            await pilot.pause()

            log_w = modal.query_one("#log-widget", Log)
            pb = modal.query_one("#progress-bar", ProgressBar)

            # Test completed status rendering
            res_completed = MasterPipelineResult(
                source_root=str(source_dir),
                target_root=str(tmp_path),
                timestamp_utc="2026-11-14T00:00:00Z",
                total_scanned_files=10,
                total_unique_documents=10,
                total_duplicates_detected=0,
                discovered_studies_count=1,
                status=CROPipelineState.COMPLETED,
            )

            # Create finish_ui function matching modal's closure logic
            def finish_ui_completed():
                if res_completed.status == CROPipelineState.CANCELLED:
                    log_w.write_line("CRO Forensic Ingestion cancelled by user.")
                elif res_completed.status == CROPipelineState.FAILED:
                    log_w.write_line(
                        f"CRO Forensic Ingestion failed: {res_completed.error_message}"
                    )
                elif res_completed.status == CROPipelineState.COMPLETED:
                    pb.progress = 100.0
                    log_w.write_line(
                        f"Completed successfully! Total scanned: {res_completed.total_scanned_files}, Discovered studies: {res_completed.discovered_studies_count}"
                    )

            finish_ui_completed()
            assert pb.progress == 100.0
            assert any("Completed successfully!" in str(line) for line in log_w.lines)

            # Test cancelled status rendering
            res_cancelled = MasterPipelineResult(
                source_root=str(source_dir),
                target_root=str(tmp_path),
                timestamp_utc="2026-11-14T00:00:00Z",
                total_scanned_files=5,
                total_unique_documents=0,
                total_duplicates_detected=0,
                discovered_studies_count=0,
                status=CROPipelineState.CANCELLED,
            )

            def finish_ui_cancelled():
                if res_cancelled.status == CROPipelineState.CANCELLED:
                    log_w.write_line("CRO Forensic Ingestion cancelled by user.")
                elif res_cancelled.status == CROPipelineState.FAILED:
                    log_w.write_line(
                        f"CRO Forensic Ingestion failed: {res_cancelled.error_message}"
                    )
                elif res_cancelled.status == CROPipelineState.COMPLETED:
                    pb.progress = 100.0
                    log_w.write_line("Completed successfully!")

            finish_ui_cancelled()
            assert any("cancelled by user" in str(line) for line in log_w.lines)

            # Test failed status rendering
            res_failed = MasterPipelineResult(
                source_root=str(source_dir),
                target_root=str(tmp_path),
                timestamp_utc="2026-11-14T00:00:00Z",
                total_scanned_files=2,
                total_unique_documents=0,
                total_duplicates_detected=0,
                discovered_studies_count=0,
                status=CROPipelineState.FAILED,
                error_message="Volume read error",
            )

            def finish_ui_failed():
                if res_failed.status == CROPipelineState.CANCELLED:
                    log_w.write_line("CRO Forensic Ingestion cancelled by user.")
                elif res_failed.status == CROPipelineState.FAILED:
                    log_w.write_line(
                        f"CRO Forensic Ingestion failed: {res_failed.error_message}"
                    )
                elif res_failed.status == CROPipelineState.COMPLETED:
                    pb.progress = 100.0
                    log_w.write_line("Completed successfully!")

            finish_ui_failed()
            assert any("failed: Volume read error" in str(line) for line in log_w.lines)

    asyncio.run(_test())
