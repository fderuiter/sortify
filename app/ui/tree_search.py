"""Debounced tree search filtering, focus handoff, and search-scoped node selection helpers."""

from typing import TYPE_CHECKING, Any

from textual import events
from textual.screen import ModalScreen
from textual.widgets import Input, Tree
from textual.widgets.tree import TreeNode

if TYPE_CHECKING:
    from app.ui.protocols import TUIHostProtocol

HANDOFF_KEYS: frozenset[str] = frozenset({"enter", "return", "down", "escape", "esc"})
ESCAPE_KEYS: frozenset[str] = frozenset({"escape", "esc"})


def collect_scoped_file_nodes(
    host: "TUIHostProtocol",
    node_dict: dict[str, Any],
    *,
    current_folder: str = "",
    query: str = "",
) -> list[dict[str, Any]]:
    """Collect file info dicts under ``node_dict``, respecting active ``query`` filters."""
    return host._collect_visible_file_nodes_from_dict(
        node_dict,
        current_folder=current_folder,
        query=query,
    )


def find_first_selectable_tree_node(root: TreeNode[Any]) -> TreeNode[Any] | None:
    """Return the first visible file node (or non-empty-state folder node) under ``root``."""
    first_folder: TreeNode[Any] | None = None

    def _walk(node: TreeNode[Any]) -> TreeNode[Any] | None:
        nonlocal first_folder
        for child in node.children:
            cdata = getattr(child, "data", None)
            if isinstance(cdata, dict) and not cdata.get("is_empty_state"):
                if cdata.get("is_file"):
                    return child
                if first_folder is None:
                    first_folder = child
            found = _walk(child)
            if found is not None:
                return found
        return None

    return _walk(root) or first_folder


def schedule_tree_search_filter(host: Any, value: str, *, delay: float = 0.15) -> None:
    """Debounce real-time ``#tree-search-input`` changes while skipping already-flushed queries."""
    if getattr(host, "_flushed_search_value", None) == value:
        host._flushed_search_value = None
        return
    host._flushed_search_value = None
    timer = getattr(host, "_tree_filter_timer", None)
    if timer is not None:
        timer.stop()
    host._tree_filter_pending = True

    def _apply_filter() -> None:
        host._tree_filter_pending = False
        host._tree_filter_timer = None
        host.render_tree(search_query=value)

    host._tree_filter_timer = host.set_timer(delay, _apply_filter)


def flush_tree_search_filter(host: Any) -> None:
    """Flush any pending debounced tree filter timer and render the current search query immediately."""
    inp_matches = host.query("#tree-search-input")
    if not inp_matches:
        return
    inp = inp_matches.first(Input)
    timer = getattr(host, "_tree_filter_timer", None)
    pending = bool(getattr(host, "_tree_filter_pending", False))
    if timer is not None:
        timer.stop()
        host._tree_filter_timer = None
    host._tree_filter_pending = False
    current_val = inp.value
    normalized_val = current_val.strip().lower()
    last_rendered = getattr(host, "_last_rendered_search_query", None)
    if pending or last_rendered != normalized_val:
        host._flushed_search_value = current_val
        host.render_tree(search_query=current_val)


def handoff_search_to_tree(host: Any) -> None:
    """Flush pending filter updates and move focus from ``#tree-search-input`` to ``#plan-tree``."""
    flush_tree_search_filter(host)
    tree_matches = host.query("#plan-tree")
    if not tree_matches:
        return
    tree = tree_matches.first(Tree)
    cursor_data = getattr(tree.cursor_node, "data", None) if tree.cursor_node else None
    if not (isinstance(cursor_data, dict) and not cursor_data.get("is_empty_state")):
        first_node = find_first_selectable_tree_node(tree.root)
        if first_node is not None:
            tree.select_node(first_node)
            host.active_tree_node = first_node
    tree.focus()


def handle_search_and_tree_key(host: Any, event: events.Key) -> bool:
    """Handle search-to-tree focus handoff keys and second-step ``Escape`` filter clearing."""
    if isinstance(host.screen, ModalScreen):
        return False

    inp_matches = host.query("#tree-search-input")
    if not inp_matches:
        return False
    inp = inp_matches.first(Input)

    if inp.has_focus and event.key in HANDOFF_KEYS:
        handoff_search_to_tree(host)
        event.stop()
        event.prevent_default()
        return True

    if event.key in ESCAPE_KEYS:
        tree_matches = host.query("#plan-tree")
        if not tree_matches:
            return False
        tree = tree_matches.first(Tree)
        has_pending = bool(getattr(host, "_tree_filter_pending", False))
        if (
            tree.has_focus
            and not getattr(host, "visual_select_active", False)
            and (bool(inp.value) or has_pending)
        ):
            timer = getattr(host, "_tree_filter_timer", None)
            if timer is not None:
                timer.stop()
                host._tree_filter_timer = None
            host._tree_filter_pending = False
            host._flushed_search_value = ""
            inp.value = ""
            host.render_tree(search_query="")
            event.stop()
            event.prevent_default()
            return True

    return False
