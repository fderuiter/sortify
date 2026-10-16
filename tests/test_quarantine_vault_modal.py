"""Unit and integration tests for QuarantineVaultModal and quarantine review workflows in Textual TUI."""

import asyncio
import os

import pytest
from textual.widgets import Button, DataTable, Input, Select, Static

from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.ui.tui import AutoSorterTUI, QuarantineVaultModal

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def temp_db(tmp_path):
    """Fixture creating a temporary database for quarantine tests."""
    db_path = str(tmp_path / "test_quarantine.db")
    db = Database(db_path=db_path, worker=DBWorker())
    return db


@pytest.fixture
def staged_quarantine_item(tmp_path, temp_db):
    """Fixture staging a sample file in quarantine for testing."""
    base_dir = str(tmp_path / "base")
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    src_file = src_dir / "confidential_record.pdf"
    src_file.write_text("CONFIDENTIAL CLINICAL STUDY DATA - SUBJECT 101")

    service = QuarantineInterceptorService(db=temp_db)
    staged = service.stage_incoming_file(
        source_path=str(src_file),
        base_dir=base_dir,
        original_relative_path="medical/confidential_record.pdf",
        policy_action="QUARANTINE_STAGING",
    )
    return staged, temp_db, base_dir


def test_quarantine_vault_modal_mount_and_a11y(temp_db, tmp_path):
    """Test QuarantineVaultModal composition, layout, controls, and accessibility compliance audit."""

    async def _test():
        modal = QuarantineVaultModal(db=temp_db, base_dir="")
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            # Verify presence of main UI controls
            table = modal.query_one("#quarantine-table", DataTable)
            assert table is not None

            search_inp = modal.query_one("#quarantine-search-input", Input)
            assert search_inp is not None

            status_sel = modal.query_one("#quarantine-status-filter", Select)
            assert status_sel is not None

            btn_rel = modal.query_one("#btn-quarantine-release", Button)
            assert btn_rel is not None

            btn_dis = modal.query_one("#btn-quarantine-dismiss", Button)
            assert btn_dis is not None

            btn_insp = modal.query_one("#btn-quarantine-reinspect", Button)
            assert btn_insp is not None

            # Verify WCAG accessibility audit compliance
            a11y_result = modal.audit_a11y_compliance()
            assert (
                a11y_result.get("violations") == [] or "violations" not in a11y_result
            )

            # Verify initial accessibility announcement
            last_ann = modal.get_last_announcement()
            assert last_ann is not None

    asyncio.run(_test())


def test_quarantine_vault_modal_records_loading_and_filtering(staged_quarantine_item):
    """Test loading staged items from SQLite, filtering by status/text, and viewing audit logs."""
    staged, db, base_dir = staged_quarantine_item
    job_id = staged["job_id"]

    async def _test():
        modal = QuarantineVaultModal(db=db, base_dir="")
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause(0.5)

            table = modal.query_one("#quarantine-table", DataTable)
            assert table.row_count >= 1

            # Detail pane should render audit log timeline
            detail_content = modal.query_one("#quarantine-detail-content", Static)
            content_text = str(getattr(detail_content, "content", detail_content))
            assert job_id in content_text
            assert "Structured Audit Log Timeline" in content_text

            # Test text search filter
            search_inp = modal.query_one("#quarantine-search-input", Input)
            search_inp.value = "nonexistent_term_999"
            await pilot.pause(0.1)
            assert table.row_count == 0

            search_inp.value = "confidential"
            await pilot.pause(0.1)
            assert table.row_count == 1

            # Test status select filter
            status_sel = modal.query_one("#quarantine-status-filter", Select)
            status_sel.value = "RELEASED"
            await pilot.pause(0.1)
            assert table.row_count == 0

            status_sel.value = "STAGED"
            await pilot.pause(0.1)
            assert table.row_count == 1

    asyncio.run(_test())


def test_quarantine_vault_modal_release_action(staged_quarantine_item, tmp_path):
    """Test releasing a quarantined item atomically and checking DB status updated to RELEASED."""
    staged, db, base_dir = staged_quarantine_item
    job_id = staged["job_id"]
    staged_path = staged["staged_filepath"]
    assert os.path.exists(staged_path)

    dest_dir = tmp_path / "released_target"
    dest_file = dest_dir / "confidential_record.pdf"

    async def _test():
        modal = QuarantineVaultModal(db=db, base_dir="")
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause(0.3)

            modal.execute_release_async(staged, str(dest_file))
            await pilot.pause(0.5)

            rec = db.get_quarantine_record(job_id)
            assert rec is not None
            assert rec["status"] == "RELEASED"
            assert os.path.exists(dest_file)
            assert not os.path.exists(staged_path)
            assert "Released from quarantine modal" in str(rec["audit_log"])

    asyncio.run(_test())


def test_quarantine_vault_modal_dismiss_action(staged_quarantine_item):
    """Test dismissing a quarantine item and verifying status DISMISSED without moving file."""
    staged, db, base_dir = staged_quarantine_item
    job_id = staged["job_id"]
    staged_path = staged["staged_filepath"]
    assert os.path.exists(staged_path)

    async def _test():
        modal = QuarantineVaultModal(db=db, base_dir="")
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause(0.3)

            modal.execute_dismiss_async(job_id)
            await pilot.pause(0.5)

            rec = db.get_quarantine_record(job_id)
            assert rec is not None
            assert rec["status"] == "DISMISSED"
            assert os.path.exists(staged_path)
            assert "Dismissed manually via quarantine vault modal review" in str(
                rec["audit_log"]
            )

    asyncio.run(_test())


def test_quarantine_vault_modal_reinspect_action(staged_quarantine_item):
    """Test triggering forensic re-inspection scan on a quarantined item."""
    staged, db, base_dir = staged_quarantine_item
    job_id = staged["job_id"]

    async def _test():
        modal = QuarantineVaultModal(db=db, base_dir="")
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause(0.3)

            modal.execute_reinspect_async(job_id)
            await pilot.pause(0.5)

            rec = db.get_quarantine_record(job_id)
            assert rec is not None
            assert rec["status"] in (
                "QUARANTINED",
                "CLEARED",
                "IN_INSPECTION",
                "RELEASED",
                "MANUAL_REVIEW_REQUIRED",
            )

    asyncio.run(_test())


def test_auto_sorter_tui_ctrl_u_binding():
    """Test pressing ctrl+u in AutoSorterTUI opens QuarantineVaultModal screen."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("ctrl+u")
            await pilot.pause(0.3)

            assert isinstance(app.screen, QuarantineVaultModal)

    asyncio.run(_test())
