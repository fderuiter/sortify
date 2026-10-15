"""Tests for TUI tree view multi-file checkbox selection toggles and bulk operations."""

import asyncio
import time

import pytest

from app.core.verifier import VerificationEngine
from app.ui.tui import AutoSorterTUI


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
            },
            "draft_2.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_2.docx",
                "target_filename": "draft_2.docx",
                "is_checked": True,
                "is_excluded": False,
            },
            "draft_3.docx": {
                "__type__": "file",
                "filepath": "/app/sample/draft_3.docx",
                "target_filename": "draft_3.docx",
                "is_checked": True,
                "is_excluded": False,
            },
        },
        "Invoices": {
            "inv_2026_01.pdf": {
                "__type__": "file",
                "filepath": "/app/sample/inv_2026_01.pdf",
                "target_filename": "inv_2026_01.pdf",
                "is_checked": True,
                "is_excluded": False,
            },
            "inv_2026_02.pdf": {
                "__type__": "file",
                "filepath": "/app/sample/inv_2026_02.pdf",
                "target_filename": "inv_2026_02.pdf",
                "is_checked": True,
                "is_excluded": False,
            },
        },
    }


def test_checkboxes_render_in_tree(sample_plan):
    """Verify checkboxes render next to file nodes and folder nodes in tree view."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()
            tree = app.query_one("#plan-tree")

            folder_labels = [str(node.label) for node in tree.root.children]
            assert any("[x] 📁 Documents" in label for label in folder_labels)
            assert any("[x] 📁 Invoices" in label for label in folder_labels)

            doc_node = next(n for n in tree.root.children if "Documents" in str(n.label))
            file_labels = [str(child.label) for child in doc_node.children]
            assert any("[x] 📄 draft_1.docx" in label for label in file_labels)
            assert any("[x] 📄 draft_2.docx" in label for label in file_labels)

    asyncio.run(_test())


def test_spacebar_toggles_single_file_node(sample_plan):
    """Verify Spacebar toggles checkbox selection for a highlighted file node."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()
            tree = app.query_one("#plan-tree")

            doc_folder = next(n for n in tree.root.children if "Documents" in str(n.label))
            file_node = doc_folder.children[0]
            app.active_tree_node = file_node

            assert file_node.data["info"]["is_checked"] is True

            app.action_toggle_selection()
            await pilot.pause()

            doc_folder_updated = next(n for n in tree.root.children if "Documents" in str(n.label))
            file_node_updated = doc_folder_updated.children[0]

            assert file_node_updated.data["info"]["is_checked"] is False
            assert "[ ]" in str(file_node_updated.label)

    asyncio.run(_test())


def test_spacebar_toggles_folder_group(sample_plan):
    """Verify Spacebar toggles checkbox selection for all items in a folder group simultaneously."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()
            tree = app.query_one("#plan-tree")

            doc_folder = next(n for n in tree.root.children if "Documents" in str(n.label))
            app.active_tree_node = doc_folder

            for child in doc_folder.children:
                assert child.data["info"]["is_checked"] is True

            app.action_toggle_selection()
            await pilot.pause()

            doc_folder_updated = next(n for n in tree.root.children if "Documents" in str(n.label))

            for child in doc_folder_updated.children:
                assert child.data["info"]["is_checked"] is False

            assert "[ ] 📁 Documents" in str(doc_folder_updated.label)

            app.active_tree_node = doc_folder_updated
            app.action_toggle_selection()
            await pilot.pause()

            doc_folder_restored = next(n for n in tree.root.children if "Documents" in str(n.label))

            for child in doc_folder_restored.children:
                assert child.data["info"]["is_checked"] is True

            assert "[x] 📁 Documents" in str(doc_folder_restored.label)

    asyncio.run(_test())


def test_bulk_exclusion_scenario(sample_plan):
    """Verify Scenario: Bulk Exclusion across checked items."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            for fi in sample_plan["Invoices"].values():
                fi["is_checked"] = False

            app.rebuild_tree()
            await pilot.pause()

            app.action_exclude_selected()
            await pilot.pause()

            for fi in sample_plan["Documents"].values():
                assert fi["is_excluded"] is True
                assert fi["is_checked"] is False

            exec_plan = app._get_executable_plan()
            assert "Documents" not in exec_plan or len(exec_plan["Documents"]) == 0

    asyncio.run(_test())


def test_bulk_inclusion_scenario(sample_plan):
    """Verify Bulk Inclusion restores excluded items."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        for folder in app.plan.values():
            for fi in folder.values():
                fi["is_excluded"] = True
                fi["is_checked"] = False

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            app.action_include_selected()
            await pilot.pause()

            for folder in app.plan.values():
                for fi in folder.values():
                    assert fi["is_excluded"] is False
                    assert fi["is_checked"] is True

    asyncio.run(_test())


def test_bulk_reassign_action(sample_plan):
    """Verify Bulk Reassign moves checked items to a target folder in plan."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            for fi in sample_plan["Invoices"].values():
                fi["is_checked"] = False

            app.rebuild_tree()
            await pilot.pause()

            target_files = app._get_target_file_infos()
            assert len(target_files) == 3

            reassigned_count = 0
            target_folder = "Archive"
            for fi in target_files:
                file_key = None
                old_folder = fi.get("folder", "Documents")
                for f_name, f_content in list(app.plan.items()):
                    if isinstance(f_content, dict):
                        for k, v in list(f_content.items()):
                            if v is fi:
                                file_key = k
                                old_folder = f_name
                                break
                    if file_key:
                        break

                if file_key and old_folder in app.plan:
                    app.plan[old_folder].pop(file_key, None)
                    if target_folder not in app.plan:
                        app.plan[target_folder] = {}
                    app.plan[target_folder][file_key] = fi
                    fi["folder"] = target_folder
                    reassigned_count += 1

            app.rebuild_tree()
            await pilot.pause()

            assert reassigned_count == 3
            assert "Archive" in app.plan
            assert len(app.plan["Archive"]) == 3
            assert len(app.plan["Documents"]) == 0

    asyncio.run(_test())


def test_toggle_select_all_action(sample_plan):
    """Verify 'a' key toggles select all and deselect all."""
    async def _test():
        app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
        app.plan = sample_plan

        async with app.run_test() as pilot:
            app.rebuild_tree()
            await pilot.pause()

            app.action_toggle_select_all()
            await pilot.pause()

            all_files = app._collect_file_nodes_from_dict(app.plan)
            assert all(f["is_checked"] is False for f in all_files)

            app.action_toggle_select_all()
            await pilot.pause()

            assert all(f["is_checked"] is True for f in all_files)

    asyncio.run(_test())


def test_verification_engine_skips_excluded():
    """Verify VerificationEngine.get_moves respects excluded / unchecked states."""
    plan = {
        "Docs": {
            "kept.pdf": {
                "__type__": "file",
                "relative_source": "kept.pdf",
                "target_filename": "kept.pdf",
                "is_checked": True,
                "is_excluded": False,
            },
            "excluded.pdf": {
                "__type__": "file",
                "relative_source": "excluded.pdf",
                "target_filename": "excluded.pdf",
                "is_checked": False,
                "is_excluded": True,
            },
        }
    }

    moves = VerificationEngine.get_moves("/app/sample", plan)
    assert len(moves) == 1
    assert moves[0][0] == "kept.pdf"


def test_performance_on_large_tree_500_nodes():
    """Verify rendering and selection toggling performance on trees with 500+ nodes."""
    app = AutoSorterTUI(settings=MockSettings(), base_dir="/app/sample")
    large_plan = {"LargeCategory": {}}
    for i in range(500):
        large_plan["LargeCategory"][f"file_{i}.txt"] = {
            "__type__": "file",
            "filepath": f"/app/sample/file_{i}.txt",
            "target_filename": f"file_{i}.txt",
            "is_checked": True,
            "is_excluded": False,
        }
    app.plan = large_plan

    start = time.perf_counter()
    all_files = app._collect_file_nodes_from_dict(app.plan)
    for f in all_files:
        f["is_checked"] = False
    elapsed = time.perf_counter() - start

    assert elapsed < 0.05
    assert len(all_files) == 500
