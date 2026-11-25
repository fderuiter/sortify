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

            # First Escape moves focus from search_input to plan-tree while keeping filter active
            await pilot.press("escape")
            await pilot.pause(0.05)

            assert search_input.value == "doc"
            assert tree.has_focus

            # Second Escape on plan-tree clears query and keeps tree focused
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

            # First Escape focuses plan-tree while keeping active filter
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert search_input.value == "report"
            assert tree.has_focus

            # Second Escape on plan-tree clears search filter and restores full tree
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


def test_tui_tree_filter_rendering_executes_zero_stat_calls(temp_workspace):
    """Verify tree rendering during debounced search filtering executes zero synchronous stat/lexists/samefile calls."""
    from unittest.mock import patch

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
            await app.workers.wait_for_complete()
            await pilot.pause(0.05)

            # Pre-populate collision paths as background worker would
            target_a = os.path.join(temp_workspace, "Alpha_Dir", "report_a.pdf")
            app.collision_paths = {os.path.normpath(target_a)}

            # Intercept filesystem stat calls during tree rendering
            with (
                patch("os.path.lexists") as mock_lexists,
                patch("os.path.samefile") as mock_samefile,
                patch("os.stat") as mock_stat,
                patch("os.lstat") as mock_lstat,
            ):
                search_input = app.query_one("#tree-search-input", Input)
                search_input.focus()
                await set_filter(app, pilot, "report")

                assert mock_lexists.call_count == 0
                assert mock_samefile.call_count == 0
                assert mock_stat.call_count == 0
                assert mock_lstat.call_count == 0

            # Verify that collision badge is rendered correctly from collision_paths
            tree = app.query_one("#plan-tree", Tree)
            alpha_node = next(
                node for node in tree.root.children if "Alpha_Dir" in str(node.label)
            )
            report_node = alpha_node.children[0]
            assert "[COLLISION:" in str(report_node.label)

    asyncio.run(_test())


def test_build_path_lookup_index_edge_cases(temp_workspace):
    """Verify _build_path_lookup_index normalizes Windows/POSIX separators, ignores empty strings, and extracts basenames."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

    # Test None and empty
    empty_idx = app._build_path_lookup_index(None)
    assert empty_idx.paths == set()
    assert empty_idx.basenames == set()

    empty_idx2 = app._build_path_lookup_index([])
    assert empty_idx2.paths == set()
    assert empty_idx2.basenames == set()

    # Test collection with empty strings, whitespace, and mixed separators
    input_paths = [
        "",
        "   ",
        None,
        "C:\\Users\\test\\folder\\doc_win.pdf",
        "/var/data/folder/doc_posix.txt",
        "relative/sub\\mixed_sep.csv",
        "lone_file.docx",
    ]
    idx = app._build_path_lookup_index(input_paths)

    # Empty strings and None must not be indexed
    assert "" not in idx.paths
    assert "   " not in idx.paths
    assert None not in idx.paths
    assert "" not in idx.basenames

    # Basenames must contain filenames
    assert "doc_win.pdf" in idx.basenames
    assert "doc_posix.txt" in idx.basenames
    assert "mixed_sep.csv" in idx.basenames
    assert "lone_file.docx" in idx.basenames

    # Normalized paths must be present
    assert "lone_file.docx" in idx.paths
    assert os.path.normpath("/var/data/folder/doc_posix.txt") in idx.paths
    assert (
        "relative/sub/mixed_sep.csv" in idx.paths
        or "relative/sub\\mixed_sep.csv" in idx.paths
    )

    # Test __contains__ operator
    assert "doc_win.pdf" in idx
    assert "doc_posix.txt" in idx
    assert "nonexistent_file.pdf" not in idx


def test_tui_tree_filter_collision_tag_lookups(temp_workspace):
    """Verify pre-indexed collision lookup correctly decorates tree nodes with collision badges."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            file_a = os.path.join(temp_workspace, "Alpha", "collision_a.pdf")
            file_b = os.path.join(temp_workspace, "Alpha", "normal_b.pdf")
            file_c = os.path.join(temp_workspace, "Beta", "collision_c.pdf")

            app.plan = {
                "Alpha": {
                    "collision_a.pdf": {
                        "__type__": "file",
                        "filepath": file_a,
                        "target_filename": "collision_a.pdf",
                    },
                    "normal_b.pdf": {
                        "__type__": "file",
                        "filepath": file_b,
                        "target_filename": "normal_b.pdf",
                    },
                },
                "Beta": {
                    "collision_c.pdf": {
                        "__type__": "file",
                        "filepath": file_c,
                        "target_filename": "collision_c.pdf",
                    },
                },
            }

            # Set collision paths: one via full path, one via basename
            app.collision_paths = {
                os.path.normpath(file_a),
                "collision_c.pdf",
            }

            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            alpha_node = next(n for n in tree.root.children if "Alpha" in str(n.label))
            beta_node = next(n for n in tree.root.children if "Beta" in str(n.label))

            alpha_children = {
                (c.data.get("key") if c.data else ""): str(c.label)
                for c in alpha_node.children
            }
            beta_children = {
                (c.data.get("key") if c.data else ""): str(c.label)
                for c in beta_node.children
            }

            assert "[COLLISION:" in alpha_children["collision_a.pdf"]
            assert "[COLLISION:" not in alpha_children["normal_b.pdf"]
            assert "[COLLISION:" in beta_children["collision_c.pdf"]

            # Filter tree to "collision_a" and verify collision badge persists
            search_input = app.query_one("#tree-search-input", Input)
            search_input.focus()
            await set_filter(app, pilot, "collision_a")

            alpha_node_filtered = next(
                n for n in tree.root.children if "Alpha" in str(n.label)
            )
            filtered_labels = [str(c.label) for c in alpha_node_filtered.children]
            assert any(
                "[COLLISION:" in l and "collision_a.pdf" in l for l in filtered_labels
            )

    asyncio.run(_test())


def test_tui_tree_filter_error_and_warning_badges(temp_workspace):
    """Verify pre-indexed error and warning lookups render [ERR] and [WARN] badges with correct precedence."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            file_err = os.path.join(temp_workspace, "Reports", "critical_err.pdf")
            file_warn = os.path.join(temp_workspace, "Reports", "warning_item.pdf")
            file_ok = os.path.join(temp_workspace, "Reports", "clean_item.pdf")

            app.plan = {
                "Reports": {
                    "critical_err.pdf": {
                        "__type__": "file",
                        "filepath": file_err,
                        "target_filename": "critical_err.pdf",
                    },
                    "warning_item.pdf": {
                        "__type__": "file",
                        "filepath": file_warn,
                        "target_filename": "warning_item.pdf",
                    },
                    "clean_item.pdf": {
                        "__type__": "file",
                        "filepath": file_ok,
                        "target_filename": "clean_item.pdf",
                    },
                }
            }

            app.critical_error_paths = {os.path.normpath(file_err)}
            app.warning_paths = {os.path.normpath(file_warn)}

            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            folder = tree.root.children[0]
            nodes = {c.data.get("key"): c for c in folder.children if c.data}

            err_label = str(nodes["critical_err.pdf"].label)
            warn_label = str(nodes["warning_item.pdf"].label)
            ok_label = str(nodes["clean_item.pdf"].label)

            assert "[ERR]" in err_label
            assert "[WARN]" not in err_label

            assert "[WARN]" in warn_label
            assert "[ERR]" not in warn_label

            assert "[ERR]" not in ok_label
            assert "[WARN]" not in ok_label

            # Verify precedence: if a path is in both critical and warning, critical wins
            app.critical_error_paths = {
                os.path.normpath(file_err),
                os.path.normpath(file_warn),
            }
            app.warning_paths = {os.path.normpath(file_warn)}
            app._apply_tree_node_styles()
            await pilot.pause(0.05)

            warn_node_promoted = str(nodes["warning_item.pdf"].label)
            assert "[ERR]" in warn_node_promoted
            assert "[WARN]" not in warn_node_promoted

    asyncio.run(_test())


def test_tui_tree_filter_lookup_efficiency_large_collection(temp_workspace):
    """Verify tree rendering scales with exact O(1) set lookups even when collections contain large numbers of paths."""
    import time

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            # Build plan with 50 files
            plan = {"Docs": {}}
            for i in range(50):
                fn = f"doc_{i:03d}.pdf"
                fp = os.path.join(temp_workspace, "Docs", fn)
                plan["Docs"][fn] = {
                    "__type__": "file",
                    "filepath": fp,
                    "target_filename": fn,
                }
            app.plan = plan

            # Populate collections with 1000 dummy non-matching paths plus 2 colliding
            dummy_collision_paths = {
                f"/some/other/nonexistent/path/dummy_{i:04d}.pdf" for i in range(1000)
            }
            colliding_file = os.path.join(temp_workspace, "Docs", "doc_025.pdf")
            dummy_collision_paths.add(os.path.normpath(colliding_file))
            app.collision_paths = dummy_collision_paths

            dummy_error_paths = {
                f"/error/path/unrelated_{i:04d}.pdf" for i in range(1000)
            }
            err_file = os.path.join(temp_workspace, "Docs", "doc_010.pdf")
            dummy_error_paths.add(os.path.normpath(err_file))
            app.critical_error_paths = dummy_error_paths

            start_t = time.perf_counter()
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)
            elapsed = time.perf_counter() - start_t

            # Tree rebuild with 1,000 paths in collision and error collections should finish promptly
            assert elapsed < 5.0

            tree = app.query_one("#plan-tree", Tree)
            docs_folder = tree.root.children[0]
            nodes = {
                c.data.get("key"): str(c.label) for c in docs_folder.children if c.data
            }

            assert "[COLLISION:" in nodes["doc_025.pdf"]
            assert "[ERR]" in nodes["doc_010.pdf"]
            assert "[COLLISION:" not in nodes["doc_001.pdf"]
            assert "[ERR]" not in nodes["doc_001.pdf"]

    asyncio.run(_test())


def test_path_lookup_index_helpers_and_tuple_coercion(temp_workspace):
    """Verify PathLookupIndex helpers distinguish path-only checks from basename __contains__ checks and coerce plain tuples."""
    from app.ui.mixins.tree_view import PathLookupIndex

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

    other_path = os.path.normpath("/other/folder/shared_name.pdf")
    raw_tuple: tuple[set[str], set[str]] = (
        {other_path, other_path.replace("\\", "/")},
        {"shared_name.pdf"},
    )
    coerced = PathLookupIndex._coerce(raw_tuple)
    assert isinstance(coerced, PathLookupIndex)
    assert PathLookupIndex._coerce(coerced) is coerced

    # _matches_path checks self.paths only and must not match self.basenames
    assert coerced._matches_path(other_path) is True
    assert coerced._matches_path("shared_name.pdf") is False
    assert coerced._matches_path("") is False
    assert coerced._matches_path(None) is False

    # _matches_node checks filepath via _matches_path and norm_key via __contains__
    assert coerced._matches_node("/unrelated/source.pdf", "shared_name.pdf") is True
    assert coerced._matches_node("shared_name.pdf", "distinct_key.pdf") is False

    # _matches_collision checks file_info flags, target_path (paths only), filepath (paths only), and key (__contains__)
    assert coerced._matches_collision({"collision": True}, None, None, None) is True
    assert coerced._matches_collision({"has_collision": True}, None, None, None) is True
    assert coerced._matches_collision({"is_collision": True}, None, None, None) is True
    assert (
        coerced._matches_collision(
            {}, "shared_name.pdf", "shared_name.pdf", "distinct_key.pdf"
        )
        is False
    )
    assert coerced._matches_collision({}, other_path, "/src/a.pdf", "a.pdf") is True
    assert (
        coerced._matches_collision({}, "/dst/a.pdf", "/src/a.pdf", "shared_name.pdf")
        is True
    )

    async def _test():
        async with app.run_test() as pilot:
            tree = app.query_one("#plan-tree", Tree)
            tree.reset("Plan")
            src_fp = os.path.join(temp_workspace, "Docs", "distinct_key.pdf")
            plan_sub = {
                "Docs": {
                    "distinct_key.pdf": {
                        "__type__": "file",
                        "filepath": src_fp,
                        "target_filename": "shared_name.pdf",
                    },
                    "shared_name.pdf": {
                        "__type__": "file",
                        "filepath": os.path.join(
                            temp_workspace, "Docs", "shared_name.pdf"
                        ),
                        "target_filename": "shared_name.pdf",
                    },
                }
            }
            # Pass plain 2-tuples directly to _build_tree_nodes
            added = app._build_tree_nodes(
                plan_sub,
                tree.root,
                current_folder="",
                crit_index=raw_tuple,
                warn_index=(set(), set()),
                collision_index=raw_tuple,
            )
            await pilot.pause(0.02)
            assert added == 3
            docs_node = tree.root.children[0]
            by_key = {
                c.data["key"]: c
                for c in docs_node.children
                if isinstance(c.data, dict) and "key" in c.data
            }
            assert "[ERR]" not in str(by_key["distinct_key.pdf"].label)
            assert "[COLLISION:" not in str(by_key["distinct_key.pdf"].label)
            assert "[ERR]" in str(by_key["shared_name.pdf"].label)
            assert "[COLLISION:" in str(by_key["shared_name.pdf"].label)

            # _apply_tree_node_styles must preserve pre-existing data["target_path"]
            # and must not match target_path or filepath against basenames
            custom_tp = "shared_name.pdf"
            assert isinstance(by_key["distinct_key.pdf"].data, dict)
            by_key["distinct_key.pdf"].data["target_path"] = custom_tp
            by_key["distinct_key.pdf"].data["filepath"] = "shared_name.pdf"
            app.collision_paths = {other_path}
            app.critical_error_paths = {other_path}
            app._apply_tree_node_styles()
            assert by_key["distinct_key.pdf"].data["target_path"] == custom_tp
            assert by_key["distinct_key.pdf"].data["is_collision"] is False
            assert "[ERR]" not in str(by_key["distinct_key.pdf"].label)
            assert "[COLLISION:" not in str(by_key["distinct_key.pdf"].label)

    asyncio.run(_test())


def test_resolve_node_target_path_windows_drives_and_fallbacks(temp_workspace):
    """Verify _resolve_node_target_path preserves Windows drive separators and handles base_dir and relative fallbacks."""
    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

    # Windows backslash drive override preserves backslash separator even on POSIX hosts
    win_back = app._resolve_node_target_path(
        {"destination_override": "C:\\Sorted\\Finance\\", "target_filename": "q4.pdf"},
        "IgnoredFolder",
        "raw.pdf",
    )
    assert win_back == "C:\\Sorted\\Finance\\q4.pdf"

    # Windows forward-slash drive override preserves forward-slash separator
    win_fwd = app._resolve_node_target_path(
        {"destination_override": "D:/Sorted/Legal/", "target_filename": "nda.pdf"},
        "IgnoredFolder",
        "raw.pdf",
    )
    assert win_fwd == "D:/Sorted/Legal/nda.pdf"

    # Relative folder joins against base_dir and falls back to file_key when target_filename is absent
    rel_joined = app._resolve_node_target_path({}, "Reports", "summary.pdf")
    assert rel_joined == os.path.normpath(
        os.path.join(str(temp_workspace), "Reports", "summary.pdf")
    )

    # Without base_dir, relative folder joins folder + filename, and empty folder returns None
    app.base_dir = ""
    assert app._resolve_node_target_path({}, "Reports", "summary.pdf") == (
        os.path.normpath(os.path.join("Reports", "summary.pdf"))
    )
    assert app._resolve_node_target_path({}, "", "summary.pdf") is None


def test_invalid_locks_parity_across_tree_and_inspector(temp_workspace):
    """Verify invalid_locks helpers support SortingPlan and dict plans and all 4 lookup variants in tree and inspector."""
    from app.core.analyzer import SortingPlan

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            fp_exact = os.path.join(temp_workspace, "Docs", "exact_fp.pdf")
            fp_by_key = os.path.join(temp_workspace, "Docs", "key_match.pdf")
            fp_win_sep = "C:\\Locked\\win_sep.pdf"
            fp_unnorm = os.path.join(temp_workspace, "Docs", ".", "norm_match.pdf")
            fp_clean = os.path.join(temp_workspace, "Docs", "clean.pdf")

            plan_nodes = {
                "Docs": {
                    "exact_fp.pdf": {
                        "__type__": "file",
                        "filepath": fp_exact,
                        "target_filename": "exact_fp.pdf",
                    },
                    "key_match.pdf": {
                        "__type__": "file",
                        "filepath": fp_by_key,
                        "target_filename": "key_match.pdf",
                    },
                    "win_sep.pdf": {
                        "__type__": "file",
                        "filepath": fp_win_sep,
                        "target_filename": "win_sep.pdf",
                    },
                    "norm_match.pdf": {
                        "__type__": "file",
                        "filepath": fp_unnorm,
                        "target_filename": "norm_match.pdf",
                    },
                    "empty_fp.pdf": {
                        "__type__": "file",
                        "filepath": "",
                        "target_filename": "empty_fp.pdf",
                    },
                    "clean.pdf": {
                        "__type__": "file",
                        "filepath": fp_clean,
                        "target_filename": "clean.pdf",
                    },
                }
            }
            locks = {
                fp_exact: "Invalid exact filepath lock",
                "key_match.pdf": "Invalid key lock",
                "C:/Locked/win_sep.pdf": "Invalid slash-normalized lock",
                os.path.normpath(fp_unnorm): "Invalid normpath lock",
                ".": "Dot sentinel must not match empty filepath",
            }

            # 1. Test with SortingPlan instance
            app.plan = SortingPlan(plan_nodes, invalid_locks=locks)
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            tree = app.query_one("#plan-tree", Tree)
            docs_node = tree.root.children[0]
            by_key = {
                c.data["key"]: c
                for c in docs_node.children
                if isinstance(c.data, dict) and "key" in c.data
            }

            expected_errors = {
                "exact_fp.pdf": "Invalid exact filepath lock",
                "key_match.pdf": "Invalid key lock",
                "win_sep.pdf": "Invalid slash-normalized lock",
                "norm_match.pdf": "Invalid normpath lock",
            }
            for k, expected_msg in expected_errors.items():
                assert "[LOCK WARNING]" in str(by_key[k].label)
                inspector_txt = app._update_inspector(by_key[k], announce=False)
                assert f"Lock Warning:[/bold error] {expected_msg}" in inspector_txt

            # Empty filepath fallback in _build_tree_nodes resolves via base_dir,
            # while direct helper calls with empty filepath must not match "."
            assert app._has_invalid_lock("", "unlisted.pdf", locks) is False
            assert app._get_invalid_lock_error("", "unlisted.pdf", locks) is None
            assert "[LOCK WARNING]" not in str(by_key["clean.pdf"].label)
            assert "Lock Warning:" not in app._update_inspector(
                by_key["clean.pdf"], announce=False
            )

            # 2. Test with plain dict plan carrying "invalid_locks"
            dict_plan = {
                "Docs": plan_nodes["Docs"],
                "invalid_locks": {"clean.pdf": "Dict plan lock error"},
            }
            app.plan = dict_plan
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            docs_node_dict = next(
                n for n in tree.root.children if "Docs" in str(n.label)
            )
            clean_node = next(
                c
                for c in docs_node_dict.children
                if isinstance(c.data, dict) and c.data.get("key") == "clean.pdf"
            )
            assert "[LOCK WARNING]" in str(clean_node.label)
            assert "Dict plan lock error" in app._update_inspector(
                clean_node, announce=False
            )

    asyncio.run(_test())


@pytest.mark.parametrize("handoff_key", ["enter", "down", "escape"])
def test_search_input_handoff_keys_flush_timer_keep_filter_and_enable_tree_actions(
    temp_workspace, handoff_key
):
    """Enter, Down, and Escape in #tree-search-input flush pending debounce, keep query, focus #plan-tree, and scope single-key actions to visible matches."""
    from app.ui.modals.edit import ReassignModal

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            inv_path = os.path.join(temp_workspace, "invoice_2026.pdf")
            note_path = os.path.join(temp_workspace, "notes_draft.txt")
            app.plan = {
                "Finance": {
                    "invoice_2026.pdf": {
                        "__type__": "file",
                        "filepath": inv_path,
                        "target_filename": "invoice_2026.pdf",
                        "proposed_folder": "Finance",
                        "is_checked": True,
                        "is_excluded": False,
                    },
                    "notes_draft.txt": {
                        "__type__": "file",
                        "filepath": note_path,
                        "target_filename": "notes_draft.txt",
                        "proposed_folder": "Finance",
                        "is_checked": True,
                        "is_excluded": False,
                    },
                }
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            search_input = app.query_one("#tree-search-input", Input)
            tree = app.query_one("#plan-tree", Tree)

            # Focus search input and type without waiting for 150ms debounce timer
            search_input.focus()
            await pilot.pause(0.02)
            await pilot.press("i", "n", "v", handoff_key)
            await pilot.pause(0.02)

            assert app._tree_filter_timer is None
            assert search_input.value == "inv"
            assert tree.has_focus

            # Only invoice_2026.pdf is rendered under Finance
            finance_node = tree.root.children[0]
            assert len(finance_node.children) == 1
            assert "invoice_2026.pdf" in str(finance_node.children[0].label)

            # Focus cursor on the matched file node and run single-key actions
            tree.select_node(finance_node.children[0])
            await pilot.pause(0.02)

            await pilot.press("space")
            await pilot.pause(0.02)
            assert app.plan["Finance"]["invoice_2026.pdf"]["is_checked"] is False
            assert app.plan["Finance"]["notes_draft.txt"]["is_checked"] is True

            await pilot.press("x")
            await pilot.pause(0.02)
            assert app.plan["Finance"]["invoice_2026.pdf"]["is_excluded"] is True
            assert app.plan["Finance"]["notes_draft.txt"]["is_excluded"] is False

            # Respect Ctrl+Alt+K / ENABLE_CHARACTER_KEY_SHORTCUTS after returning to #plan-tree
            await pilot.press("ctrl+alt+k")
            await pilot.pause(0.02)
            assert app.settings.ENABLE_CHARACTER_KEY_SHORTCUTS is False
            await pilot.press("i")
            await pilot.pause(0.02)
            assert app.plan["Finance"]["invoice_2026.pdf"]["is_excluded"] is True

            await pilot.press("ctrl+alt+k")
            await pilot.pause(0.02)
            assert app.settings.ENABLE_CHARACTER_KEY_SHORTCUTS is True
            await pilot.press("i")
            await pilot.pause(0.02)
            assert app.plan["Finance"]["invoice_2026.pdf"]["is_excluded"] is False
            assert app.plan["Finance"]["invoice_2026.pdf"]["is_checked"] is True
            assert app.plan["Finance"]["notes_draft.txt"]["is_excluded"] is False

            # Select/Deselect All ('a') only affects visible filtered matches
            await pilot.press("a")
            await pilot.pause(0.02)
            assert app.plan["Finance"]["invoice_2026.pdf"]["is_checked"] is False
            assert app.plan["Finance"]["notes_draft.txt"]["is_checked"] is True

            # Reassign ('r') opens ReassignModal and Escape closes modal without clearing search filter
            await pilot.press("r")
            await pilot.pause(0.05)
            assert isinstance(app.screen, ReassignModal)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ReassignModal)
            assert search_input.value == "inv"

    asyncio.run(_test())


def test_folder_actions_during_active_filter_only_affect_visible_matches(
    temp_workspace,
):
    """Nested folder Space and 'x' while a search filter is active only mutate visible matching files and preserve resolved paths."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            inv_path = os.path.join(temp_workspace, "invoice_2026.pdf")
            note_path = os.path.join(temp_workspace, "notes_draft.txt")
            with open(inv_path, "w", encoding="utf-8") as fh:
                fh.write("inv")
            with open(note_path, "w", encoding="utf-8") as fh:
                fh.write("note")

            app.plan = {
                "Finance": {
                    "Q1": {
                        "invoice_2026.pdf": {
                            "__type__": "file",
                            "relative_source": "../../invoice_2026.pdf",
                            "target_filename": "invoice_2026.pdf",
                            "is_checked": True,
                            "is_excluded": False,
                        },
                        "notes_draft.txt": {
                            "__type__": "file",
                            "relative_source": "../../notes_draft.txt",
                            "target_filename": "notes_draft.txt",
                            "is_checked": True,
                            "is_excluded": False,
                        },
                    }
                }
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            await set_filter(app, pilot, "invoice")
            tree = app.query_one("#plan-tree", Tree)
            tree.focus()
            finance_node = tree.root.children[0]
            tree.select_node(finance_node)
            await pilot.pause(0.05)

            await pilot.press("space")
            await pilot.pause(0.05)
            assert app.plan["Finance"]["Q1"]["invoice_2026.pdf"]["is_checked"] is False
            assert app.plan["Finance"]["Q1"]["notes_draft.txt"]["is_checked"] is True

            await pilot.press("x")
            await pilot.pause(0.05)
            assert app.plan["Finance"]["Q1"]["invoice_2026.pdf"]["is_excluded"] is True
            assert app.plan["Finance"]["Q1"]["notes_draft.txt"]["is_excluded"] is False

    asyncio.run(_test())


def test_plan_tree_escape_respects_visual_select_before_clearing_search_filter(
    temp_workspace,
):
    """Escape on #plan-tree does not clear an active search filter while visual_select_active is True, and clears it once visual_select_active is False."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test() as pilot:
            doc_path = os.path.join(temp_workspace, "doc_alpha.pdf")
            other_path = os.path.join(temp_workspace, "sheet_beta.xlsx")
            app.plan = {
                "Docs": {
                    "doc_alpha.pdf": {
                        "__type__": "file",
                        "filepath": doc_path,
                        "target_filename": "doc_alpha.pdf",
                    },
                    "sheet_beta.xlsx": {
                        "__type__": "file",
                        "filepath": other_path,
                        "target_filename": "sheet_beta.xlsx",
                    },
                }
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            search_input = app.query_one("#tree-search-input", Input)
            tree = app.query_one("#plan-tree", Tree)
            search_input.focus()
            await set_filter(app, pilot, "doc_alpha")
            await pilot.press("enter")
            await pilot.pause(0.05)

            assert tree.has_focus
            assert search_input.value == "doc_alpha"
            assert len(tree.root.children[0].children) == 1

            # Enter visual select mode on #plan-tree
            await pilot.press("v")
            await pilot.pause(0.05)
            assert app.visual_select_active is True

            # Escape while visual_select_active is True does NOT clear the search filter
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert search_input.value == "doc_alpha"
            assert len(tree.root.children[0].children) == 1

            # Exit visual select mode, then Escape clears filter and restores all plan nodes
            await pilot.press("v")
            await pilot.pause(0.05)
            assert app.visual_select_active is False

            await pilot.press("escape")
            await pilot.pause(0.05)
            assert search_input.value == ""
            assert len(tree.root.children[0].children) == 2

    asyncio.run(_test())


def test_status_bar_and_meta_details_preserve_literal_brackets_and_rich_labels(
    temp_workspace,
):
    """#status-bar disables markup parsing and #meta-details escapes dynamic values/badges (including resolved real source paths) while keeping bold/color label tags intact."""
    import re

    from textual.widgets import Static

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(temp_workspace))

        async with app.run_test(size=(120, 40)) as pilot:
            inbox_dir = os.path.join(temp_workspace, "inbox_[raw]")
            os.makedirs(inbox_dir, exist_ok=True)
            bracket_name = "report_[draft]_[2026].pdf"
            file_path = os.path.join(inbox_dir, bracket_name)
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write("sample")

            app.locked_files[bracket_name] = "Finance_[Q1]"
            app.collision_paths.add(file_path)
            app.plan = {
                "Finance_[Q1]": {
                    bracket_name: {
                        "__type__": "file",
                        "relative_source": f"../inbox_[raw]/{bracket_name}",
                        "target_filename": "2026_report_[final].pdf",
                        "routed_by": "rule_[G]",
                        "match": "Rule_[G] -> Finance_[Q1]",
                        "category": "Tax_[2026]",
                        "policy_action": "route_[PASS]",
                        "is_locked": True,
                    }
                },
                "invalid_locks": {file_path.replace("\\", "/"): "Broken [LOCK] target"},
            }
            app.rebuild_tree(run_validation=False)
            await pilot.pause(0.05)

            # Select the file node (with collision metadata active) to populate #meta-details
            tree = app.query_one("#plan-tree", Tree)
            folder_node = tree.root.children[0]
            file_node = folder_node.children[0]
            assert isinstance(file_node.data, dict)
            file_node.data["is_collision"] = True
            file_node.data["conflict_policy"] = "rename"
            tree.select_node(file_node)
            await pilot.pause(0.05)

            # Verify #status-bar preserves literal bracket tokens via update_status
            status_bar = app.query_one("#status-bar", Static)
            status_tail = f"Toggled [G] [x] [Locked] [100%] {bracket_name}"
            app.update_status(status_tail)
            await pilot.pause(0.05)
            rendered_status = str(status_bar.render())
            assert status_tail in rendered_status
            assert "[AI:" in rendered_status
            assert "[Daemon:" in rendered_status

            meta_rendered = str(app.query_one("#meta-details", Static).render())
            for expected_literal in (
                bracket_name,
                "inbox_[raw]",
                "Finance_[Q1]",
                "2026_report_[final].pdf",
                "Yes [LOCKED]",
                "Broken [LOCK] target",
                "rule_[G]",
                "Rule_[G] -> Finance_[Q1]",
                "Tax_[2026]",
                "route_[PASS]",
                "[COLLISION: RENAME]",
            ):
                assert expected_literal in meta_rendered

            # Label styling tags must be parsed as styles, not leaked as literal markup tags
            for style_tag in (
                "[bold accent]",
                "[bold error]",
                "[bold warning]",
                "[/bold]",
            ):
                assert style_tag not in meta_rendered

            svg_text = re.sub(r"<[^>]+>", "", app.export_screenshot()).replace(
                "&#160;", " "
            )
            assert "[G] [x] [Locked] [100%]" in svg_text
            assert "Yes [LOCKED]" in svg_text
            assert "[COLLISION: RENAME]" in svg_text

    asyncio.run(_test())
