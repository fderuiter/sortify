"""Unit and integration tests for AuditReportModal and post-sort verification workflows in Textual TUI."""

import asyncio
from unittest.mock import MagicMock

import pytest
from textual.widgets import Button, DataTable, Input, Select

from app.config import AppSettings
from app.core.audit_reporter import generate_audit_report
from app.ui.tui import AuditReportModal, AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def sample_audit_report(tmp_path):
    """Generate a sample audit report dict for modal testing."""
    records = [
        {
            "source_path": str(tmp_path / "doc1.pdf"),
            "destination_path": str(tmp_path / "Sorted" / "doc1.pdf"),
            "pre_hash": "aaaa111122223333",
            "post_hash": "aaaa111122223333",
        },
        {
            "source_path": str(tmp_path / "doc2.docx"),
            "destination_path": str(tmp_path / "Sorted" / "doc2.docx"),
            "pre_hash": "bbbb111122223333",
            "post_hash": "cccc111122223333",
        },
        {
            "source_path": str(tmp_path / "doc3.xlsx"),
            "destination_path": str(tmp_path / "doc3.xlsx"),
            "pre_hash": "dddd111122223333",
            "post_hash": "dddd111122223333",
            "skipped": True,
        },
    ]
    return generate_audit_report(str(tmp_path), records)


def test_audit_report_modal_mount_and_a11y(sample_audit_report):
    """Test AuditReportModal composition, rendering, and accessibility compliance audit."""

    async def _test():
        modal = AuditReportModal(sample_audit_report)
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            # Verify table and controls presence
            table = modal.query_one("#audit-table", DataTable)
            assert table is not None
            assert table.row_count == 3

            search_inp = modal.query_one("#audit-search-input", Input)
            assert search_inp is not None

            status_sel = modal.query_one("#audit-status-filter", Select)
            assert status_sel is not None

            # Verify WCAG accessibility audit compliance
            a11y_result = modal.audit_a11y_compliance()
            assert (
                a11y_result.get("violations") == [] or "violations" not in a11y_result
            )

            # Verify initial accessibility announcement
            last_ann = modal.get_last_announcement()
            assert last_ann is not None
            assert "Opened Audit Verification Report" in last_ann

    asyncio.run(_test())


def test_audit_report_modal_search_and_filter(sample_audit_report):
    """Test text search and status filtering updates DataTable rows."""

    async def _test():
        modal = AuditReportModal(sample_audit_report)
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            table = modal.query_one("#audit-table", DataTable)
            assert table.row_count == 3

            # Search by filename "doc1"
            search_inp = modal.query_one("#audit-search-input", Input)
            search_inp.value = "doc1"
            await pilot.pause()

            assert table.row_count == 1

            # Clear search input
            search_inp.value = ""
            await pilot.pause()
            assert table.row_count == 3

            # Filter by status MISMATCH_FAIL
            status_sel = modal.query_one("#audit-status-filter", Select)
            status_sel.value = "MISMATCH_FAIL"
            await pilot.pause()

            assert table.row_count == 1

    asyncio.run(_test())


def test_audit_report_modal_export_actions(sample_audit_report, tmp_path):
    """Test JSON and CSV export buttons in AuditReportModal."""

    async def _test():
        # Set base_dir in report to tmp_path
        sample_audit_report["base_dir"] = str(tmp_path)
        modal = AuditReportModal(sample_audit_report)
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            btn_json = modal.query_one("#btn-export-json", Button)
            await pilot.click(btn_json)
            await pilot.pause()

            json_export_file = tmp_path / "audit_report.json"
            assert json_export_file.exists()

            btn_csv = modal.query_one("#btn-export-csv", Button)
            await pilot.click(btn_csv)
            await pilot.pause()

            csv_export_file = tmp_path / "audit_report.csv"
            assert csv_export_file.exists()

    asyncio.run(_test())


def test_run_execute_worker_opens_audit_modal(tmp_path):
    """Test run_execute_worker opening AuditReportModal automatically upon move completion."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        records = [
            {
                "source_path": str(tmp_path / "a.txt"),
                "destination_path": str(tmp_path / "out" / "a.txt"),
                "pre_hash": "123",
                "post_hash": "123",
            }
        ]
        mock_summary = {
            "deleted_folders": 0,
            "protected_folders": 0,
            "cancelled": False,
            "audit_report": generate_audit_report(str(tmp_path), records),
        }

        app.app_session = MagicMock()
        app.app_session.execute_moves.return_value = mock_summary
        app.plan = {"out": {"a.txt": {"__type__": "file"}}}

        async with app.run_test() as pilot:
            worker = app.run_execute_worker()
            if worker:
                try:
                    await worker.wait()
                except Exception:
                    pass
            await pilot.pause()

            # Verify screen stack has pushed AuditReportModal
            assert isinstance(app.screen, AuditReportModal)

    asyncio.run(_test())
