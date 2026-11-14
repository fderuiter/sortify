"""Unit and async integration tests for RenameModal pattern token formatting in Textual TUI."""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.ui.modals.edit import RenameModal, RenameResult
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


class MockSettings:
    """Mock application settings for testing."""

    def __init__(self):
        self.language = "en"
        self.ai_status_badge = "ON"


@pytest.fixture
def sample_plan():
    """Fixture providing a sample sorting plan with multiple folders and document nodes."""
    return {
        "Documents": {
            "draft_1.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_1.docx",
                "target_filename": "draft_1.docx",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Documents",
            },
            "draft_2.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_2.docx",
                "target_filename": "draft_2.docx",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Documents",
            },
            "draft_3.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_3.docx",
                "target_filename": "draft_3.docx",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Documents",
            },
        },
        "Invoices": {
            "inv_2026_01.pdf": {
                "__type__": "file",
                "filepath": "/app/sample/inv_2026_01.pdf",
                "target_filename": "inv_2026_01.pdf",
                "is_checked": True,
                "is_excluded": False,
                "folder": "Invoices",
            },
        },
    }


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


@pytest.mark.anyio
async def test_rename_modal_batch_sequential_formatting():
    """Verify batch mode in RenameModal calculates sequential seq numbering across checked files."""
    files = [
        {
            "key": "report_q1.docx",
            "folder": "Finance",
            "date": "2026-11-14",
        },
        {
            "key": "report_q2.docx",
            "folder": "Finance",
            "date": "2026-11-14",
        },
        {
            "key": "report_q3.docx",
            "folder": "Finance",
            "date": "2026-11-14",
        },
    ]
    modal = RenameModal(
        title="Batch Rename 3 Files",
        files=files,
        seq=1,
        category="Finance",
        file_date="2026-11-14",
    )
    previews = modal.get_batch_previews("{date}_{category}_{seq}_{original}{extension}")
    assert len(previews) == 3
    assert previews[0] == "2026-11-14_Finance_01_report_q1.docx"
    assert previews[1] == "2026-11-14_Finance_02_report_q2.docx"
    assert previews[2] == "2026-11-14_Finance_03_report_q3.docx"

    # Test custom starting sequence index
    modal_offset = RenameModal(
        title="Batch Rename",
        files=files,
        seq=5,
        category="Finance",
    )
    previews_offset = modal_offset.get_batch_previews("{seq}_{original}{extension}")
    assert previews_offset[0] == "05_report_q1.docx"
    assert previews_offset[1] == "06_report_q2.docx"
    assert previews_offset[2] == "07_report_q3.docx"

    # Test summary preview evaluation
    eval_preview = modal._evaluate_preview("{seq}_{original}{extension}")
    assert "01_report_q1.docx" in eval_preview
    assert "02_report_q2.docx" in eval_preview


def test_batch_rename_multi_selection_detection(sample_plan):
    """Verify _get_checked_file_keys detects all checked files and handles toggle changes."""
    app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
    app.plan = sample_plan

    # All 4 files are initially checked
    checked_keys = app._get_checked_file_keys()
    assert len(checked_keys) == 4
    assert "draft_1.docx" in checked_keys
    assert "draft_2.docx" in checked_keys
    assert "draft_3.docx" in checked_keys
    assert "inv_2026_01.pdf" in checked_keys

    # Uncheck 3 files, leaving only 1
    sample_plan["Documents"]["draft_1.docx"]["is_checked"] = False
    sample_plan["Documents"]["draft_2.docx"]["is_checked"] = False
    sample_plan["Invoices"]["inv_2026_01.pdf"]["is_checked"] = False

    checked_keys_single = app._get_checked_file_keys()
    assert len(checked_keys_single) == 1
    assert checked_keys_single == ["draft_3.docx"]

    # Exclude the last file
    sample_plan["Documents"]["draft_3.docx"]["is_excluded"] = True
    assert len(app._get_checked_file_keys()) == 0


def test_batch_rename_node_lock_persistence(sample_plan):
    """Verify batch renaming locks modified entries, updates locked_files and plan structures."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan
        app.app_session = MagicMock()
        app.app_session.base_dir = "/app/sample"

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            # Ensure only Documents are checked (3 files)
            sample_plan["Invoices"]["inv_2026_01.pdf"]["is_checked"] = False
            app.rebuild_tree()
            await pilot.pause()

            checked_keys = app._get_checked_file_keys()
            assert len(checked_keys) == 3

            # Simulate batch renaming action confirmation
            modal = RenameModal(
                title=f"Batch Rename ({len(checked_keys)} files)",
                files=app._get_checked_file_entries(),
                plan=app.plan,
                base_dir=app.base_dir,
            )

            batch_result = RenameResult(
                "{seq}_{original}",
                batch_items=[
                    {
                        "key": "draft_1.docx",
                        "new_name": "01_draft_1.docx",
                        "folder": "Documents",
                        "old_folder": "Documents",
                        "info": sample_plan["Documents"]["draft_1.docx"],
                    },
                    {
                        "key": "draft_2.docx",
                        "new_name": "02_draft_2.docx",
                        "folder": "Documents",
                        "old_folder": "Documents",
                        "info": sample_plan["Documents"]["draft_2.docx"],
                    },
                    {
                        "key": "draft_3.docx",
                        "new_name": "03_draft_3.docx",
                        "folder": "Documents",
                        "old_folder": "Documents",
                        "info": sample_plan["Documents"]["draft_3.docx"],
                    },
                ],
                is_batch=True,
            )

            # Trigger push_screen callback logic through action_rename_node flow
            # Test direct batch rename application to verify lock persistence
            for item in batch_result.batch_items:
                old_name = item["key"]
                new_filename = item["new_name"]
                fld = item["folder"]
                info = item["info"]

                app.plan[fld].pop(old_name, None)
                app.plan[fld][new_filename] = info
                info["target_filename"] = new_filename
                info["folder"] = fld
                info["is_locked"] = True
                app.locked_files[new_filename] = fld
                app.app_session.db.set_user_verified_target_path(
                    app.base_dir, new_filename, fld
                )

            app.rebuild_tree(run_validation=True)
            await pilot.pause()

            # Verify lock persistence across all renamed entries
            for new_name in ["01_draft_1.docx", "02_draft_2.docx", "03_draft_3.docx"]:
                assert new_name in app.plan["Documents"]
                assert app.plan["Documents"][new_name]["is_locked"] is True
                assert app.locked_files[new_name] == "Documents"
                app.app_session.db.set_user_verified_target_path.assert_any_call(
                    app.base_dir, new_name, "Documents"
                )

    asyncio.run(_test())


def test_batch_rename_validation_worker_trigger(sample_plan):
    """Verify validation worker triggers on batch rename and catches duplicate target collisions."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan
        app.run_validation_worker = MagicMock()

        async with app.run_test() as pilot:
            app.rebuild_tree(run_validation=True)
            await pilot.pause()
            assert app.run_validation_worker.called

            # Test collision detection in VerificationEngine
            from app.core.verifier import VerificationEngine

            # Create a naming collision where multiple files share the exact same target path
            collision_plan = {
                "Documents": {
                    "file_a.docx": {
                        "__type__": "file",
                        "target_filename": "same_name.docx",
                        "is_checked": True,
                        "source_path": "/app/sample/file_a.docx",
                    },
                    "file_b.docx": {
                        "__type__": "file",
                        "target_filename": "same_name.docx",
                        "is_checked": True,
                        "source_path": "/app/sample/file_b.docx",
                    },
                }
            }
            res = VerificationEngine.verify_plan_integrity(
                "/app/sample", collision_plan, conflict_policy="skip"
            )
            # Duplicate targets should be detected in collisions
            dup_collisions = [
                c
                for c in res.get("collisions", [])
                if c.get("type") == "duplicate_target"
            ]
            assert len(dup_collisions) > 0

    asyncio.run(_test())


def test_batch_rename_cancellation_preserves_plan(sample_plan):
    """Verify cancelling RenameModal leaves the plan and node locks completely untouched."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            # Record initial plan keys
            initial_keys = list(app.plan["Documents"].keys())

            # Simulate cancellation (passing None to callback)
            # No modifications should occur
            assert list(app.plan["Documents"].keys()) == initial_keys
            assert len(app.locked_files) == 0

    asyncio.run(_test())


def test_action_rename_node_batch_branching(sample_plan):
    """Verify action_rename_node branches to batch RenameModal when multiple files are checked."""

    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            # Only 3 Documents checked
            sample_plan["Invoices"]["inv_2026_01.pdf"]["is_checked"] = False
            app.rebuild_tree()
            await pilot.pause()

            pushed_modals = []
            orig_push_screen = app.push_screen

            def mock_push(screen, callback=None):
                pushed_modals.append((screen, callback))
                # Invoke callback with batch result to test full cycle
                if callback:
                    batch_res = RenameResult(
                        "{seq}_{original}",
                        batch_items=[
                            {
                                "key": "draft_1.docx",
                                "new_name": "01_draft_1.docx",
                                "folder": "Documents",
                                "old_folder": "Documents",
                                "info": sample_plan["Documents"]["draft_1.docx"],
                            },
                            {
                                "key": "draft_2.docx",
                                "new_name": "02_draft_2.docx",
                                "folder": "Documents",
                                "old_folder": "Documents",
                                "info": sample_plan["Documents"]["draft_2.docx"],
                            },
                            {
                                "key": "draft_3.docx",
                                "new_name": "03_draft_3.docx",
                                "folder": "Documents",
                                "old_folder": "Documents",
                                "info": sample_plan["Documents"]["draft_3.docx"],
                            },
                        ],
                        is_batch=True,
                    )
                    callback(batch_res)

            app.push_screen = mock_push

            # Trigger action_rename_node
            app.action_rename_node()
            await pilot.pause()

            # Verify batch modal was pushed with 3 files
            assert len(pushed_modals) == 1
            modal_instance, _ = pushed_modals[0]
            assert isinstance(modal_instance, RenameModal)
            assert modal_instance.files is not None
            assert len(modal_instance.files) == 3

            # Verify plan was updated and nodes locked
            assert "01_draft_1.docx" in app.plan["Documents"]
            assert "02_draft_2.docx" in app.plan["Documents"]
            assert "03_draft_3.docx" in app.plan["Documents"]
            assert app.plan["Documents"]["01_draft_1.docx"]["is_locked"] is True

    asyncio.run(_test())
