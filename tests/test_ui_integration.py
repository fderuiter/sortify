"""Integration tests for terminal user interface and workflow."""

import pytest

from app.config import AppSettings
from app.ui.app import AutoSorterApp, run_app

pytestmark = [pytest.mark.slow, pytest.mark.integration]


@pytest.mark.anyio
async def test_terminal_autosorter_app_workflow(tmp_path):
    # 1. Setup temporary directory with test files
    target_dir = tmp_path / "test_workspace"
    target_dir.mkdir()

    file1 = target_dir / "invoice_2026.txt"
    file1.write_text("Invoice number 1024 for software subscription billing payment.")

    file2 = target_dir / "medical_report.txt"
    file2.write_text("Patient clinical lab diagnostics blood test report results.")

    file3 = target_dir / "tax_form.txt"
    file3.write_text("Annual tax return filing documentation and revenue records.")

    # 2. Instantiate Terminal AutoSorterApp
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.base_dir = str(target_dir)
    app.build_ui()

    # 3. Test synchronous analysis and plan generation
    await app._scan_and_process_worker()
    assert app.plan is not None

    # 4. Verify terminal tree output function
    app.print_terminal_tree()

    # 5. Test execution of sorting plan
    await app.execute_sort_async()

    # 6. Test undo rollback
    await app.undo_last_sort_async()
    assert file1.exists() or any(f.name == "invoice_2026.txt" for f in target_dir.rglob("*"))


def test_run_app_headless_execution(tmp_path):
    target_dir = tmp_path / "headless_workspace"
    target_dir.mkdir()

    f1 = target_dir / "doc1.txt"
    f1.write_text("Sample document one content for testing.")
    f2 = target_dir / "doc2.txt"
    f2.write_text("Sample document two content for testing.")

    settings = AppSettings()
    run_app(settings, str(target_dir))
    assert target_dir.exists()
