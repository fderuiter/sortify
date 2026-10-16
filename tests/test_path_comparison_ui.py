"""Unit and integration tests for side-by-side path comparison pane in TUI preview UI."""

import asyncio
import os
import tempfile

import pytest
from rich.table import Table

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    _diff_highlight_paths,
    format_folder_path_comparison,
    format_side_by_side_path_comparison,
)

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        file1 = os.path.join(tmpdir, "doc1.pdf")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("test content")
        yield tmpdir


def test_diff_highlight_paths_identical():
    """Verify identical paths render without diff highlighting styles."""
    src, tgt = _diff_highlight_paths("/path/to/file.pdf", "/path/to/file.pdf")
    assert src.plain == "/path/to/file.pdf"
    assert tgt.plain == "/path/to/file.pdf"
    assert len(src.spans) == 0 or src.style == "dim white"


def test_diff_highlight_paths_differences():
    """Verify contrasting styles are applied to changed path segments."""
    src, tgt = _diff_highlight_paths("docs/invoice.pdf", "Finance/2026_invoice.pdf")
    assert src.plain == "docs/invoice.pdf"
    assert tgt.plain == "Finance/2026_invoice.pdf"

    # Check that red style exists in src spans and green style exists in tgt spans
    src_styles = [span.style for span in src.spans]
    tgt_styles = [span.style for span in tgt.spans]

    assert any("red" in str(s) for s in src_styles)
    assert any("green" in str(s) for s in tgt_styles)


def test_format_side_by_side_path_comparison_table():
    """Verify format_side_by_side_path_comparison generates a 2-column Rich Table."""
    table = format_side_by_side_path_comparison(
        source_path="/workspace/docs/report.pdf",
        target_path="/workspace/Finance/report.pdf",
        base_dir="/workspace",
    )
    assert isinstance(table, Table)
    assert len(table.columns) == 2
    assert table.columns[0].header == "Source Path (Current)"
    assert table.columns[1].header == "Target Path (Proposed)"


def test_format_folder_path_comparison_table():
    """Verify format_folder_path_comparison renders multiple file items."""
    files = [
        {
            "filepath": "/workspace/doc1.pdf",
            "key": "doc1.pdf",
            "folder": "Invoices",
            "target_filename": "doc1_invoice.pdf",
        },
        {
            "filepath": "/workspace/doc2.pdf",
            "key": "doc2.pdf",
            "folder": "Invoices",
            "target_filename": "doc2_invoice.pdf",
        },
    ]
    table = format_folder_path_comparison(files, base_dir="/workspace")
    assert isinstance(table, Table)
    assert len(table.columns) == 2


def test_tui_path_comparison_pane_mounted_and_updated(temp_workspace):
    """Verify AutoSorterTUI mounts #path-comparison widget and updates dynamically."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            # Check presence of path comparison widget and header
            path_comp = app.query_one("#path-comparison")
            assert path_comp is not None

            # Populate plan
            file1 = os.path.join(temp_workspace, "report_2026.pdf")
            app.plan = {
                "Finance": {
                    "report_2026.pdf": {
                        "__type__": "file",
                        "filepath": file1,
                        "target_filename": "2026_Finance_report.pdf",
                        "folder": "Finance",
                    }
                }
            }
            app.rebuild_tree()
            await pilot.pause()

            # Select file node
            tree = app.query_one("#plan-tree")
            file_node = tree.root.children[0].children[0]
            app.active_tree_node = file_node
            app._update_inspector(file_node)

            # Verify path comparison panel contains updated Rich Table
            assert isinstance(path_comp.content, Table)

            # Reassign file node to new folder 'Tax'
            app.plan["Tax"] = {
                "report_2026.pdf": app.plan["Finance"]["report_2026.pdf"]
            }
            del app.plan["Finance"]
            app.plan["Tax"]["report_2026.pdf"]["folder"] = "Tax"
            app.rebuild_tree()
            await pilot.pause()

            # Verify widget updated
            assert isinstance(path_comp.content, Table)

    asyncio.run(_test())
