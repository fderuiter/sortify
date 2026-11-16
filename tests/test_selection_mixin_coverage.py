"""Unit and integration tests for SelectionActionsMixin in app/ui/mixins/selection.py."""

import pytest
from textual.widgets import Tree

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def sample_tui_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr("app.core.session_catalog.find_abandoned_sessions", lambda: [])
    settings = AppSettings()
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    app.plan = {
        "Finance": {
            "invoice1.pdf": {
                "__type__": "file",
                "filepath": str(tmp_path / "invoice1.pdf"),
                "target_filename": "invoice1.pdf",
                "is_checked": True,
                "is_excluded": False,
                "is_locked": False,
                "folder": "Finance",
            },
            "invoice2.pdf": {
                "__type__": "file",
                "filepath": str(tmp_path / "invoice2.pdf"),
                "target_filename": "invoice2.pdf",
                "is_checked": True,
                "is_excluded": False,
                "is_locked": False,
                "folder": "Finance",
            },
        },
        "Legal": {
            "contract.docx": {
                "__type__": "file",
                "filepath": str(tmp_path / "contract.docx"),
                "target_filename": "contract.docx",
                "is_checked": False,
                "is_excluded": True,
                "is_locked": False,
                "folder": "Legal",
            }
        },
    }
    return app


@pytest.mark.anyio
async def test_selection_visual_range_toggle(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        tree = app.query_one("#plan-tree", Tree)
        tree.focus()

        # Toggle visual range select on
        assert app.visual_select_active is False
        app.action_visual_range_select()
        assert app.visual_select_active is True

        # Register nodes into visual selection
        if tree.root.children:
            child = tree.root.children[0]
            app.register_visual_selected_node(child)
            assert child in app.visual_selected_nodes
            # Registering again should not duplicate
            app.register_visual_selected_node(child)
            assert app.visual_selected_nodes.count(child) == 1

        # Toggle visual range select off
        app.action_visual_range_select()
        assert app.visual_select_active is False


@pytest.mark.anyio
async def test_selection_exclude_and_include(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        # Select all first
        app.action_toggle_select_all()

        # Exclude selected
        app.action_exclude_selected()
        for f in ["invoice1.pdf", "invoice2.pdf"]:
            assert app.plan["Finance"][f]["is_excluded"] is True
            assert app.plan["Finance"][f]["is_checked"] is False

        # Include selected
        # Mark one checked to include
        app.plan["Finance"]["invoice1.pdf"]["is_checked"] = True
        app.action_include_selected()
        assert app.plan["Finance"]["invoice1.pdf"]["is_excluded"] is False
        assert app.plan["Finance"]["invoice1.pdf"]["is_checked"] is True


@pytest.mark.anyio
async def test_selection_toggle_lock(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        tree = app.query_one("#plan-tree", Tree)

        # Find a file node
        def find_file_node(node):
            if node.data and node.data.get("is_file"):
                return node
            for child in node.children:
                found = find_file_node(child)
                if found:
                    return found
            return None

        file_node = find_file_node(tree.root)
        if file_node:
            app.active_tree_node = file_node
            assert file_node.data.get("is_locked") is False
            app.action_toggle_lock()
            assert file_node.data.get("is_locked") is True
            app.action_toggle_lock()
            assert file_node.data.get("is_locked") is False


@pytest.mark.anyio
async def test_selection_ratings(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        tree = app.query_one("#plan-tree", Tree)

        def find_file_node(node):
            if node.data and node.data.get("is_file"):
                return node
            for child in node.children:
                found = find_file_node(child)
                if found:
                    return found
            return None

        file_node = find_file_node(tree.root)
        if file_node:
            app.active_tree_node = file_node
            app.action_rate_positive()
            assert file_node.data.get("rating") == "positive"
            app.action_rate_negative()
            assert file_node.data.get("rating") == "negative"
            # Setting again toggles to None
            app.action_rate_negative()
            assert file_node.data.get("rating") is None


@pytest.mark.anyio
async def test_action_toggle_selection_visual_and_single(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        tree = app.query_one("#plan-tree", Tree)

        def find_file_node(node):
            if node.data and node.data.get("is_file"):
                return node
            for child in node.children:
                found = find_file_node(child)
                if found:
                    return found
            return None

        file_node = find_file_node(tree.root)
        if file_node:
            app.active_tree_node = file_node
            # Toggle single file
            app.action_toggle_selection()
            # Toggle visual selected nodes
            app.visual_select_active = True
            app.visual_selected_nodes = [file_node]
            app.action_toggle_selection()
            assert app.visual_select_active is False
            assert len(app.visual_selected_nodes) == 0


@pytest.mark.anyio
async def test_action_toggle_select_all_with_query(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        # Without search query
        app.action_toggle_select_all()
        # With search query matching files
        search_inp = app.query_one("#tree-search-input")
        search_inp.value = "invoice"
        app.action_toggle_select_all()
        # With query matching nothing
        search_inp.value = "nonexistent_term_xyz"
        app.action_toggle_select_all()


@pytest.mark.anyio
async def test_executable_plan_filtering(sample_tui_app):
    app = sample_tui_app
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app.plan["Finance"]["invoice1.pdf"]["is_checked"] = True
        app.plan["Finance"]["invoice1.pdf"]["is_excluded"] = False
        app.plan["Finance"]["invoice2.pdf"]["is_checked"] = False
        app.plan["Legal"]["contract.docx"]["is_excluded"] = True

        exec_plan = app._get_executable_plan()
        assert "Finance" in exec_plan
        assert "invoice1.pdf" in exec_plan["Finance"]
        assert "invoice2.pdf" not in exec_plan["Finance"]
        assert "Legal" not in exec_plan or len(exec_plan["Legal"]) == 0

        checked_keys = app._get_checked_file_keys()
        assert len(checked_keys) >= 1
