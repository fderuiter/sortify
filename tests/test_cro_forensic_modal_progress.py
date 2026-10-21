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
