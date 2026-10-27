"""Unit test suite for real-time tree filter input bar in AutoSorterTUI."""

import asyncio
import os
import tempfile

import pytest
from textual.widgets import Input, Tree

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


@pytest.fixture(autouse=True)
def isolated_app_dir(monkeypatch, tmp_path):
    """Ensure AppSettings is isolated from persistent disk configuration changes."""
    import app.config
    import app.core.session_catalog

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(app.core.session_catalog, "find_abandoned_sessions", lambda: [])


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for TUI testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


async def set_filter(app, pilot, query: str, max_pauses: int = 100) -> None:
    """Type a tree filter query and wait for the debounced re-render.

    The input re-renders the tree 0.15 s after its last change, so a fixed
    0.2 s pause loses that race on a slow runner (seen on macOS CI). Record
    the queries the tree is rendered with and wait for this one.
    """
    rendered = []
    real_render = app.render_tree

    def recording_render(*args, **kwargs):
        real_render(*args, **kwargs)
        rendered.append(kwargs.get("search_query"))

    app.render_tree = recording_render
    try:
        app.query_one("#tree-search-input", Input).value = query
        for _ in range(max_pauses):
            if query in rendered:
                break
            await pilot.pause(0.05)
        else:
            pytest.fail(f"Tree was never filtered by {query!r}")
    finally:
        del app.render_tree
    await pilot.pause()


def test_tui_tree_search_input_layout(temp_workspace):
    """Verify Requirement 1: TUI preview layout renders a search input field above directory tree view."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            # Query search input field
            search_input = app.query_one("#tree-search-input", Input)
            assert search_input is not None
            assert search_input.placeholder == "Search tree nodes..."
            assert (
                search_input.tooltip
                == "Real-time search input field to filter preview tree nodes"
            )

            # Query plan tree
            tree = app.query_one("#plan-tree", Tree)
            assert tree is not None

            # Verify left tree pane container
            left_pane = app.query_one("#left-tree-pane")
            assert left_pane is not None

            # Verify layout order: search_input precedes tree in left_pane children
            children = list(left_pane.children)
            assert search_input in children
            assert tree in children
            assert children.index(search_input) < children.index(tree)

    asyncio.run(_test())


def test_tui_tree_search_focus_binding(temp_workspace):
    """Verify Ctrl+F key binding focuses the search input field."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            await pilot.pause(0.05)

            search_input = app.query_one("#tree-search-input", Input)
            tree = app.query_one("#plan-tree", Tree)

            tree.focus()
            await pilot.pause(0.05)
            assert tree.has_focus

            # Press Ctrl+F to focus search input
            await pilot.press("ctrl+f")
            await pilot.pause(0.05)
            assert search_input.has_focus

    asyncio.run(_test())


def test_tui_tree_filter_dynamic_substring_matching(temp_workspace):
    """Verify Requirement 2: Typing text into search field dynamically filters tree nodes matching search query."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            app.plan = {
                "Financial_Reports": {
                    "Q1_Invoice_2024.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "Q1_Invoice_2024.pdf"),
                        "target_filename": "Q1_Invoice_2024.pdf",
                    },
                    "Q2_Tax_Summary.docx": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "Q2_Tax_Summary.docx"),
                        "target_filename": "Q2_Tax_Summary.docx",
                    },
                },
                "Marketing_Docs": {
                    "Brochure_Design.png": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "Brochure_Design.png"),
                        "target_filename": "Brochure_Design.png",
                    },
                },
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            search_input = app.query_one("#tree-search-input", Input)

            # Initially all nodes present (2 folders)
            root_children_labels = [str(node.label) for node in tree.root.children]
            assert any("Financial_Reports" in label for label in root_children_labels)
            assert any("Marketing_Docs" in label for label in root_children_labels)

            # Type "invoice" (case-insensitive substring filter)
            search_input.focus()
            await set_filter(app, pilot, "invoice")

            filtered_root_labels = [str(node.label) for node in tree.root.children]
            # Financial_Reports is included because Q1_Invoice_2024.pdf matches "invoice"
            assert any("Financial_Reports" in label for label in filtered_root_labels)
            # Marketing_Docs is excluded because neither folder name nor children match "invoice"
            assert not any("Marketing_Docs" in label for label in filtered_root_labels)

            # Verify child leaf inside Financial_Reports
            fin_node = next(
                node
                for node in tree.root.children
                if "Financial_Reports" in str(node.label)
            )
            child_labels = [str(child.label) for child in fin_node.children]
            assert any("Q1_Invoice_2024.pdf" in label for label in child_labels)
            assert not any("Q2_Tax_Summary.docx" in label for label in child_labels)

    asyncio.run(_test())


def test_tui_tree_filter_clearing_restores_full_visibility(temp_workspace):
    """Verify Requirement 3: Clearing the search field restores full visibility of all preview tree nodes."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            app.plan = {
                "Alpha": {
                    "file_a.txt": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "file_a.txt"),
                        "target_filename": "file_a.txt",
                    }
                },
                "Beta": {
                    "file_b.txt": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "file_b.txt"),
                        "target_filename": "file_b.txt",
                    }
                },
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            search_input = app.query_one("#tree-search-input", Input)

            # Apply filter for "alpha"
            await set_filter(app, pilot, "alpha")

            assert len(tree.root.children) == 1
            assert "Alpha" in str(tree.root.children[0].label)

            # Clear search field
            await set_filter(app, pilot, "")

            # Restores full visibility of all nodes (both Alpha and Beta)
            restored_labels = [str(node.label) for node in tree.root.children]
            assert len(restored_labels) == 2
            assert any("Alpha" in label for label in restored_labels)
            assert any("Beta" in label for label in restored_labels)

    asyncio.run(_test())


def test_tui_tree_filter_no_matches_feedback(temp_workspace):
    """Verify empty state message when search query matches no nodes."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            app.plan = {
                "Documents": {
                    "report.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "report.pdf"),
                        "target_filename": "report.pdf",
                    }
                }
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            search_input = app.query_one("#tree-search-input", Input)

            # Type non-matching query
            await set_filter(app, pilot, "nonexistent_query_123")

            assert len(tree.root.children) == 1
            info_label = str(tree.root.children[0].label)
            assert (
                "No matching tree nodes found for 'nonexistent_query_123'" in info_label
            )

    asyncio.run(_test())


def test_tui_tree_filter_select_all_only_affects_visible_nodes(temp_workspace):
    """Verify pressing 'A' (Select All) when a tree search filter is active toggles only visible matching nodes."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            file1 = {
                "__type__": "file",
                "filepath": os.path.join(temp_workspace, "invoice_1.pdf"),
                "target_filename": "invoice_1.pdf",
                "is_checked": True,
            }
            file2 = {
                "__type__": "file",
                "filepath": os.path.join(temp_workspace, "invoice_2.pdf"),
                "target_filename": "invoice_2.pdf",
                "is_checked": True,
            }
            file3 = {
                "__type__": "file",
                "filepath": os.path.join(temp_workspace, "photo.jpg"),
                "target_filename": "photo.jpg",
                "is_checked": True,
            }
            app.plan = {
                "Invoices": {
                    "invoice_1.pdf": file1,
                    "invoice_2.pdf": file2,
                },
                "Photos": {
                    "photo.jpg": file3,
                },
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            search_input = app.query_one("#tree-search-input", Input)
            tree = app.query_one("#plan-tree", Tree)

            # Filter to 'invoice'
            await set_filter(app, pilot, "invoice")

            # Ensure focus is on tree, not search input, so 'a' hotkey triggers action_toggle_select_all
            tree.focus()
            await pilot.pause(0.05)

            # All visible files (invoice_1, invoice_2) are checked, so pressing 'a' deselects visible items
            await pilot.press("a")
            await pilot.pause(0.05)

            # Verify visible invoice files were unchecked
            assert file1["is_checked"] is False
            assert file2["is_checked"] is False
            # Verify hidden photo file remains checked
            assert file3["is_checked"] is True

            # Pressing 'a' again re-selects visible items
            await pilot.press("a")
            await pilot.pause(0.05)

            assert file1["is_checked"] is True
            assert file2["is_checked"] is True
            assert file3["is_checked"] is True

    asyncio.run(_test())


def test_tui_tree_filter_input_changes_do_not_trigger_plan_validation(
    temp_workspace, mocker
):
    """Verify typing into the search input filters tree nodes without invoking run_validation_worker."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            app.plan = {
                "Docs": {
                    "doc1.txt": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "doc1.txt"),
                        "target_filename": "doc1.txt",
                    }
                }
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            validation_spy = mocker.spy(app, "run_validation_worker")

            search_input = app.query_one("#tree-search-input", Input)
            search_input.focus()
            await set_filter(app, pilot, "doc")

            # Verify run_validation_worker was NOT called during filter input typing
            assert validation_spy.call_count == 0

    asyncio.run(_test())


def test_vim_tree_slash_hotkey_focuses_search_input(temp_workspace):
    """Verify pressing '/' while focused on VimTree focuses search input and highlights text."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            search_input = app.query_one("#tree-search-input", Input)
            tree = app.query_one("#plan-tree", Tree)

            tree.focus()
            await pilot.pause(0.05)
            assert tree.has_focus

            # Press '/' hotkey in VimTree
            await pilot.press("/")
            await pilot.pause(0.05)

            assert search_input.has_focus

    asyncio.run(_test())


def test_search_input_escape_clears_filter_and_focuses_tree(temp_workspace):
    """Verify pressing Escape in search input clears query, cancels timer, and focuses plan-tree."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            app.plan = {
                "Folder": {
                    "doc.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(temp_workspace, "doc.pdf"),
                        "target_filename": "doc.pdf",
                    }
                }
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            search_input = app.query_one("#tree-search-input", Input)
            tree = app.query_one("#plan-tree", Tree)

            search_input.focus()
            await set_filter(app, pilot, "doc")
            assert search_input.value == "doc"

            # Press Escape while search_input is focused
            await pilot.press("escape")
            await pilot.pause(0.05)

            assert search_input.value == ""
            assert tree.has_focus

    asyncio.run(_test())


def test_search_filter_preserves_cursor_and_restores_folder_expansion_state(
    temp_workspace,
):
    """Verify stateful folder expansion state preservation and cursor node persistence across search filtering."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            file_a_path = os.path.join(temp_workspace, "report_a.pdf")
            file_b_path = os.path.join(temp_workspace, "notes_b.txt")
            app.plan = {
                "Alpha_Dir": {
                    "report_a.pdf": {
                        "__type__": "file",
                        "filepath": file_a_path,
                        "target_filename": "report_a.pdf",
                    }
                },
                "Beta_Dir": {
                    "notes_b.txt": {
                        "__type__": "file",
                        "filepath": file_b_path,
                        "target_filename": "notes_b.txt",
                    }
                },
            }
            app.rebuild_tree()
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            search_input = app.query_one("#tree-search-input", Input)

            # Manually collapse Beta_Dir
            beta_node = next(
                node for node in tree.root.children if "Beta_Dir" in str(node.label)
            )
            beta_node.collapse()
            await pilot.pause(0.05)

            # Select file report_a.pdf
            alpha_node = next(
                node for node in tree.root.children if "Alpha_Dir" in str(node.label)
            )
            file_a_node = alpha_node.children[0]
            tree.select_node(file_a_node)
            await pilot.pause(0.05)

            assert app._last_cursor_key == file_a_path

            # Type search query "report"
            search_input.focus()
            await set_filter(app, pilot, "report")

            # Active file selection key preserved
            assert app._last_cursor_key == file_a_path

            # Clear search filter by pressing Escape
            await pilot.press("escape")
            await pilot.pause(0.05)

            # Verify search query cleared and tree focused
            assert search_input.value == ""
            assert tree.has_focus

            # Beta_Dir restored to collapsed state
            beta_node_restored = next(
                node for node in tree.root.children if "Beta_Dir" in str(node.label)
            )
            assert beta_node_restored.is_expanded is False

            # Active cursor highlights file_a_node
            active_node = app.active_tree_node or tree.cursor_node
            assert active_node is not None
            cdata = getattr(active_node, "data", {}) or {}
            assert cdata.get("filepath") == file_a_path

    asyncio.run(_test())
