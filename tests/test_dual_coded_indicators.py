"""Unit and integration tests for dual-coded indicator system (WCAG 2.2 SC 1.4.1).

Verifies that tree node items and path comparison diffs convey error, warning,
and diff status through both explicit text badges/markers and typographic/color styles.
"""

import asyncio
import os
import tempfile
from typing import Any

import pytest
from rich.text import Text
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from app.config import AppSettings
from app.ui.tui import (
    AutoSorterTUI,
    _diff_highlight_paths,
)

pytestmark = pytest.mark.xdist_group(name="tui")


def _get_plain(node: TreeNode[Any]) -> str:
    """Return plain text string of a tree node label."""
    if isinstance(node.label, Text):
        return str(node.label.plain)
    return str(node.label)


def _get_styles(node: TreeNode[Any]) -> list[str]:
    """Return style string list for spans of a tree node label."""
    if isinstance(node.label, Text):
        return [str(span.style) for span in node.label.spans]
    return []


def _get_data(node: TreeNode[Any]) -> dict[str, Any]:
    """Return data dict of a tree node."""
    data = getattr(node, "data", None)
    return data if isinstance(data, dict) else {}


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_path_diff_dual_coded_markers_and_styles():
    """Verify path diffs use [-] with strikethrough for removals and [+] with underline for insertions."""
    # 1. Path replacement diff
    src, tgt = _diff_highlight_paths("docs/old_invoice.pdf", "Finance/new_invoice.pdf")
    assert "[-]" in src.plain
    assert "[+]" in tgt.plain

    src_styles = [str(span.style) for span in src.spans]
    tgt_styles = [str(span.style) for span in tgt.spans]

    # Deleted/replaced segment must have bold red strike
    assert any(
        "red" in s and ("strike" in s or "strikethrough" in s) for s in src_styles
    )
    # Inserted/replacement segment must have bold green underline
    assert any("green" in s and "underline" in s for s in tgt_styles)

    # 2. Pure deletion (source has segment deleted in target)
    src_del, tgt_del = _diff_highlight_paths("archive/2025/doc.txt", "archive/doc.txt")
    assert "[-]" in src_del.plain
    assert "[+]" not in tgt_del.plain
    assert any(
        "red" in str(span.style) and "strike" in str(span.style)
        for span in src_del.spans
    )

    # 3. Pure insertion (target has added segment)
    src_ins, tgt_ins = _diff_highlight_paths("archive/doc.txt", "archive/2026/doc.txt")
    assert "[-]" not in src_ins.plain
    assert "[+]" in tgt_ins.plain
    assert any(
        "green" in str(span.style) and "underline" in str(span.style)
        for span in tgt_ins.spans
    )

    # 4. Identical paths must remain free of diff markers and render in uniform neutral text
    src_id, tgt_id = _diff_highlight_paths("folder/file.pdf", "folder/file.pdf")
    assert "[-]" not in src_id.plain
    assert "[+]" not in src_id.plain
    assert "[-]" not in tgt_id.plain
    assert "[+]" not in tgt_id.plain
    assert src_id.plain == "folder/file.pdf"
    assert tgt_id.plain == "folder/file.pdf"
    assert src_id.style == "dim white"
    assert tgt_id.style == "dim white"

    # 5. Empty / None paths
    src_none, tgt_none = _diff_highlight_paths("", "")
    assert src_none.plain == "(None)"
    assert tgt_none.plain == "(None)"
    assert "[-]" not in src_none.plain
    assert "[+]" not in tgt_none.plain


def test_tree_node_dual_coded_badges_and_styles(temp_workspace):
    """Verify tree nodes show [ERR] + red for errors, [WARN] + yellow for warnings, none for normal."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            err_file = os.path.join(temp_workspace, "error_file.pdf")
            warn_file = os.path.join(temp_workspace, "warn_file.pdf")
            normal_file = os.path.join(temp_workspace, "normal_file.pdf")

            app.plan = {
                "Reports": {
                    "error_file.pdf": {
                        "__type__": "file",
                        "relative_source": "error_file.pdf",
                        "filepath": err_file,
                        "target_filename": "error_file.pdf",
                    },
                    "warn_file.pdf": {
                        "__type__": "file",
                        "relative_source": "warn_file.pdf",
                        "filepath": warn_file,
                        "target_filename": "warn_file.pdf",
                    },
                    "normal_file.pdf": {
                        "__type__": "file",
                        "relative_source": "normal_file.pdf",
                        "filepath": normal_file,
                        "target_filename": "normal_file.pdf",
                    },
                }
            }

            crit_paths = {os.path.normpath(err_file), "error_file.pdf"}
            warn_paths = {os.path.normpath(warn_file), "warn_file.pdf"}

            app.critical_error_paths = crit_paths
            app.warning_paths = warn_paths

            app.rebuild_tree()
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Apply tree node styles explicitly
            app.critical_error_paths = crit_paths
            app.warning_paths = warn_paths
            app._apply_tree_node_styles()
            await pilot.pause()

            tree = app.query_one("#plan-tree", Tree)
            reports_folder = tree.root.children[0]
            nodes_by_key = {
                _get_data(child).get("key"): child for child in reports_folder.children
            }
            err_node = nodes_by_key["error_file.pdf"]
            warn_node = nodes_by_key["warn_file.pdf"]
            normal_node = nodes_by_key["normal_file.pdf"]

            # Critical error node checks
            err_plain = _get_plain(err_node)
            assert "[ERR]" in err_plain
            assert "[WARN]" not in err_plain
            err_styles = _get_styles(err_node)
            assert any("red" in s for s in err_styles)

            # Warning node checks
            warn_plain = _get_plain(warn_node)
            assert "[WARN]" in warn_plain
            assert "[ERR]" not in warn_plain
            warn_styles = _get_styles(warn_node)
            assert any("yellow" in s for s in warn_styles)

            # Normal node checks
            normal_plain = _get_plain(normal_node)
            assert "[ERR]" not in normal_plain
            assert "[WARN]" not in normal_plain
            normal_styles = _get_styles(normal_node)
            assert not any("red" in s or "yellow" in s for s in normal_styles)

    asyncio.run(_test())


def test_tree_node_idempotence_and_state_transitions(temp_workspace):
    """Verify tree styling updates update badges idempotently without duplicates and handle transitions."""

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=temp_workspace)

        async with app.run_test() as pilot:
            file_a = os.path.join(temp_workspace, "doc_a.pdf")
            file_b = os.path.join(temp_workspace, "doc_b.pdf")

            app.plan = {
                "Folder": {
                    "doc_a.pdf": {
                        "__type__": "file",
                        "relative_source": "doc_a.pdf",
                        "filepath": file_a,
                        "target_filename": "doc_a.pdf",
                    },
                    "doc_b.pdf": {
                        "__type__": "file",
                        "relative_source": "doc_b.pdf",
                        "filepath": file_b,
                        "target_filename": "doc_b.pdf",
                    },
                }
            }

            # Initially: doc_a is critical, doc_b is warning
            app.critical_error_paths = {os.path.normpath(file_a)}
            app.warning_paths = {os.path.normpath(file_b)}

            app.rebuild_tree()
            await app.workers.wait_for_complete()
            await pilot.pause()

            # Ensure paths preserved
            app.critical_error_paths = {os.path.normpath(file_a)}
            app.warning_paths = {os.path.normpath(file_b)}

            # Call _apply_tree_node_styles 3 times in a row to verify idempotency
            for _ in range(3):
                app._apply_tree_node_styles()
                await pilot.pause()

            tree = app.query_one("#plan-tree", Tree)
            folder = tree.root.children[0]
            nodes_by_key = {
                _get_data(child).get("key"): child for child in folder.children
            }
            node_a = nodes_by_key["doc_a.pdf"]
            node_b = nodes_by_key["doc_b.pdf"]

            # Exactly one [ERR] badge, not duplicated
            plain_a = _get_plain(node_a)
            assert plain_a.count("[ERR]") == 1
            assert "[WARN]" not in plain_a

            # Exactly one [WARN] badge, not duplicated
            plain_b = _get_plain(node_b)
            assert plain_b.count("[WARN]") == 1
            assert "[ERR]" not in plain_b

            # State Transition 1: Clear all errors and warnings (everything normal)
            app.critical_error_paths = set()
            app.warning_paths = set()
            app._apply_tree_node_styles()
            await pilot.pause()

            plain_a = _get_plain(node_a)
            plain_b = _get_plain(node_b)
            assert "[ERR]" not in plain_a
            assert "[WARN]" not in plain_a
            assert not any("red" in s or "yellow" in s for s in _get_styles(node_a))

            assert "[ERR]" not in plain_b
            assert "[WARN]" not in plain_b
            assert not any("red" in s or "yellow" in s for s in _get_styles(node_b))

            # State Transition 2: doc_a becomes warning, doc_b becomes critical
            app.critical_error_paths = {os.path.normpath(file_b)}
            app.warning_paths = {os.path.normpath(file_a)}
            app._apply_tree_node_styles()
            await pilot.pause()

            plain_a = _get_plain(node_a)
            plain_b = _get_plain(node_b)
            assert plain_a.count("[WARN]") == 1
            assert "[ERR]" not in plain_a
            assert any("yellow" in s for s in _get_styles(node_a))

            assert plain_b.count("[ERR]") == 1
            assert "[WARN]" not in plain_b
            assert any("red" in s for s in _get_styles(node_b))

    asyncio.run(_test())
