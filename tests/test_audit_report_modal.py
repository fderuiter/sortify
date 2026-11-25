"""Unit and integration tests for AuditReportModal and post-sort verification workflows in Textual TUI."""

import asyncio
from unittest.mock import MagicMock

import pytest
from textual.widgets import Button, DataTable, Input, Select

from app.config import AppSettings
from app.core.audit_reporter import generate_audit_report
from app.ui.tui import AuditReportModal, AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


async def wait_for_rows(pilot, table: DataTable, expected: int) -> None:
    """Pause until the filter handlers repopulate the table.

    The Changed message hops from the widget to the modal, so one pause
    is not always enough on a slow runner.
    """
    for _ in range(40):
        if table.row_count == expected:
            return
        await pilot.pause(0.05)
    assert table.row_count == expected


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
        {
            "source_path": str(tmp_path / "doc4.txt"),
            "destination_path": str(tmp_path / "Sorted" / "doc4.txt"),
            "pre_hash": None,
            "post_hash": "eeee111122223333",
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
            assert table.row_count == 4

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
            assert "1 unverified" in last_ann

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
            assert table.row_count == 4

            # Search by filename "doc1"
            search_inp = modal.query_one("#audit-search-input", Input)
            search_inp.value = "doc1"
            await wait_for_rows(pilot, table, 1)

            # Clear search input
            search_inp.value = ""
            await wait_for_rows(pilot, table, 4)

            # Filter by status MISMATCH_FAIL
            status_sel = modal.query_one("#audit-status-filter", Select)
            status_sel.value = "MISMATCH_FAIL"
            await wait_for_rows(pilot, table, 1)

            # Filter by status UNVERIFIED
            status_sel.value = "UNVERIFIED"
            await wait_for_rows(pilot, table, 1)

    asyncio.run(_test())


def test_audit_report_modal_bindings_no_ctrl_c():
    """Verify ctrl+c keybinding is not present in AuditReportModal.BINDINGS."""
    ctrl_c_bindings = [
        b for b in AuditReportModal.BINDINGS if getattr(b, "key", None) == "ctrl+c"
    ]
    assert len(ctrl_c_bindings) == 0


def test_audit_report_modal_export_actions(sample_audit_report, tmp_path, monkeypatch):
    """Test JSON and CSV export buttons prompt with external path outside base_dir and block sorted-folder writes."""
    from app.ui.modals.reports import ExportReportModal

    assert not any(
        getattr(b, "key", None) == "ctrl+c" for b in AuditReportModal.BINDINGS
    )

    app_dir = tmp_path / "app_dir"
    sorted_dir = tmp_path / "sorted_folder"
    sorted_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    async def _test():
        sample_audit_report["base_dir"] = str(sorted_dir)
        modal = AuditReportModal(sample_audit_report)
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            # Pressing ctrl+c should not trigger report export or write CSV to disk
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert list(sorted_dir.iterdir()) == []

            # Pressing Export JSON opens ExportReportModal defaulting to <app_dir>/exports/audit_report.json
            btn_json = modal.query_one("#btn-export-json", Button)
            btn_json.press()
            await pilot.pause()
            assert isinstance(app.screen, ExportReportModal)
            expected_json = app_dir / "exports" / "audit_report.json"
            assert app.screen.query_one("#input-export-path", Input).value == str(
                expected_json
            )
            app.screen.query_one("#btn-export", Button).press()
            await pilot.pause()

            assert expected_json.exists()
            assert list(sorted_dir.iterdir()) == []

            # Pressing Export CSV opens ExportReportModal defaulting to <app_dir>/exports/audit_report.csv
            btn_csv = modal.query_one("#btn-export-csv", Button)
            btn_csv.press()
            await pilot.pause()
            assert isinstance(app.screen, ExportReportModal)
            expected_csv = app_dir / "exports" / "audit_report.csv"
            assert app.screen.query_one("#input-export-path", Input).value == str(
                expected_csv
            )
            app.screen.query_one("#btn-export", Button).press()
            await pilot.pause()

            assert expected_csv.exists()
            assert list(sorted_dir.iterdir()) == []

            # Attempting to save inside the sorted folder fails cleanly with AuditExportError announcement
            btn_json.press()
            await pilot.pause()
            assert isinstance(app.screen, ExportReportModal)
            inside_path = sorted_dir / "audit_report.json"
            app.screen.query_one("#input-export-path", Input).value = str(inside_path)
            app.screen.query_one("#btn-export", Button).press()
            await pilot.pause()

            assert not inside_path.exists()
            assert list(sorted_dir.iterdir()) == []
            assert "Failed to export audit report" in (
                modal.get_last_announcement() or ""
            )

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
        app.action_scan_directory = MagicMock()

        async with app.run_test() as pilot:
            worker = app.run_execute_worker()
            try:
                await worker.wait()
            except Exception:
                pass
            await pilot.pause()

            # Verify screen stack has pushed AuditReportModal
            assert isinstance(app.screen, AuditReportModal)

    asyncio.run(_test())


def test_audit_report_modal_failed_queue_table_and_retry(tmp_path):
    """Test AuditReportModal failed items table rendering and interactive retry actions."""

    async def _test():
        from app.core.extractor import get_file_hash

        src_file = tmp_path / "failed_doc.txt"
        src_file.write_text("retry content", encoding="utf-8")
        dst_file = tmp_path / "Sorted" / "failed_doc.txt"
        file_hash = get_file_hash(str(src_file))

        records = [
            {
                "source_path": str(src_file),
                "destination_path": str(dst_file),
                "pre_hash": file_hash,
                "post_hash": None,
                "status": "MOVE_FAIL",
                "error": "Permission denied / file lock",
            }
        ]
        audit_rep = generate_audit_report(str(tmp_path), records)
        modal = AuditReportModal(audit_rep)

        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test() as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            # Verify Failed Items queue table exists and lists the failed item
            failed_table = modal.query_one("#failed-items-table", DataTable)
            assert failed_table is not None
            assert failed_table.row_count == 1

            # Test Retry All action
            btn_retry_all = modal.query_one("#btn-retry-all", Button)
            btn_retry_all.press()
            await pilot.pause()

            # Verify file was moved to destination
            assert dst_file.exists()
            assert dst_file.read_text(encoding="utf-8") == "retry content"

            # Verify modal report metrics updated after retry
            assert modal.report_data.get("mismatch_fail_count") == 0
            assert modal.report_data.get("verified_pass_count") == 1

    asyncio.run(_test())


def test_mover_retry_failed_moves(tmp_path):
    """Test mover engine retry_failed_moves re-attempting file moves."""
    from app.core.mover import retry_failed_moves

    src_file = tmp_path / "item_to_retry.pdf"
    src_file.write_text("pdf bytes content", encoding="utf-8")
    dst_file = tmp_path / "Output" / "item_to_retry.pdf"

    from app.core.extractor import get_file_hash

    file_hash = get_file_hash(str(src_file))

    failed_items = [
        {
            "source_path": str(src_file),
            "destination_path": str(dst_file),
            "pre_hash": file_hash,
            "status": "MOVE_FAIL",
            "error": "Temporary lock failure",
        }
    ]

    res = retry_failed_moves(str(tmp_path), failed_items)

    assert res is not None
    assert dst_file.exists()
    assert dst_file.read_text(encoding="utf-8") == "pdf bytes content"

    rep = res.get("audit_report", {})
    assert rep.get("verified_pass_count") == 1
    assert rep.get("mismatch_fail_count") == 0


def test_audit_report_modal_literal_status_badges_on_cards_filter_and_retry(
    sample_audit_report, tmp_path
):
    """Verify [PASS], [FAIL], [SKIPPED], and [UNVERIFIED] render literally on stat cards, status Select, and after retry."""
    import re

    from textual.widgets import Static

    from app.core.extractor import get_file_hash

    def _svg_text(app: AutoSorterTUI) -> str:
        return re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")

    async def _test_mount_and_select():
        modal = AuditReportModal(sample_audit_report)
        settings = AppSettings(filepath=str(tmp_path / "settings_mount.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            cards = [str(card.render()) for card in modal.query(".stat-card")]
            assert cards == [
                "Total: 4",
                "[PASS]: 1",
                "[FAIL]: 1",
                "[SKIPPED]: 1",
                "[UNVERIFIED]: 1",
            ]
            mounted_svg = _svg_text(app)
            for expected in (
                "[PASS]: 1",
                "[FAIL]: 1",
                "[SKIPPED]: 1",
                "[UNVERIFIED]: 1",
            ):
                assert expected in mounted_svg

            status_sel = modal.query_one("#audit-status-filter", Select)
            status_sel.focus()
            status_sel.action_show_overlay()
            await pilot.pause()
            overlay_svg = _svg_text(app)
            for badge in ("[PASS]", "[FAIL]", "[SKIPPED]", "[UNVERIFIED]"):
                assert badge in overlay_svg

            await pilot.press("escape")
            await pilot.pause()

            for val, expected_label, badge in (
                ("VERIFIED_PASS", "Verified Pass [PASS]", "[PASS]"),
                ("MISMATCH_FAIL", "Mismatch Fail [FAIL]", "[FAIL]"),
                ("SKIPPED", "Skipped [SKIPPED]", "[SKIPPED]"),
                ("UNVERIFIED", "Unverified [UNVERIFIED]", "[UNVERIFIED]"),
            ):
                status_sel.value = val
                await pilot.pause()
                closed_label = str(status_sel.query_one("#label", Static).render())
                assert closed_label == expected_label
                assert badge in _svg_text(app)

    async def _test_after_retry():
        src_file = tmp_path / "retry_literal.txt"
        src_file.write_text("literal badge test", encoding="utf-8")
        dst_file = tmp_path / "Sorted" / "retry_literal.txt"
        file_hash = get_file_hash(str(src_file))
        audit_rep = generate_audit_report(
            str(tmp_path),
            [
                {
                    "source_path": str(src_file),
                    "destination_path": str(dst_file),
                    "pre_hash": file_hash,
                    "post_hash": None,
                    "status": "MOVE_FAIL",
                    "error": "Locked",
                }
            ],
        )
        modal = AuditReportModal(audit_rep)
        settings = AppSettings(filepath=str(tmp_path / "settings_retry.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(modal)
            await pilot.pause()

            before_cards = [str(card.render()) for card in modal.query(".stat-card")]
            assert "[FAIL]: 1" in before_cards
            assert "[PASS]: 0" in before_cards

            modal.query_one("#btn-retry-all", Button).press()
            await pilot.pause()

            after_cards = [str(card.render()) for card in modal.query(".stat-card")]
            assert after_cards == [
                "Total: 1",
                "[PASS]: 1",
                "[FAIL]: 0",
                "[SKIPPED]: 0",
                "[UNVERIFIED]: 0",
            ]
            after_svg = _svg_text(app)
            for expected in (
                "[PASS]: 1",
                "[FAIL]: 0",
                "[SKIPPED]: 0",
                "[UNVERIFIED]: 0",
            ):
                assert expected in after_svg

    asyncio.run(_test_mount_and_select())
    asyncio.run(_test_after_retry())
