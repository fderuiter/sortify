"""Structural host protocol shared by AutoSorterTUI and its UI behaviour mixins."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol, TypeVar, overload

from textual.app import AwaitMount, ScreenResultCallbackType, ScreenResultType
from textual.css.query import DOMQuery
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

if TYPE_CHECKING:
    from app.ui.mixins.tree_view import PathLookupIndex

_QueryType = TypeVar("_QueryType", bound=Widget)
_CallThreadReturnType = TypeVar("_CallThreadReturnType")


class TUIHostProtocol(Protocol):
    """Structural interface of ``AutoSorterTUI`` consumed by TUI behaviour mixins."""

    settings: Any
    db: Optional[Any]
    base_dir: str
    source_dirs: list[str]
    plan: dict[str, Any]
    locked_files: dict[str, str]
    _ratings_cache: dict[str, str]
    app_session: Optional[Any]
    pipeline: Optional[Any]
    active_tree_node: Optional[TreeNode[Any]]
    visual_select_active: bool
    visual_selected_nodes: list[TreeNode[Any]]
    _wizard_dismissed_this_session: bool
    verification_result: dict[str, Any]
    has_critical_errors: bool
    critical_error_paths: set[str]
    warning_paths: set[str]
    collision_paths: set[str]
    _daemon_state: str
    _daemon_is_moving: bool
    _daemon_watched_folders: list[str]
    _daemon_queue_depth: Optional[int]
    _daemon_max_queue: Optional[int]
    _daemon_active_workers: Optional[int]
    _tree_expansion_memory: dict[str, bool]
    _last_cursor_key: Optional[str]
    _pending_flush_trash: bool

    @overload
    def query_one(self, selector: str) -> Widget: ...

    @overload
    def query_one(self, selector: type[_QueryType]) -> _QueryType: ...

    @overload
    def query_one(self, selector: str, expect_type: type[_QueryType]) -> _QueryType: ...

    @overload
    def query(self, selector: str | None = None) -> DOMQuery[Widget]: ...

    @overload
    def query(self, selector: type[_QueryType]) -> DOMQuery[_QueryType]: ...

    @overload
    def push_screen(
        self,
        screen: Screen[ScreenResultType] | str,
        callback: ScreenResultCallbackType[ScreenResultType] | None = None,
        wait_for_dismiss: Literal[False] = False,
        *,
        mode: str | None = None,
    ) -> AwaitMount: ...

    @overload
    def push_screen(
        self,
        screen: Screen[ScreenResultType] | str,
        callback: ScreenResultCallbackType[ScreenResultType] | None = None,
        wait_for_dismiss: Literal[True] = True,
        *,
        mode: str | None = None,
    ) -> asyncio.Future[ScreenResultType]: ...

    def call_from_thread(
        self,
        callback: Callable[
            ..., _CallThreadReturnType | Awaitable[_CallThreadReturnType]
        ],
        *args: Any,
        **kwargs: Any,
    ) -> _CallThreadReturnType:
        """Schedule ``callback`` on the main Textual event loop from a worker thread."""
        ...

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """Display a transient notification toast in the TUI."""
        ...

    def announce(
        self,
        message: str,
        priority: str = "polite",
        help_url: Optional[str] = None,
    ) -> str:
        """Emit screen reader announcement and write to the TUI execution log."""
        ...

    def update_status(self, text: Optional[str] = None) -> None:
        """Update the TUI status bar text and badges."""
        ...

    def reset_progress_bar(self) -> None:
        """Reset and hide the main progress bar."""
        ...

    def _handle_progress_update(
        self,
        update_or_prog: Any = 0.0,
        stage: Optional[str] = None,
        unit_count: Optional[int] = None,
        unit_type: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Dispatch worker progress updates onto the main UI progress widgets."""
        ...

    def _start_hint(self) -> str:
        """Return onboarding hint text when no plan is loaded."""
        ...

    def _update_onboarding_cta_visibility(self) -> None:
        """Toggle visibility between the onboarding CTA card and inspector pane."""
        ...

    def _get_active_node(self) -> Optional[TreeNode[Any]]:
        """Return the currently highlighted or active plan tree node."""
        ...

    def _is_text_control_focused(self, allow_tree_search: bool = False) -> bool:
        """Return True if an Input or Select control holds active focus."""
        ...

    def action_select_dir(self) -> None:
        """Open the workspace directory selection modal."""
        ...

    def action_open_audit_report(
        self, audit_report: Optional[dict[str, Any]] = None
    ) -> None:
        """Open the post-move audit report modal."""
        ...

    # --- SortPipelineMixin methods ---

    def action_scan_directory(self) -> None:
        """Validate workspace selection and trigger a directory scan."""
        ...

    def _start_scan(self) -> None:
        """Announce and launch the background scan worker."""
        ...

    def _running_pipeline_worker(self) -> str | None:
        """Return the name of an active scan or execution worker, if any."""
        ...

    def _announce_if_busy(self, action: str) -> bool:
        """Announce and return True if a pipeline or daemon operation is active."""
        ...

    def _handle_pipeline_state_transition(
        self, old_state: Any, new_state: Any, context: dict[str, Any] | None = None
    ) -> None:
        """Record pipeline lifecycle state transitions."""
        ...

    def _handle_pipeline_telemetry(self, event: str, payload: dict[str, Any]) -> None:
        """Record telemetry events emitted by the sort pipeline."""
        ...

    def run_scan_worker(self) -> Any:
        """Launch background directory scan and plan generation worker."""
        ...

    def action_execute_sort(self) -> None:
        """Validate plan and execute scheduled file moves."""
        ...

    def _normalize_plan_for_verification(
        self, plan_dict: dict[str, Any], base_dir: str
    ) -> dict[str, Any]:
        """Populate source path fields across plan file entries before verification."""
        ...

    def run_validation_worker(self) -> Any:
        """Launch background plan integrity validation worker."""
        ...

    def _update_validation_state(self, result: dict[str, Any]) -> None:
        """Apply verification errors and warnings to UI banners and tree styles."""
        ...

    def run_execute_worker(self) -> Any:
        """Launch background file relocation worker."""
        ...

    # --- SelectionActionsMixin methods ---

    def register_visual_selected_node(self, node: TreeNode[Any]) -> None:
        """Append ``node`` to the active visual selection range."""
        ...

    def action_visual_range_select(self) -> None:
        """Toggle visual range selection mode."""
        ...

    def action_toggle_selection(self) -> None:
        """Toggle checkbox state on the active node or visual selection."""
        ...

    def _get_target_file_infos(self) -> list[dict[str, Any]]:
        """Return checked file info dicts or fall back to the focused node."""
        ...

    def action_exclude_selected(self) -> None:
        """Exclude checked or focused file items from the sorting plan."""
        ...

    def action_include_selected(self) -> None:
        """Include checked or focused file items in the sorting plan."""
        ...

    def action_reassign_selected(self) -> None:
        """Open reassignment modal for checked or focused file items."""
        ...

    def action_toggle_select_all(self) -> None:
        """Select or deselect all visible file items in the sorting plan."""
        ...

    def _get_executable_plan(
        self, plan_dict: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Filter ``plan_dict`` down to checked, non-excluded file entries."""
        ...

    def action_toggle_lock(self) -> None:
        """Lock or unlock the active file node's target folder assignment."""
        ...

    def _get_checked_file_keys(self) -> list[str]:
        """Return keys of all checked, non-excluded file nodes."""
        ...

    def _get_checked_file_entries(self) -> list[dict[str, Any]]:
        """Return metadata entries for all checked, non-excluded file nodes."""
        ...

    def action_rename_node(self) -> None:
        """Rename the active folder/file node or batch-rename checked files."""
        ...

    def action_new_folder(self) -> None:
        """Prompt to create a new folder category in the sorting plan."""
        ...

    def action_rate_positive(self) -> None:
        """Apply positive rating feedback to the active file node."""
        ...

    def action_rate_negative(self) -> None:
        """Apply negative rating feedback to the active file node."""
        ...

    def _set_rating_for_selected(self, rating: str) -> None:
        """Persist rating feedback for the active file node."""
        ...

    # --- TreeViewMixin methods ---

    def _build_path_lookup_index(self, paths: Optional[Any] = None) -> PathLookupIndex:
        """Pre-index path and basename variants for O(1) membership checks."""
        ...

    def _capture_tree_state(self, tree: Tree[Any], search_query: str = "") -> None:
        """Capture folder expansion state and cursor location prior to tree reset."""
        ...

    def _restore_tree_state(self, tree: Tree[Any], search_query: str = "") -> None:
        """Restore folder expansion state and cursor location after tree render."""
        ...

    def _apply_tree_node_styles(self) -> None:
        """Refresh error, warning, and collision badges on rendered tree nodes."""
        ...

    def _has_file_nodes(self, plan_dict: dict[str, Any]) -> bool:
        """Return True if ``plan_dict`` contains at least one file entry."""
        ...

    def _get_current_search_query(self) -> str:
        """Return the active lowercased search filter string."""
        ...

    def _build_matching_node_ids(
        self, plan_dict: dict[str, Any], query: str, current_folder: str = ""
    ) -> set[int]:
        """Collect object IDs of plan nodes matching ``query``."""
        ...

    def render_tree(self, search_query: Optional[str] = None) -> None:
        """Render the plan tree widget without triggering background validation."""
        ...

    def rebuild_tree(
        self, search_query: Optional[str] = None, run_validation: bool = True
    ) -> None:
        """Re-render the plan tree widget and optionally run integrity validation."""
        ...

    def _collect_file_nodes_from_dict(self, d: dict[str, Any]) -> list[dict[str, Any]]:
        """Recursively collect file info dicts under ``d``."""
        ...

    def _collect_visible_file_nodes_from_dict(
        self,
        node_dict: dict[str, Any],
        current_folder: str = "",
        query: str = "",
        parent_folder_matches: bool = False,
        matching_ids: Optional[set[int]] = None,
    ) -> list[dict[str, Any]]:
        """Collect file info dicts visible under the active search query."""
        ...

    def _node_matches_query(
        self, key: str, val: dict[str, Any], current_folder: str, query: str
    ) -> bool:
        """Return True if ``val`` or any descendant matches ``query``."""
        ...

    def _build_tree_nodes(
        self,
        node_dict: dict[str, Any],
        parent_item: TreeNode[Any],
        current_folder: str,
        query: str = "",
        parent_folder_matches: bool = False,
        matching_ids: Optional[set[int]] = None,
        crit_index: Optional[tuple[set[str], set[str]]] = None,
        warn_index: Optional[tuple[set[str], set[str]]] = None,
        collision_index: Optional[tuple[set[str], set[str]]] = None,
    ) -> int:
        """Recursively populate ``parent_item`` from ``node_dict``."""
        ...

    def _update_inspector(self, node_or_data: Any = None, announce: bool = True) -> str:
        """Update the metadata inspector and side-by-side path comparison panes."""
        ...
