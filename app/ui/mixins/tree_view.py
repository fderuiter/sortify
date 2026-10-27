"""Plan tree rendering, search filtering and the inspector panel for the TUI."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from rich.text import Text
from textual.widgets import Input, Static, Tree
from textual.widgets.tree import TreeNode

from app.ui.widgets.path_diff import (
    format_folder_path_comparison,
    format_side_by_side_path_comparison,
)

logger = logging.getLogger(__name__)


class TreeViewMixin:
    """Render the plan tree, filter it by search query and fill the inspector."""

    _tree_expansion_memory: Dict[str, bool] = {}
    _last_cursor_key: Optional[str] = None

    def _capture_tree_state(self, tree: Tree, search_query: str = "") -> None:
        """Record node expansion status and cursor key prior to tree rebuild."""
        if (
            not hasattr(self, "_tree_expansion_memory")
            or self._tree_expansion_memory is None
        ):
            self._tree_expansion_memory = {}

        def _walk(node: TreeNode):
            data = getattr(node, "data", {}) or {}
            if isinstance(data, dict):
                key = data.get("filepath") or data.get("folder") or data.get("key")
                if key and not data.get("is_file") and not search_query:
                    self._tree_expansion_memory[key] = node.is_expanded
            for child in node.children:
                _walk(child)

        _walk(tree.root)

        node_for_cursor = tree.cursor_node or getattr(self, "active_tree_node", None)
        if node_for_cursor and getattr(node_for_cursor, "data", None):
            cdata = node_for_cursor.data
            if isinstance(cdata, dict):
                key = cdata.get("filepath") or cdata.get("folder") or cdata.get("key")
                if key:
                    self._last_cursor_key = key

    def _restore_tree_state(self, tree: Tree, search_query: str = "") -> None:
        """Apply saved expansion states and restore cursor position after render."""
        target_cursor_node = None
        last_key = getattr(self, "_last_cursor_key", None)
        expansion_memory = getattr(self, "_tree_expansion_memory", {}) or {}

        def _walk(node: TreeNode):
            nonlocal target_cursor_node
            data = getattr(node, "data", {}) or {}
            if isinstance(data, dict):
                key = data.get("filepath") or data.get("folder") or data.get("key")

                if key and key == last_key:
                    target_cursor_node = node

                if not search_query and key in expansion_memory:
                    if expansion_memory[key]:
                        node.expand()
                    else:
                        node.collapse()
            for child in node.children:
                _walk(child)

        _walk(tree.root)
        if target_cursor_node:
            curr = target_cursor_node.parent
            while curr:
                curr.expand()
                curr = curr.parent
            tree.select_node(target_cursor_node)
            self.active_tree_node = target_cursor_node

    def _apply_tree_node_styles(self) -> None:
        """Apply red/yellow styling to existing tree nodes based on active error/warning paths."""
        try:
            tree = self.query_one("#plan-tree", Tree)

            def _stylize_node(node: TreeNode) -> None:
                data = getattr(node, "data", None)
                if not data or not isinstance(data, dict):
                    for child in node.children:
                        _stylize_node(child)
                    return

                filepath = data.get("filepath", "")
                key = data.get("key", "")
                norm_fp = os.path.normpath(filepath) if filepath else ""
                norm_key = os.path.normpath(key) if key else ""

                is_crit = False
                is_warn = False

                if norm_fp and norm_fp in self.critical_error_paths:
                    is_crit = True
                elif norm_key and norm_key in self.critical_error_paths:
                    is_crit = True
                elif any(
                    p
                    and (
                        p == norm_fp
                        or p == norm_key
                        or p.endswith("/" + norm_key)
                        or p.endswith("\\" + norm_key)
                    )
                    for p in self.critical_error_paths
                ):
                    is_crit = True

                if not is_crit:
                    if norm_fp and norm_fp in self.warning_paths:
                        is_warn = True
                    elif norm_key and norm_key in self.warning_paths:
                        is_warn = True
                    elif any(
                        p
                        and (
                            p == norm_fp
                            or p == norm_key
                            or p.endswith("/" + norm_key)
                            or p.endswith("\\" + norm_key)
                        )
                        for p in self.warning_paths
                    ):
                        is_warn = True

                if isinstance(node.label, Text):
                    plain_txt = node.label.plain
                    new_txt = Text(plain_txt)
                    if is_crit:
                        new_txt.stylize("bold red")
                    elif is_warn:
                        new_txt.stylize("bold yellow")
                    node.label = new_txt

                for child in node.children:
                    _stylize_node(child)

            _stylize_node(tree.root)
        except Exception as e:
            logger.debug(f"Error applying tree node styles: {e}")

    def _has_file_nodes(self, plan_dict: Dict[str, Any]) -> bool:
        """Check if plan structure contains any document file nodes."""
        if not plan_dict or not isinstance(plan_dict, dict):
            return False
        for k, v in plan_dict.items():
            if isinstance(v, dict):
                if v.get("__type__") == "file":
                    return True
                if self._has_file_nodes(v):
                    return True
            elif isinstance(v, list) and len(v) > 0:
                return True
            elif isinstance(v, str) and k != "action":
                return True
        return False

    def _get_current_search_query(self) -> str:
        """Return lowercased search query string if search input exists and contains text."""
        try:
            search_input = self.query_one("#tree-search-input", Input)
            return search_input.value.strip().lower()
        except Exception:
            return ""

    def _build_matching_node_ids(
        self, plan_dict: Dict[str, Any], query: str, current_folder: str = ""
    ) -> Set[int]:
        """Perform a single O(N) bottom-up pass returning a set of dictionary object IDs matching the query."""
        matching_ids: Set[int] = set()
        if not query or not isinstance(plan_dict, dict):
            return matching_ids

        def _walk(d: Dict[str, Any], folder: str) -> bool:
            has_match = False
            for k, v in d.items():
                if isinstance(v, dict):
                    if v.get("__type__") == "file":
                        filepath = str(
                            v.get(
                                "filepath",
                                (Path(self.base_dir) / folder / k).as_posix()
                                if self.base_dir
                                else (Path(folder) / k).as_posix(),
                            )
                        )
                        target_filename = str(v.get("target_filename", ""))
                        if (
                            query in k.lower()
                            or query in filepath.lower()
                            or query in target_filename.lower()
                        ):
                            matching_ids.add(id(v))
                            has_match = True
                    else:
                        sub_folder = (Path(folder) / k).as_posix() if folder else k
                        folder_direct_match = (
                            query in k.lower() or query in sub_folder.lower()
                        )
                        child_match = _walk(v, sub_folder)
                        if folder_direct_match or child_match:
                            matching_ids.add(id(v))
                            has_match = True
            return has_match

        _walk(plan_dict, current_folder)
        return matching_ids

    def render_tree(self, search_query: Optional[str] = None) -> None:
        """Render Textual Tree widget from in-memory plan structure without running validation worker."""
        try:
            tree = self.query_one("#plan-tree", Tree)
            if search_query is None:
                search_query = self._get_current_search_query()

            query = search_query.strip().lower() if search_query else ""

            self._capture_tree_state(tree, query)

            tree.reset("Proposed Organization Plan")

            if not self._has_file_nodes(self.plan):
                tree.root.add_leaf(
                    "💡 Empty Workspace: No documents found in target directory",
                    data={"is_empty_state": True, "action": "info"},
                )
                tree.root.add_leaf(
                    "▶ Press [G] to Load Sample Dataset",
                    data={
                        "is_empty_state": True,
                        "action": "generate_sample_corpus",
                        "shortcut": "G",
                    },
                )
                tree.root.add_leaf(
                    "▶ Press [Ctrl+B] to Select Different Directory",
                    data={
                        "is_empty_state": True,
                        "action": "select_dir",
                        "shortcut": "Ctrl+B",
                    },
                )
            else:
                matching_ids = (
                    self._build_matching_node_ids(self.plan, query) if query else None
                )
                nodes_added = self._build_tree_nodes(
                    self.plan,
                    tree.root,
                    current_folder="",
                    query=query,
                    matching_ids=matching_ids,
                )
                if query and nodes_added == 0:
                    tree.root.add_leaf(
                        f"🔍 No matching tree nodes found for '{search_query}'",
                        data={"is_empty_state": True, "action": "info"},
                    )
            tree.root.expand_all()
            self._restore_tree_state(tree, query)
            if tree.cursor_node and tree.cursor_node.data:
                self._update_inspector(tree.cursor_node, announce=False)
            elif getattr(self, "active_tree_node", None) and self.active_tree_node.data:
                self._update_inspector(self.active_tree_node, announce=False)
            else:
                self._update_inspector(None, announce=False)
        except Exception as e:
            logger.error(f"Error rendering tree: {e}")

    def rebuild_tree(
        self, search_query: Optional[str] = None, run_validation: bool = True
    ) -> None:
        """Rebuild Textual Tree widget from in-memory plan structure and optionally validate plan integrity."""
        self.render_tree(search_query=search_query)
        if run_validation:
            self.run_validation_worker()

    def _collect_file_nodes_from_dict(self, d: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Collect all file info dicts nested within a plan directory subtree."""
        files = []
        if not isinstance(d, dict):
            return files
        for val in d.values():
            if isinstance(val, dict):
                if val.get("__type__") == "file":
                    files.append(val)
                else:
                    files.extend(self._collect_file_nodes_from_dict(val))
        return files

    def _collect_visible_file_nodes_from_dict(
        self,
        node_dict: Dict[str, Any],
        current_folder: str = "",
        query: str = "",
        parent_folder_matches: bool = False,
        matching_ids: Optional[Set[int]] = None,
    ) -> List[Dict[str, Any]]:
        """Collect file info dicts for nodes visible under the active search query filter."""
        files = []
        if not isinstance(node_dict, dict):
            return files

        if not query:
            return self._collect_file_nodes_from_dict(node_dict)

        if matching_ids is None and query:
            matching_ids = self._build_matching_node_ids(
                node_dict, query, current_folder=current_folder
            )

        for k, v in sorted(node_dict.items()):
            if isinstance(v, dict) and v.get("__type__") == "file":
                if parent_folder_matches or (
                    matching_ids is not None and id(v) in matching_ids
                ):
                    files.append(v)
            elif isinstance(v, dict):
                sub_folder = (
                    (Path(current_folder) / k).as_posix() if current_folder else k
                )
                folder_matches = query in k.lower() or query in sub_folder.lower()
                if (
                    parent_folder_matches
                    or folder_matches
                    or (matching_ids is not None and id(v) in matching_ids)
                ):
                    files.extend(
                        self._collect_visible_file_nodes_from_dict(
                            v,
                            current_folder=sub_folder,
                            query=query,
                            parent_folder_matches=parent_folder_matches
                            or folder_matches,
                            matching_ids=matching_ids,
                        )
                    )
        return files

    def _node_matches_query(
        self, key: str, val: Dict[str, Any], current_folder: str, query: str
    ) -> bool:
        """Check if a node (file or directory) or any of its descendants match the search query."""
        if not query:
            return True
        matching_ids = self._build_matching_node_ids(
            {key: val}, query, current_folder=current_folder
        )
        return id(val) in matching_ids

    def _build_tree_nodes(
        self,
        node_dict: Dict[str, Any],
        parent_item: TreeNode,
        current_folder: str,
        query: str = "",
        parent_folder_matches: bool = False,
        matching_ids: Optional[Set[int]] = None,
    ) -> int:
        """Recursively build tree nodes, filtering by search query if specified. Returns count of nodes added."""
        added_count = 0
        if query and matching_ids is None:
            matching_ids = self._build_matching_node_ids(
                node_dict, query, current_folder=current_folder
            )

        for k, v in sorted(
            node_dict.items(),
            key=lambda x: (
                1 if isinstance(x[1], dict) and x[1].get("__type__") == "file" else 0,
                x[0],
            ),
        ):
            if isinstance(v, dict) and v.get("__type__") == "file":
                file_key = k
                file_info = v
                filepath = file_info.get(
                    "filepath",
                    (Path(self.base_dir) / current_folder / file_key).as_posix()
                    if self.base_dir
                    else (Path(current_folder) / file_key).as_posix(),
                )
                target_filename = file_info.get("target_filename", "")

                if query and not parent_folder_matches:
                    file_matches = matching_ids is not None and id(v) in matching_ids
                    if not file_matches:
                        continue

                is_locked = (
                    file_key in self.locked_files
                    or filepath in self.locked_files
                    or file_info.get("is_locked")
                )
                rating = self._ratings_cache.get(filepath) or file_info.get("rating")

                invalid_locks = getattr(self.plan, "invalid_locks", {}) or {}
                if not invalid_locks and isinstance(self.plan, dict):
                    invalid_locks = self.plan.get("invalid_locks", {}) or {}

                is_invalid_lock = (
                    filepath in invalid_locks
                    or file_key in invalid_locks
                    or (bool(filepath) and filepath.replace("\\", "/") in invalid_locks)
                    or (bool(filepath) and os.path.normpath(filepath) in invalid_locks)
                )

                is_excluded = file_info.get("is_excluded", False)
                if "is_checked" not in file_info:
                    is_checked = not is_excluded
                else:
                    is_checked = file_info.get("is_checked", True)
                if is_excluded:
                    is_checked = False
                file_info["is_checked"] = is_checked
                file_info["is_excluded"] = is_excluded

                cb_str = "[x]" if is_checked else "[ ]"
                label_parts = [cb_str]
                if is_invalid_lock:
                    label_parts.append("[LOCK WARNING]")
                elif is_locked:
                    label_parts.append("[LOCKED]")
                if is_excluded:
                    label_parts.append("[EXCLUDED]")

                source_root = file_info.get("source_root")
                if source_root:
                    root_name = os.path.basename(source_root) or source_root
                    label_parts.append(f"[ROOT: {root_name}]")

                if rating == "positive":
                    label_parts.append("[+]")
                elif rating == "negative":
                    label_parts.append("[-]")

                routed_by = file_info.get("routed_by")
                if routed_by == "jev_classifier" or file_info.get("is_jev"):
                    label_parts.append("[JEV]")

                sens_rating = file_info.get("sensitivity_rating")
                if sens_rating:
                    label_parts.append(f"[SENS: {str(sens_rating).upper()}]")

                arch_prio = file_info.get("archival_priority")
                if arch_prio is not None:
                    arch_str = (
                        f"P{arch_prio}"
                        if isinstance(arch_prio, int)
                        or (
                            isinstance(arch_prio, str)
                            and not str(arch_prio).upper().startswith("P")
                        )
                        else str(arch_prio).upper()
                    )
                    label_parts.append(f"[ARCH: {arch_str}]")

                target_filename = file_info.get("target_filename") or file_key
                target_path = None
                if self.base_dir:
                    target_path = os.path.normpath(
                        os.path.join(self.base_dir, current_folder, target_filename)
                    )
                elif current_folder:
                    target_path = os.path.normpath(
                        os.path.join(current_folder, target_filename)
                    )

                is_collision = (
                    file_info.get("collision")
                    or file_info.get("has_collision")
                    or False
                )
                if (
                    not is_collision
                    and target_path
                    and filepath
                    and os.path.lexists(target_path)
                ):
                    try:
                        is_same = os.path.samefile(target_path, filepath)
                    except Exception:
                        is_same = os.path.normpath(
                            os.path.abspath(target_path)
                        ) == os.path.normpath(os.path.abspath(filepath))
                    if not is_same:
                        is_collision = True

                pol = str(getattr(self.settings, "CONFLICT_POLICY", "rename")).lower()
                if is_collision:
                    label_parts.append(f"[COLLISION: {pol.upper()}]")

                label_parts.append(file_key)
                if target_filename and target_filename != file_key:
                    label_parts.append(f"-> {target_filename}")

                norm_fp = os.path.normpath(filepath) if filepath else ""
                norm_key = os.path.normpath(file_key) if file_key else ""

                is_crit_node = (
                    norm_fp in self.critical_error_paths
                    or norm_key in self.critical_error_paths
                    or any(
                        p
                        and (
                            p == norm_fp
                            or p == norm_key
                            or p.endswith("/" + norm_key)
                            or p.endswith("\\" + norm_key)
                        )
                        for p in self.critical_error_paths
                    )
                )
                is_warn_node = (
                    norm_fp in self.warning_paths
                    or norm_key in self.warning_paths
                    or any(
                        p
                        and (
                            p == norm_fp
                            or p == norm_key
                            or p.endswith("/" + norm_key)
                            or p.endswith("\\" + norm_key)
                        )
                        for p in self.warning_paths
                    )
                )

                label_str = cb_str + " 📄 " + " ".join(label_parts[1:])
                text_obj = Text(label_str)
                if is_crit_node:
                    text_obj.stylize("bold red")
                elif is_warn_node:
                    text_obj.stylize("bold yellow")

                node_data = {
                    "is_file": True,
                    "key": file_key,
                    "filepath": filepath,
                    "folder": current_folder,
                    "info": file_info,
                    "is_locked": is_locked,
                    "rating": rating,
                    "is_checked": is_checked,
                    "is_excluded": is_excluded,
                    "is_collision": is_collision,
                    "conflict_policy": pol,
                }
                parent_item.add_leaf(text_obj, data=node_data)
                added_count += 1
            elif isinstance(v, dict):
                sub_folder = (
                    (Path(current_folder) / k).as_posix() if current_folder else k
                )
                folder_matches = (
                    (query in k.lower() or query in sub_folder.lower())
                    if query
                    else True
                )

                if query and not (
                    parent_folder_matches
                    or folder_matches
                    or (matching_ids is not None and id(v) in matching_ids)
                ):
                    continue

                node_data = {
                    "is_file": False,
                    "key": k,
                    "folder": sub_folder,
                    "info": v,
                }
                child_files = self._collect_file_nodes_from_dict(v)
                if child_files:
                    checked_count = sum(
                        1
                        for f in child_files
                        if f.get("is_checked", True) and not f.get("is_excluded", False)
                    )
                    if checked_count == len(child_files):
                        folder_cb = "[x] "
                    elif checked_count > 0:
                        folder_cb = "[-] "
                    else:
                        folder_cb = "[ ] "
                else:
                    folder_cb = ""

                child_tree_node = parent_item.add(
                    Text(f"{folder_cb}📁 {k}"), data=node_data, expand=True
                )
                child_added = self._build_tree_nodes(
                    v,
                    child_tree_node,
                    current_folder=sub_folder,
                    query=query,
                    parent_folder_matches=parent_folder_matches or folder_matches,
                    matching_ids=matching_ids,
                )
                added_count += 1 + child_added

        return added_count

    def _update_inspector(self, node_or_data: Any = None, announce: bool = True) -> str:
        """Update and return formatted metadata text for the inspector panel and path comparison pane."""
        if hasattr(self, "_update_onboarding_cta_visibility"):
            try:
                self._update_onboarding_cta_visibility()
            except Exception:
                pass

        if node_or_data is None:
            node_or_data = self.active_tree_node

        data = None
        if hasattr(node_or_data, "data"):
            data = getattr(node_or_data, "data", None)
        elif isinstance(node_or_data, dict):
            data = node_or_data

        path_comp_widget = None
        try:
            path_comp_widget = self.query_one("#path-comparison", Static)
        except Exception:
            pass

        if not data or not isinstance(data, dict):
            msg = (
                "Root folder of sorting plan."
                if getattr(self, "plan", None)
                else self._start_hint()
            )
            try:
                meta_widget = self.query_one("#meta-details", Static)
                meta_widget.update(msg)
            except Exception:
                pass
            if path_comp_widget:
                all_files = (
                    self._collect_file_nodes_from_dict(self.plan)
                    if hasattr(self, "plan") and self.plan
                    else []
                )
                if all_files:
                    path_comp_widget.update(
                        format_folder_path_comparison(all_files, self.base_dir)
                    )
                else:
                    path_comp_widget.update(
                        "Select a file or folder in the tree to compare source and target paths."
                    )
            return msg

        if data.get("is_empty_state"):
            action = data.get("action")
            if action == "generate_sample_corpus":
                msg = "Empty workspace node. Press [G] to create a sample dataset to try sorting on."
            elif action == "select_dir":
                msg = "Empty workspace node. Press [Ctrl+B] to select a different directory."
            else:
                msg = "Empty workspace node. No documents found in target directory."
            if announce:
                self.announce(msg)
            try:
                meta_widget = self.query_one("#meta-details", Static)
                meta_widget.update(msg)
            except Exception:
                pass
            if path_comp_widget:
                path_comp_widget.update("No documents available for path comparison.")
            return msg

        lines = []
        if data.get("is_file"):
            key = data.get("key", "")
            folder = data.get("folder") or "(Root)"
            locked = "Yes [LOCKED]" if data.get("is_locked") else "No"
            rating = data.get("rating") or "None"
            info = data.get("info") if isinstance(data.get("info"), dict) else {}

            src_filepath = data.get("filepath") or info.get("filepath") or key
            target_fn = info.get("target_filename") or key
            target_folder = folder if folder != "(Root)" else ""
            if self.base_dir and target_folder:
                target_filepath = os.path.join(self.base_dir, target_folder, target_fn)
            elif self.base_dir:
                target_filepath = os.path.join(self.base_dir, target_fn)
            else:
                target_filepath = (
                    os.path.join(target_folder, target_fn)
                    if target_folder
                    else target_fn
                )

            if path_comp_widget:
                table = format_side_by_side_path_comparison(
                    source_path=src_filepath,
                    target_path=target_filepath,
                    base_dir=self.base_dir,
                )
                path_comp_widget.update(table)

            lines.append(f"[bold accent]File:[/bold accent] {key}")
            lines.append(f"[bold]Path:[/bold] {data.get('filepath')}")
            lines.append(f"[bold]Target Folder:[/bold] {folder}")

            target_fn = info.get("target_filename")
            if target_fn:
                lines.append(f"[bold]Target Filename:[/bold] {target_fn}")

            lines.append(f"[bold]Locked:[/bold] {locked}")
            lines.append(f"[bold]ML Rating:[/bold] {rating}")

            invalid_locks = getattr(self.plan, "invalid_locks", {}) or {}
            if not invalid_locks and isinstance(self.plan, dict):
                invalid_locks = self.plan.get("invalid_locks", {}) or {}
            filepath_str = data.get("filepath", "")
            key_str = data.get("key", "")
            lock_err = (
                invalid_locks.get(filepath_str)
                or invalid_locks.get(key_str)
                or (
                    invalid_locks.get(filepath_str.replace("\\", "/"))
                    if filepath_str
                    else None
                )
                or (
                    invalid_locks.get(os.path.normpath(filepath_str))
                    if filepath_str
                    else None
                )
            )
            if lock_err:
                lines.append(f"[bold error]Lock Warning:[/bold error] {lock_err}")

            routing_src = info.get("routed_by")
            if routing_src:
                lines.append(f"[bold]Routing Source:[/bold] {routing_src}")

            cat = info.get("category") or info.get("jev_category")
            if cat:
                lines.append(f"[bold]Category:[/bold] {cat}")

            sens_rating = info.get("sensitivity_rating")
            if sens_rating is not None:
                lines.append(f"[bold]Sensitivity Rating:[/bold] {sens_rating}")

            sens_score = info.get("sensitivity_score")
            if sens_score is not None:
                try:
                    lines.append(
                        f"[bold]Sensitivity Score:[/bold] {float(sens_score):.2f}"
                    )
                except (ValueError, TypeError):
                    lines.append(f"[bold]Sensitivity Score:[/bold] {sens_score}")

            arch_prio = info.get("archival_priority")
            if arch_prio is not None:
                arch_str = (
                    f"P{arch_prio}"
                    if isinstance(arch_prio, int)
                    or (
                        isinstance(arch_prio, str)
                        and not str(arch_prio).upper().startswith("P")
                    )
                    else str(arch_prio).upper()
                )
                lines.append(f"[bold]Archival Priority:[/bold] {arch_str}")

            arch_score = info.get("archival_priority_score")
            if arch_score is not None:
                try:
                    lines.append(
                        f"[bold]Archival Priority Score:[/bold] {float(arch_score):.2f}"
                    )
                except (ValueError, TypeError):
                    lines.append(f"[bold]Archival Priority Score:[/bold] {arch_score}")

            conf = info.get("confidence")
            if conf is not None:
                try:
                    lines.append(f"[bold]Confidence:[/bold] {float(conf):.2%}")
                except (ValueError, TypeError):
                    lines.append(f"[bold]Confidence:[/bold] {conf}")

            if data.get("is_collision"):
                pol_str = str(
                    data.get("conflict_policy")
                    or getattr(self.settings, "CONFLICT_POLICY", "rename")
                ).upper()
                lines.append(
                    f"[bold warning]Collision Indicator:[/bold warning] [COLLISION: {pol_str}]"
                )

            if announce:
                self.announce(
                    f"Selected file '{key}' in folder '{folder}'. Locked: {locked}. Rating: {rating}."
                )
        else:
            key = data.get("key", "")
            folder = data.get("folder", "")
            folder_info = data.get("info", {})
            child_files = (
                self._collect_file_nodes_from_dict(folder_info)
                if isinstance(folder_info, dict)
                else []
            )

            if path_comp_widget:
                if child_files:
                    table = format_folder_path_comparison(
                        files_info=child_files,
                        base_dir=self.base_dir,
                    )
                    path_comp_widget.update(table)
                else:
                    path_comp_widget.update(
                        f"Folder category '{key}' contains no document items."
                    )

            lines.append(f"[bold accent]Folder Category:[/bold accent] {key}")
            lines.append(f"[bold]Relative Path:[/bold] {folder}")
            if announce:
                self.announce(f"Selected folder category '{key}' at path '{folder}'.")

        text = "\n".join(lines)
        try:
            meta_widget = self.query_one("#meta-details", Static)
            meta_widget.update(text)
        except Exception:
            pass

        return text
