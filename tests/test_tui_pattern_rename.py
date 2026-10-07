"""Unit and async integration tests for RenameModal pattern token formatting in Textual TUI."""

import pytest

from app.ui.tui import RenameModal


@pytest.mark.anyio
async def test_rename_modal_preview_update():
    modal = RenameModal(
        title="Rename File: report.pdf",
        current_name="report",
        extension=".pdf",
        category="Finance",
        file_date="2026-10-07",
        seq=1,
    )
    # Test preview evaluation logic directly
    preview_static = modal._evaluate_preview("{date}_{category}_{original}")
    assert preview_static == "2026-10-07_Finance_report.pdf"


@pytest.mark.anyio
async def test_rename_modal_explicit_extension_token():
    modal = RenameModal(
        title="Rename File: doc.txt",
        current_name="doc",
        extension=".txt",
        category="Notes",
        file_date="2026-10-07",
        seq=2,
    )
    preview = modal._evaluate_preview("{date}_{seq}_{original}{extension}")
    assert preview == "2026-10-07_02_doc.txt"


@pytest.mark.anyio
async def test_rename_modal_sanitized_preview():
    modal = RenameModal(
        title="Rename File: dangerous:file*.pdf",
        current_name="dangerous:file*",
        extension=".pdf",
        category="Bad/Category?",
        file_date="2026-10-07",
    )
    preview = modal._evaluate_preview("{date}_{category}_{original}")
    assert ":" not in preview
    assert "*" not in preview
    assert "?" not in preview
    assert preview.endswith(".pdf")
