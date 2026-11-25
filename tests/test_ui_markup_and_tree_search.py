"""Unit tests for app.ui.markup_utils and app.ui.tree_search helpers."""

from typing import Any

from textual.content import Content
from textual.widgets import Tree

from app.ui.markup_utils import (
    escape_textual_markup,
    format_file_inspector_lines,
    format_folder_inspector_lines,
    format_labeled_markup_line,
    literal_content,
)
from app.ui.tree_search import find_first_selectable_tree_node


def test_markup_utils_escapes_brackets_and_wraps_literal_content() -> None:
    """escape_textual_markup and literal_content preserve uppercase, lowercase, and unclosed brackets."""
    raw = "C:\\Users\\[Admin]\\report_[draft]_[PASS].pdf"
    escaped = escape_textual_markup(raw)
    assert escaped == "C:\\Users\\\\[Admin]\\report_\\[draft]_\\[PASS].pdf"

    content = literal_content("Verified Pass [PASS]")
    assert isinstance(content, Content)
    assert content.plain == "Verified Pass [PASS]"

    labeled = format_labeled_markup_line(
        "Lock Warning", "Broken [LOCK]", label_style="bold error"
    )
    assert labeled == "[bold error]Lock Warning:[/bold error] Broken \\[LOCK]"


def test_format_file_and_folder_inspector_lines_cover_edge_cases() -> None:
    """format_file_inspector_lines formats clustering keywords, fallback scores, archival priorities, and collisions."""
    info: dict[str, Any] = {
        "target_filename": "2026_[final].pdf",
        "routed_by": "clustering",
        "policy_priority": 5,
        "policy_action": "quarantine_[WARN]",
        "keyword": "[tax, 2026]",
        "jev_category": "Finance_[A]",
        "sensitivity_rating": "HIGH_[PII]",
        "sensitivity_score": "[unparsed]",
        "archival_priority": 1,
        "archival_priority_score": "0.875",
        "confidence": "[n/a]",
    }
    lines = format_file_inspector_lines(
        key="doc_[1].pdf",
        path_display="/workspace/inbox_[raw]/doc_[1].pdf",
        folder="Finance/[Q1]",
        locked="Yes [LOCKED]",
        rating="positive",
        info=info,
        lock_error="Invalid [LOCK] path",
        is_collision=True,
        conflict_policy="skip",
    )
    joined = "\n".join(lines)
    assert "[bold accent]File:[/bold accent] doc_\\[1].pdf" in joined
    assert "[bold]Path:[/bold] /workspace/inbox_\\[raw]/doc_\\[1].pdf" in joined
    assert "[bold]Target Folder:[/bold] Finance/\\[Q1]" in joined
    assert "[bold]Target Filename:[/bold] 2026_\\[final].pdf" in joined
    assert "[bold]Locked:[/bold] Yes \\[LOCKED]" in joined
    assert "[bold error]Lock Warning:[/bold error] Invalid \\[LOCK] path" in joined
    assert "[bold]Cluster Keywords:[/bold] \\[tax, 2026]" in joined
    assert "[bold]Category:[/bold] Finance_\\[A]" in joined
    assert "[bold]Sensitivity Rating:[/bold] HIGH_\\[PII]" in joined
    assert "[bold]Sensitivity Score:[/bold] \\[unparsed]" in joined
    assert "[bold]Archival Priority:[/bold] P1" in joined
    assert "[bold]Archival Priority Score:[/bold] 0.88" in joined
    assert "[bold]Confidence:[/bold] \\[n/a]" in joined
    assert (
        "[bold warning]Collision Indicator:[/bold warning] \\[COLLISION: SKIP]"
        in joined
    )

    folder_lines = format_folder_inspector_lines(
        key="Archive_[2026]", folder="Legal/Archive_[2026]"
    )
    assert folder_lines == [
        "[bold accent]Folder Category:[/bold accent] Archive_\\[2026]",
        "[bold]Relative Path:[/bold] Legal/Archive_\\[2026]",
    ]


def test_find_first_selectable_tree_node_skips_empty_states_and_prefers_files() -> None:
    """find_first_selectable_tree_node ignores empty-state leaves, falls back to empty folders, and prefers files."""
    empty_tree: Tree[Any] = Tree("Root")
    empty_tree.root.add_leaf("No matches", data={"is_empty_state": True})
    assert find_first_selectable_tree_node(empty_tree.root) is None

    folder_only_tree: Tree[Any] = Tree("Root")
    empty_folder = folder_only_tree.root.add(
        "ManualFolder", data={"is_file": False, "folder": "ManualFolder"}
    )
    assert find_first_selectable_tree_node(folder_only_tree.root) is empty_folder

    mixed_tree: Tree[Any] = Tree("Root")
    folder_a = mixed_tree.root.add("FolderA", data={"is_file": False, "folder": "A"})
    folder_b = mixed_tree.root.add("FolderB", data={"is_file": False, "folder": "B"})
    file_leaf = folder_b.add_leaf("doc.pdf", data={"is_file": True, "key": "doc.pdf"})
    assert folder_a is not None
    assert find_first_selectable_tree_node(mixed_tree.root) is file_leaf
