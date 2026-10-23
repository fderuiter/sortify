"""Unit and integration tests for interactive duplicate review modal and resolution engine."""

import os
from unittest import mock

import pytest
from textual.widgets import Button, Select

from app.config import AppSettings
from app.core.verifier import VerificationEngine
from app.ui.tui import AutoSorterTUI, DuplicateReviewModal


def test_get_duplicate_groups_single_and_multi_collisions(tmp_path):
    """Test detection of duplicate destination target collisions in move plans."""
    base_dir = str(tmp_path)
    file1 = tmp_path / "doc1.pdf"
    file2 = tmp_path / "doc2.pdf"
    file1.write_text("content 1")
    file2.write_text("content 2")

    plan = {
        "doc1.pdf": {
            "node_type": "file",
            "relative_source": "doc1.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
        "doc2.pdf": {
            "node_type": "file",
            "relative_source": "doc2.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
    }

    groups = VerificationEngine.get_duplicate_groups(base_dir, plan)
    assert len(groups) == 1
    group = groups[0]
    assert group["target_filename"] == "report.pdf"
    assert len(group["items"]) == 2
    assert group["items"][0]["source_path"] == os.path.normpath(str(file1))
    assert group["items"][1]["source_path"] == os.path.normpath(str(file2))


def test_apply_deduplication_selections_rename(tmp_path):
    """Test applying 'rename' action auto-numbers duplicate target filenames."""
    base_dir = str(tmp_path)
    file1 = tmp_path / "doc1.pdf"
    file2 = tmp_path / "doc2.pdf"
    file1.write_text("content 1")
    file2.write_text("content 2")

    plan = {
        "doc1.pdf": {
            "node_type": "file",
            "relative_source": "doc1.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
        "doc2.pdf": {
            "node_type": "file",
            "relative_source": "doc2.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
    }

    groups = VerificationEngine.get_duplicate_groups(base_dir, plan)
    assert len(groups) == 1
    gid = groups[0]["group_id"]

    selections = {gid: "rename"}
    updated_plan = VerificationEngine.apply_deduplication_selections(
        base_dir, plan, selections
    )

    assert updated_plan["doc1.pdf"]["target_filename"] == "report.pdf"
    assert updated_plan["doc2.pdf"]["target_filename"] == "report_1.pdf"
    assert updated_plan["doc1.pdf"].get("confirmed") is True
    assert updated_plan["doc2.pdf"].get("confirmed") is True


def test_apply_deduplication_selections_skip(tmp_path):
    """Test applying 'skip' action marks duplicate nodes as excluded."""
    base_dir = str(tmp_path)
    file1 = tmp_path / "doc1.pdf"
    file2 = tmp_path / "doc2.pdf"
    file1.write_text("content 1")
    file2.write_text("content 2")

    plan = {
        "doc1.pdf": {
            "node_type": "file",
            "relative_source": "doc1.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
        "doc2.pdf": {
            "node_type": "file",
            "relative_source": "doc2.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
    }

    groups = VerificationEngine.get_duplicate_groups(base_dir, plan)
    assert len(groups) == 1
    gid = groups[0]["group_id"]

    selections = {gid: "skip"}
    updated_plan = VerificationEngine.apply_deduplication_selections(
        base_dir, plan, selections
    )

    assert updated_plan["doc1.pdf"].get("is_excluded") is True
    assert updated_plan["doc2.pdf"].get("is_excluded") is True
    assert updated_plan["doc1.pdf"].get("is_checked") is False
    assert updated_plan["doc2.pdf"].get("is_checked") is False


def test_apply_deduplication_selections_keep(tmp_path):
    """Test applying 'keep' action retains primary target and excludes secondary source."""
    base_dir = str(tmp_path)
    file1 = tmp_path / "doc1.pdf"
    file2 = tmp_path / "doc2.pdf"
    file1.write_text("content 1")
    file2.write_text("content 2")

    plan = {
        "doc1.pdf": {
            "node_type": "file",
            "relative_source": "doc1.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
        "doc2.pdf": {
            "node_type": "file",
            "relative_source": "doc2.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
    }

    groups = VerificationEngine.get_duplicate_groups(base_dir, plan)
    assert len(groups) == 1
    gid = groups[0]["group_id"]

    selections = {gid: "keep"}
    updated_plan = VerificationEngine.apply_deduplication_selections(
        base_dir, plan, selections
    )

    assert updated_plan["doc1.pdf"]["target_filename"] == "report.pdf"
    assert updated_plan["doc1.pdf"].get("confirmed") is True
    assert updated_plan["doc2.pdf"].get("is_excluded") is True
    assert updated_plan["doc2.pdf"].get("is_checked") is False


@pytest.mark.anyio
async def test_duplicate_review_modal_pilot():
    """Test DuplicateReviewModal composition, rendering, and action confirm in TUI pilot."""
    groups = [
        {
            "group_id": "group_1",
            "target_path": "/tmp/output/report.pdf",
            "target_filename": "report.pdf",
            "target_dir": "/tmp/output",
            "existing_file_on_disk": False,
            "items": [
                {
                    "node_key": "doc1.pdf",
                    "source_path": "/tmp/doc1.pdf",
                    "relative_source": "doc1.pdf",
                },
                {
                    "node_key": "doc2.pdf",
                    "source_path": "/tmp/doc2.pdf",
                    "relative_source": "doc2.pdf",
                },
            ],
            "default_action": "rename",
        }
    ]

    modal = DuplicateReviewModal(duplicate_groups=groups)
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings)

    async with app.run_test() as pilot:
        await app.push_screen(modal)
        await pilot.pause()

        assert modal.user_selections["group_1"] == "rename"
        # Confirm modal
        modal.action_confirm()
        await pilot.pause()
        assert modal.user_selections == {"group_1": "rename"}


@pytest.mark.anyio
async def test_tui_action_execute_sort_intercepts_duplicates(tmp_path):
    """Test AutoSorterTUI.action_execute_sort intercepts duplicates and pops DuplicateReviewModal."""
    base_dir = str(tmp_path)
    file1 = tmp_path / "doc1.pdf"
    file2 = tmp_path / "doc2.pdf"
    file1.write_text("content 1")
    file2.write_text("content 2")

    plan = {
        "doc1.pdf": {
            "node_type": "file",
            "relative_source": "doc1.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
        "doc2.pdf": {
            "node_type": "file",
            "relative_source": "doc2.pdf",
            "target_filename": "report.pdf",
            "is_checked": True,
        },
    }

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings)

    mock_session = mock.MagicMock()
    mock_session.base_dir = base_dir
    app.app_session = mock_session
    app.plan = plan

    async with app.run_test() as pilot:
        with mock.patch.object(app, "run_execute_worker") as mock_worker:
            app.action_execute_sort()
            await pilot.pause()

            # Verify DuplicateReviewModal screen is top screen
            assert isinstance(app.screen, DuplicateReviewModal)
            modal = app.screen
            assert len(modal.duplicate_groups) == 1

            # Confirm modal
            modal.action_confirm()
            await pilot.pause()

            # Verify deduplication was applied and run_execute_worker was called
            assert plan["doc1.pdf"]["target_filename"] == "report.pdf"
            assert plan["doc2.pdf"]["target_filename"] == "report_1.pdf"
            mock_worker.assert_called_once()


@pytest.mark.anyio
async def test_duplicate_review_modal_batch_apply_all_skip():
    """Test batch action bar applies chosen resolution action to all duplicate groups."""
    groups = [
        {
            "group_id": "group_1",
            "target_filename": "report1.pdf",
            "target_dir": "/tmp/output",
            "items": [{"source_path": "/tmp/a.pdf"}, {"source_path": "/tmp/b.pdf"}],
            "default_action": "rename",
        },
        {
            "group_id": "group_2",
            "target_filename": "report2.pdf",
            "target_dir": "/tmp/output",
            "items": [{"source_path": "/tmp/c.pdf"}, {"source_path": "/tmp/d.pdf"}],
            "default_action": "rename",
        },
        {
            "group_id": "group_3",
            "target_filename": "report3.pdf",
            "target_dir": "/tmp/output",
            "items": [{"source_path": "/tmp/e.pdf"}, {"source_path": "/tmp/f.pdf"}],
            "default_action": "rename",
        },
    ]

    modal = DuplicateReviewModal(duplicate_groups=groups)
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings)

    async with app.run_test() as pilot:
        await app.push_screen(modal)
        await pilot.pause()

        # Initial default selections
        assert modal.user_selections == {
            "group_1": "rename",
            "group_2": "rename",
            "group_3": "rename",
        }

        # Select 'skip' in batch action dropdown and press 'Apply to All'
        batch_select = modal.query_one("#batch-action-select", Select)
        batch_select.value = "skip"
        await pilot.pause()

        apply_btn = modal.query_one("#btn-apply-all", Button)
        await pilot.click("#btn-apply-all")
        await pilot.pause()

        # Verify all group dropdowns and user_selections state updated to 'skip'
        assert modal.user_selections == {
            "group_1": "skip",
            "group_2": "skip",
            "group_3": "skip",
        }
        for gid in ["group_1", "group_2", "group_3"]:
            assert modal.query_one(f"#select-action-{gid}", Select).value == "skip"


@pytest.mark.anyio
async def test_duplicate_review_modal_batch_apply_with_individual_override():
    """Test batch action application followed by individual group dropdown override."""
    groups = [
        {
            "group_id": "group_1",
            "target_filename": "report1.pdf",
            "target_dir": "/tmp/output",
            "items": [{"source_path": "/tmp/a.pdf"}, {"source_path": "/tmp/b.pdf"}],
            "default_action": "rename",
        },
        {
            "group_id": "group_2",
            "target_filename": "report2.pdf",
            "target_dir": "/tmp/output",
            "items": [{"source_path": "/tmp/c.pdf"}, {"source_path": "/tmp/d.pdf"}],
            "default_action": "rename",
        },
    ]

    modal = DuplicateReviewModal(duplicate_groups=groups)
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings)

    async with app.run_test() as pilot:
        await app.push_screen(modal)
        await pilot.pause()

        # Apply 'keep' to all
        modal.query_one("#batch-action-select", Select).value = "keep"
        await pilot.click("#btn-apply-all")
        await pilot.pause()

        assert modal.user_selections == {"group_1": "keep", "group_2": "keep"}

        # Override group_2 to 'skip'
        modal.query_one("#select-action-group_2", Select).value = "skip"
        await pilot.pause()

        # Verify group_1 remains 'keep' while group_2 is 'skip'
        assert modal.user_selections == {"group_1": "keep", "group_2": "skip"}

        # Confirm modal
        modal.action_confirm()
        await pilot.pause()
        assert modal.user_selections == {"group_1": "keep", "group_2": "skip"}
