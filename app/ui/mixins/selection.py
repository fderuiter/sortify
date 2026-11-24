"""Plan tree selection, include/exclude, reassign, rename, lock and rating actions."""

import os
import re
from typing import Any, Optional

from textual.widgets.tree import TreeNode

from app.ui.modals.edit import (
    NewFolderModal,
    ReassignModal,
    RenameModal,
    RenameResult,
)
from app.ui.protocols import TUIHostProtocol


def _resolve_plan_folder_dict(
    plan: dict[str, Any], folder_path: str, *, create: bool = False
) -> Optional[dict[str, Any]]:
    """Resolve a flat or slash-delimited nested folder dictionary inside ``plan``."""
    if not folder_path:
        return plan
    direct = plan.get(folder_path)
    if isinstance(direct, dict) and direct.get("__type__") != "file":
        return direct

    from app.core.path_utils import _is_absolute_path

    if _is_absolute_path(folder_path):
        if create:
            created: dict[str, Any] = {}
            plan[folder_path] = created
            return created
        return None

    parts = [p for p in folder_path.replace("\\", "/").strip("/").split("/") if p]
    if not parts:
        return plan

    curr: dict[str, Any] = plan
    for part in parts:
        nxt = curr.get(part)
        if isinstance(nxt, dict) and nxt.get("__type__") != "file":
            curr = nxt
        elif create and nxt is None:
            new_sub: dict[str, Any] = {}
            curr[part] = new_sub
            curr = new_sub
        else:
            return None
    return curr


def _resolve_source_disk_path(
    base_dir: Optional[str],
    key_val: str,
    folder_val: str,
    info_dict: dict[str, Any],
    explicit_fp: Optional[str] = None,
) -> Optional[str]:
    """Locate the physical source file on disk before or after moves execute."""
    src_root = str(info_dict.get("source_root") or base_dir or "")
    candidates: list[str] = []

    info_fp = info_dict.get("filepath")
    if isinstance(info_fp, str) and info_fp.strip():
        candidates.append(info_fp.strip())

    src_rel = info_dict.get("source_path")
    if isinstance(src_rel, str) and src_rel.strip():
        eff_root = src_root
        clean_rel = src_rel.strip()
        if "::" in clean_rel:
            eff_root, clean_rel = clean_rel.split("::", 1)
        candidates.append(
            os.path.join(eff_root, clean_rel)
            if eff_root and not os.path.isabs(clean_rel)
            else clean_rel
        )

    rel_src = info_dict.get("relative_source")
    if isinstance(rel_src, str) and rel_src.strip():
        eff_root = src_root
        clean_rs = rel_src.strip()
        if "::" in clean_rs:
            eff_root, clean_rs = clean_rs.split("::", 1)
        if eff_root:
            if folder_val:
                candidates.append(
                    os.path.normpath(os.path.join(eff_root, folder_val, clean_rs))
                )
            candidates.append(os.path.normpath(os.path.join(eff_root, clean_rs)))
        else:
            candidates.append(clean_rs)

    if key_val:
        eff_root = src_root
        clean_key = key_val
        if "::" in clean_key:
            eff_root, clean_key = clean_key.split("::", 1)
        if eff_root and not os.path.isabs(clean_key):
            candidates.append(os.path.join(eff_root, clean_key))
        else:
            candidates.append(clean_key)

    if isinstance(explicit_fp, str) and explicit_fp.strip():
        candidates.append(explicit_fp.strip())

    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    return candidates[0] if candidates else None


def _find_file_in_plan(
    plan: dict[str, Any],
    file_key: str,
    target_info: Any = None,
    current_folder: str = "",
) -> tuple[str, Optional[dict[str, Any]], Any]:
    """Recursively locate ``file_key`` (or ``target_info``) inside a nested ``plan``."""
    for k, v in plan.items():
        if isinstance(v, dict) and v.get("__type__") == "file":
            if (target_info is not None and v is target_info) or (
                bool(file_key) and k == file_key
            ):
                return current_folder, plan, v
        elif isinstance(v, dict):
            sub_folder = f"{current_folder}/{k}" if current_folder else k
            found_folder, found_container, found_val = _find_file_in_plan(
                v, file_key, target_info=target_info, current_folder=sub_folder
            )
            if found_container is not None:
                return found_folder, found_container, found_val
    return "", None, None


def _find_key_for_info(plan: dict[str, Any], target_info: Any) -> tuple[str, str]:
    """Return ``(folder_path, key)`` for ``target_info`` object identity inside ``plan``."""
    for k, v in plan.items():
        if v is target_info:
            return "", k
        if isinstance(v, dict) and v.get("__type__") != "file":
            sub_folder, sub_key = _find_key_for_info(v, target_info)
            if sub_key:
                full_folder = f"{k}/{sub_folder}" if sub_folder else k
                return full_folder, sub_key
    return "", ""


def _collect_plan_folders(plan: dict[str, Any], prefix: str = "") -> list[str]:
    """Recursively collect slash-delimited folder paths present in ``plan``."""
    folders: list[str] = []
    for k, v in plan.items():
        if isinstance(v, dict) and v.get("__type__") != "file":
            full = f"{prefix}/{k}" if prefix else k
            folders.append(full)
            folders.extend(_collect_plan_folders(v, full))
    return folders


def _make_manual_edit_key(db_base_dir: str, doc_relpath: str) -> str:
    """Build canonical lookup key for ``_manual_plan_edits``."""
    norm_rel = doc_relpath.replace("\\", "/").lstrip("/")
    if db_base_dir:
        norm_base = os.path.normcase(os.path.normpath(os.path.abspath(db_base_dir)))
        return f"{norm_base}::{norm_rel}"
    return norm_rel


def _resolve_source_identity(
    host: Any,
    file_key: Optional[str] = None,
    filepath: Optional[str] = None,
    info: Optional[dict[str, Any]] = None,
) -> tuple[str, str, str]:
    """Resolve ``(db_base_dir, doc_relpath, abs_filepath)`` for a plan file node."""
    from app.core.path_utils import safe_relpath

    info_dict = info if isinstance(info, dict) else {}
    host_base = str(getattr(host, "base_dir", "") or "")

    db_base_dir = str(info_dict.get("source_root") or "")
    if not db_base_dir and isinstance(file_key, str) and "::" in file_key:
        db_base_dir = file_key.split("::", 1)[0]
    if (
        not db_base_dir
        and isinstance(info_dict.get("source_path"), str)
        and "::" in str(info_dict["source_path"])
    ):
        db_base_dir = str(info_dict["source_path"]).split("::", 1)[0]
    if not db_base_dir:
        db_base_dir = host_base

    raw_src: Any = (
        info_dict.get("source_path")
        or info_dict.get("filepath")
        or filepath
        or file_key
        or ""
    )
    raw_src_str = str(raw_src)
    if "::" in raw_src_str:
        parsed_root, raw_src_str = raw_src_str.split("::", 1)
        if not info_dict.get("source_root") and parsed_root:
            db_base_dir = parsed_root

    if not raw_src_str:
        return db_base_dir, "", ""

    if os.path.isabs(raw_src_str):
        abs_fp = os.path.normpath(raw_src_str)
        if db_base_dir:
            rel = safe_relpath(abs_fp, db_base_dir).replace("\\", "/")
            if not rel.startswith(".."):
                return db_base_dir, rel, abs_fp
        if host_base:
            rel_host = safe_relpath(abs_fp, host_base).replace("\\", "/")
            if not rel_host.startswith(".."):
                return host_base, rel_host, abs_fp
        return db_base_dir, os.path.basename(abs_fp), abs_fp

    doc_relpath = raw_src_str.replace("\\", "/").lstrip("/")
    abs_fp = (
        os.path.normpath(os.path.join(db_base_dir, doc_relpath))
        if db_base_dir
        else doc_relpath
    )
    return db_base_dir, doc_relpath, abs_fp


def _record_manual_edit(
    host: Any,
    db_base_dir: str,
    doc_relpath: str,
    abs_filepath: str = "",
    **updates: Any,
) -> None:
    """Persist manual node edits in ``host._manual_plan_edits`` keyed by source ``doc_relpath``."""
    if not doc_relpath:
        return
    edits_map = getattr(host, "_manual_plan_edits", None)
    if not isinstance(edits_map, dict):
        return
    key = _make_manual_edit_key(db_base_dir, doc_relpath)
    entry = edits_map.setdefault(
        key,
        {
            "db_base_dir": db_base_dir,
            "doc_relpath": doc_relpath,
            "abs_filepath": abs_filepath,
        },
    )
    if abs_filepath and not entry.get("abs_filepath"):
        entry["abs_filepath"] = abs_filepath
    for k, v in updates.items():
        if v is None and k in ("folder", "target_filename"):
            entry.pop(k, None)
        else:
            entry[k] = v


def _derive_learned_pattern(raw_name: str) -> Optional[str]:
    """Derive a normalized lowercase keyword pattern from a reassigned filename."""
    if not raw_name:
        return None
    clean_raw = raw_name.split("::", 1)[1] if "::" in raw_name else raw_name
    base = os.path.basename(clean_raw.replace("\\", "/"))
    stem = os.path.splitext(base)[0].lower()
    cleaned = re.sub(r"^[\s_\-\.()\d]+|[\s_\-\.()\d]+$", "", stem).strip(" _-.")
    if len(cleaned) >= 3:
        return cleaned
    fallback = stem.strip(" _-.")
    return fallback if len(fallback) >= 2 else None


class SelectionActionsMixin:
    """Key-bound actions that select, edit and rate nodes in the plan tree."""

    def register_visual_selected_node(
        self: TUIHostProtocol, node: TreeNode[Any]
    ) -> None:
        """Register node into visual range selection list when visual selection is active."""
        if not self.visual_select_active or not node:
            return
        if node not in self.visual_selected_nodes:
            self.visual_selected_nodes.append(node)

    def action_visual_range_select(self: TUIHostProtocol) -> None:
        """Toggle visual range selection mode [V]."""
        if self._is_text_control_focused():
            return
        self.visual_select_active = not self.visual_select_active
        if self.visual_select_active:
            self.visual_selected_nodes = []
            node = self._get_active_node()
            if node:
                self.visual_selected_nodes.append(node)
            self.announce(
                "Visual range selection mode enabled. Move cursor [j/k] to extend range."
            )
        else:
            self.announce("Visual range selection mode disabled.")

    def action_toggle_selection(self: TUIHostProtocol) -> None:
        """Toggle checkbox selection for active node, folder group, or visual range [Space]."""
        if self._is_text_control_focused():
            return

        nodes_to_toggle = []
        if self.visual_select_active and self.visual_selected_nodes:
            nodes_to_toggle = list(self.visual_selected_nodes)
            self.visual_select_active = False
            self.visual_selected_nodes = []
        else:
            active_node = self._get_active_node()
            if active_node:
                nodes_to_toggle = [active_node]

        if not nodes_to_toggle:
            self.announce("Select a tree node to toggle checkbox [Space].")
            return

        file_infos_to_toggle: list[dict[str, Any]] = []
        for n in nodes_to_toggle:
            data = getattr(n, "data", None)
            if not data or not isinstance(data, dict):
                continue
            if data.get("is_file"):
                file_infos_to_toggle.append(data["info"])
            else:
                info_dict = data.get("info", {})
                file_infos_to_toggle.extend(
                    self._collect_file_nodes_from_dict(info_dict)
                )

        if not file_infos_to_toggle:
            self.announce("No file nodes found to toggle.")
            return

        all_checked = all(
            fi.get("is_checked", True) and not fi.get("is_excluded", False)
            for fi in file_infos_to_toggle
        )
        new_checked = not all_checked

        for fi in file_infos_to_toggle:
            fi["is_checked"] = new_checked
            if new_checked:
                fi["is_excluded"] = False
            _, found_k = _find_key_for_info(self.plan, fi)
            db_b, doc_rel, abs_fp = _resolve_source_identity(
                self, file_key=found_k, filepath=fi.get("filepath"), info=fi
            )
            _record_manual_edit(
                self,
                db_b,
                doc_rel,
                abs_fp,
                is_checked=fi["is_checked"],
                is_excluded=bool(fi.get("is_excluded", False)),
            )

        self.rebuild_tree()
        status_str = "checked [x]" if new_checked else "unchecked [ ]"
        self.announce(
            f"Toggled {len(file_infos_to_toggle)} document item(s) to {status_str}."
        )

    def _get_target_file_infos(self: TUIHostProtocol) -> list[dict[str, Any]]:
        """Return checked file info dicts, or active/focused node file infos if none checked."""
        all_files = self._collect_file_nodes_from_dict(self.plan)
        checked_files = [
            f
            for f in all_files
            if f.get("is_checked", True) and not f.get("is_excluded", False)
        ]
        if checked_files:
            return checked_files

        active_node = self._get_active_node()
        if not active_node:
            return all_files

        data = getattr(active_node, "data", None)
        if not data or not isinstance(data, dict):
            return all_files

        if data.get("is_file"):
            return [data["info"]]
        else:
            folder_files = self._collect_file_nodes_from_dict(data.get("info", {}))
            return folder_files if folder_files else all_files

    def action_exclude_selected(self: TUIHostProtocol) -> None:
        """Bulk exclude checked items or active node [X]."""
        if self._is_text_control_focused():
            return

        target_files = self._get_target_file_infos()
        if not target_files:
            self.announce("No file items available to exclude.")
            return

        for fi in target_files:
            fi["is_checked"] = False
            fi["is_excluded"] = True
            _, found_k = _find_key_for_info(self.plan, fi)
            db_b, doc_rel, abs_fp = _resolve_source_identity(
                self, file_key=found_k, filepath=fi.get("filepath"), info=fi
            )
            _record_manual_edit(
                self,
                db_b,
                doc_rel,
                abs_fp,
                is_checked=False,
                is_excluded=True,
            )

        self.rebuild_tree()
        self.announce(f"Excluded {len(target_files)} file(s) from sorting plan.")

    def action_include_selected(self: TUIHostProtocol) -> None:
        """Bulk include selected items or active node [I]."""
        if self._is_text_control_focused():
            return

        target_files = self._get_target_file_infos()
        if not target_files:
            self.announce("No file items available to include.")
            return

        for fi in target_files:
            fi["is_checked"] = True
            fi["is_excluded"] = False
            _, found_k = _find_key_for_info(self.plan, fi)
            db_b, doc_rel, abs_fp = _resolve_source_identity(
                self, file_key=found_k, filepath=fi.get("filepath"), info=fi
            )
            _record_manual_edit(
                self,
                db_b,
                doc_rel,
                abs_fp,
                is_checked=True,
                is_excluded=False,
            )

        self.rebuild_tree()
        self.announce(f"Included {len(target_files)} file(s) in sorting plan.")

    def action_reassign_selected(self: TUIHostProtocol) -> None:
        """Bulk reassign checked file items or active node to target category [R]."""
        if self._is_text_control_focused():
            return

        target_files = self._get_target_file_infos()
        if not target_files:
            self.announce("No file items available to reassign.")
            return

        from app.core.scanner import discover_existing_folders

        plan_folders = _collect_plan_folders(self.plan)
        disk_folders = discover_existing_folders(
            getattr(self, "base_dir", None),
            settings=getattr(self, "settings", None),
        )
        existing_folders: list[str] = []
        seen_folders: set[str] = set()
        for folder in plan_folders + disk_folders:
            if folder and folder.lower() not in seen_folders:
                seen_folders.add(folder.lower())
                existing_folders.append(folder)

        def on_reassigned(target_folder: Optional[str]) -> None:
            if not target_folder:
                return

            from app.core.path_utils import validate_target_path

            host: Any = self
            can_learn_rule = False
            try:
                validate_target_path(target_folder)
                can_learn_rule = True
            except ValueError:
                can_learn_rule = False

            settings_obj = getattr(host, "settings", None)
            learned_rules = (
                dict(getattr(settings_obj, "LEARNED_RULES", {}) or {})
                if settings_obj is not None
                else {}
            )
            learned_updated = False

            reassigned_count = 0
            for fi in target_files:
                found_folder, old_container, _ = _find_file_in_plan(
                    host.plan,
                    str(fi.get("target_filename") or fi.get("key") or ""),
                    target_info=fi,
                )
                file_key = None
                if old_container is not None:
                    for k, v in list(old_container.items()):
                        if v is fi:
                            file_key = k
                            break
                if not file_key:
                    file_key = fi.get("target_filename") or fi.get("key")
                old_folder = found_folder or fi.get("folder", "")
                if old_container is None and old_folder:
                    old_container = _resolve_plan_folder_dict(host.plan, old_folder)

                if file_key and old_container is not None:
                    db_base, doc_relpath, abs_fp = _resolve_source_identity(
                        host,
                        file_key=str(file_key),
                        filepath=fi.get("filepath"),
                        info=fi,
                    )
                    if doc_relpath and not fi.get("source_path"):
                        fi["source_path"] = doc_relpath
                    if abs_fp and not fi.get("filepath"):
                        fi["filepath"] = abs_fp

                    old_container.pop(file_key, None)
                    target_container = _resolve_plan_folder_dict(
                        host.plan, target_folder, create=True
                    )
                    if target_container is None:
                        host.plan[target_folder] = {}
                        target_container = host.plan[target_folder]

                    from app.core.path_utils import _disambiguate_key

                    # Non-clobbering mutation with automatic numeric disambiguation
                    if (
                        file_key in target_container
                        and target_container[file_key] is not fi
                    ):
                        dest_key = _disambiguate_key(
                            target_container, file_key, is_file=True
                        )
                        fi["target_filename"] = dest_key
                        fi["confirmed"] = True
                    else:
                        dest_key = file_key

                    target_container[dest_key] = fi
                    fi["folder"] = target_folder
                    fi["is_locked"] = True
                    host.locked_files[dest_key] = target_folder

                    if doc_relpath:
                        host.locked_files[doc_relpath] = target_folder
                        if host.app_session:
                            host.app_session.db.set_user_verified_target_path(
                                db_base, doc_relpath, target_folder
                            )
                        edit_kwargs: dict[str, Any] = {
                            "folder": target_folder,
                            "is_locked": True,
                        }
                        if fi.get("target_filename"):
                            edit_kwargs["target_filename"] = fi["target_filename"]
                            edit_kwargs["confirmed"] = True
                        _record_manual_edit(
                            host, db_base, doc_relpath, abs_fp, **edit_kwargs
                        )

                    if can_learn_rule and settings_obj is not None:
                        pattern = _derive_learned_pattern(
                            str(doc_relpath or fi.get("source_path") or file_key)
                        )
                        if pattern:
                            learned_rules[pattern] = target_folder
                            learned_updated = True

                    reassigned_count += 1

            if learned_updated and settings_obj is not None:
                settings_obj.LEARNED_RULES = learned_rules
                if hasattr(settings_obj, "flush"):
                    settings_obj.flush()

            host.rebuild_tree()
            host.announce(
                f"Reassigned {reassigned_count} file(s) to folder '{target_folder}'."
            )

        self.push_screen(
            ReassignModal(
                existing_folders,
                count=len(target_files),
                plan=self.plan,
                base_dir=self.base_dir,
                target_files=target_files,
                settings=getattr(self, "settings", None),
            ),
            on_reassigned,
        )

    def action_toggle_select_all(self: TUIHostProtocol) -> None:
        """Toggle Select All / Deselect All file nodes in plan [A]."""
        if self._is_text_control_focused():
            return

        all_files = self._collect_file_nodes_from_dict(self.plan)
        if not all_files:
            self.announce("No file items available to select/deselect.")
            return

        query = self._get_current_search_query()
        if query:
            target_files = self._collect_visible_file_nodes_from_dict(
                self.plan, query=query
            )
        else:
            target_files = all_files

        if not target_files:
            self.announce(
                f"No visible file items matching '{query}' to select/deselect."
            )
            return

        all_checked = all(
            f.get("is_checked", True) and not f.get("is_excluded", False)
            for f in target_files
        )
        new_state = not all_checked

        for f in target_files:
            f["is_checked"] = new_state
            if new_state:
                f["is_excluded"] = False
            _, found_k = _find_key_for_info(self.plan, f)
            db_b, doc_rel, abs_fp = _resolve_source_identity(
                self, file_key=found_k, filepath=f.get("filepath"), info=f
            )
            _record_manual_edit(
                self,
                db_b,
                doc_rel,
                abs_fp,
                is_checked=new_state,
                is_excluded=bool(f.get("is_excluded", False)),
            )

        self.rebuild_tree(run_validation=True)

        if query:
            hidden_count = len(all_files) - len(target_files)
            status_str = "Selected" if new_state else "Deselected"
            if hidden_count > 0:
                self.announce(
                    f"{status_str} {len(target_files)} visible file item(s) ({hidden_count} hidden files unchanged)."
                )
            else:
                self.announce(f"{status_str} {len(target_files)} visible file item(s).")
        else:
            status_str = "Selected all" if new_state else "Deselected all"
            self.announce(f"{status_str} {len(all_files)} file item(s).")

    def _get_executable_plan(
        self: TUIHostProtocol, plan_dict: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Return filtered plan dictionary containing only checked, non-excluded file nodes."""
        if plan_dict is None:
            plan_dict = self.plan

        result: dict[str, Any] = {}
        for k, v in plan_dict.items():
            if isinstance(v, dict) and v.get("__type__") == "file":
                is_checked = v.get("is_checked", True)
                is_excluded = v.get("is_excluded", False)
                if is_checked and not is_excluded:
                    result[k] = v
            elif isinstance(v, dict):
                sub = self._get_executable_plan(v)
                if sub:
                    result[k] = sub
            else:
                result[k] = v
        return result

    def action_toggle_lock(self: TUIHostProtocol) -> None:
        """Toggle node lock state [Ctrl+L]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return
        node = self._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            self.announce("Select a file node to toggle lock [Ctrl+L].")
            return

        data = node.data
        file_key = data["key"]
        filepath = data["filepath"]
        folder = data["folder"]
        info_dict = data.get("info") if isinstance(data.get("info"), dict) else None
        db_base, doc_relpath, abs_fp = _resolve_source_identity(
            self, file_key=file_key, filepath=filepath, info=info_dict
        )

        if data.get("is_locked"):
            self.locked_files.pop(file_key, None)
            self.locked_files.pop(filepath, None)
            if doc_relpath:
                self.locked_files.pop(doc_relpath, None)
            data["is_locked"] = False
            if isinstance(info_dict, dict):
                info_dict["is_locked"] = False
            if self.app_session and doc_relpath:
                self.app_session.db.set_user_verified_target_path(
                    db_base, doc_relpath, None
                )
            _record_manual_edit(
                self, db_base, doc_relpath, abs_fp, folder=None, is_locked=False
            )
            msg = f"Unlocked file '{file_key}'"
        else:
            self.locked_files[file_key] = folder
            if doc_relpath:
                self.locked_files[doc_relpath] = folder
            data["is_locked"] = True
            if isinstance(info_dict, dict):
                info_dict["is_locked"] = True
            if self.app_session and doc_relpath:
                self.app_session.db.set_user_verified_target_path(
                    db_base, doc_relpath, folder
                )
            _record_manual_edit(
                self, db_base, doc_relpath, abs_fp, folder=folder, is_locked=True
            )
            msg = f"Locked file '{file_key}' to folder '{folder}'"

        self.announce(msg)
        self.rebuild_tree()

    def _get_checked_file_keys(self: TUIHostProtocol) -> list[str]:
        """Collect keys of checked, non-excluded file nodes."""
        entries = self._get_checked_file_entries()
        return [e["key"] for e in entries if e.get("key")]

    def _get_checked_file_entries(self: TUIHostProtocol) -> list[dict[str, Any]]:
        """Collect full metadata entries for checked, non-excluded file nodes."""
        entries: list[dict[str, Any]] = []

        # 1. First attempt to gather from tree widget to preserve visual display order
        try:
            from textual.widgets import Tree

            from app.core.pattern_formatter import resolve_disk_file_date

            tree = self.query_one("#plan-tree", Tree)
            seen_ids = set()

            def _resolve_fp_and_date(
                key_val: str,
                folder_val: str,
                info_dict: dict[str, Any],
                explicit_fp: Optional[str] = None,
            ) -> tuple[Optional[str], str]:
                fp = _resolve_source_disk_path(
                    getattr(self, "base_dir", None),
                    key_val,
                    folder_val,
                    info_dict,
                    explicit_fp=explicit_fp,
                )
                f_date = str(
                    info_dict.get("date")
                    or info_dict.get("file_date")
                    or resolve_disk_file_date(
                        fp, base_dir=getattr(self, "base_dir", None)
                    )
                    or ""
                )
                return fp, f_date

            def _walk(node: TreeNode) -> None:
                data = getattr(node, "data", None)
                if data and isinstance(data, dict) and data.get("is_file"):
                    info = data.get("info", {})
                    if not isinstance(info, dict):
                        info = {}
                    is_checked = data.get("is_checked", True)
                    is_excluded = data.get("is_excluded", False)
                    if is_checked and not is_excluded:
                        info_id = id(info) if info else id(data)
                        if info_id not in seen_ids:
                            seen_ids.add(info_id)
                            entry_key = (
                                data.get("key") or info.get("target_filename") or ""
                            )
                            entry_folder = str(
                                data.get("folder") or info.get("folder", "")
                            )
                            resolved_fp, resolved_date = _resolve_fp_and_date(
                                str(entry_key),
                                entry_folder,
                                info,
                                data.get("filepath"),
                            )
                            entries.append(
                                {
                                    "key": entry_key,
                                    "folder": data.get("folder")
                                    or info.get("folder", ""),
                                    "info": info,
                                    "filepath": resolved_fp,
                                    "date": resolved_date,
                                    "file_date": resolved_date,
                                    "category": data.get("folder")
                                    or info.get("folder", ""),
                                }
                            )
                for child in node.children:
                    _walk(child)

            _walk(tree.root)
            if entries:
                return entries
        except Exception:
            pass

        # 2. Fall back to traversing self.plan dictionary
        if not hasattr(self, "plan") or not isinstance(self.plan, dict):
            return entries

        from app.core.pattern_formatter import resolve_disk_file_date

        seen_plan_ids = set()

        def _walk_plan(d: dict[str, Any], current_folder: str = "") -> None:
            for k, v in d.items():
                if isinstance(v, dict):
                    if v.get("__type__") == "file":
                        is_checked = v.get("is_checked", True)
                        is_excluded = v.get("is_excluded", False)
                        if is_checked and not is_excluded:
                            v_id = id(v)
                            if v_id not in seen_plan_ids:
                                seen_plan_ids.add(v_id)
                                fp = _resolve_source_disk_path(
                                    getattr(self, "base_dir", None),
                                    k,
                                    str(v.get("folder") or current_folder),
                                    v,
                                    explicit_fp=v.get("filepath"),
                                )
                                resolved_date = str(
                                    v.get("date")
                                    or v.get("file_date")
                                    or resolve_disk_file_date(
                                        fp, base_dir=getattr(self, "base_dir", None)
                                    )
                                    or ""
                                )
                                entries.append(
                                    {
                                        "key": k,
                                        "folder": v.get("folder") or current_folder,
                                        "info": v,
                                        "filepath": fp,
                                        "date": resolved_date,
                                        "file_date": resolved_date,
                                        "category": v.get("folder") or current_folder,
                                    }
                                )
                    else:
                        sub = f"{current_folder}/{k}" if current_folder else k
                        _walk_plan(v, sub)

        _walk_plan(self.plan)
        return entries

    def action_rename_node(self: TUIHostProtocol) -> None:
        """Rename file or folder category node [F2]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        node = self._get_active_node()

        # If a folder category node is explicitly focused, preserve folder rename workflow
        if node and node.data and not node.data.get("is_file"):
            data = node.data
            old_name = str(data.get("key") or "")
            old_folder_path = str(data.get("folder") or old_name)
            modal = RenameModal(
                title=f"Rename Folder: {old_folder_path}",
                current_name=old_name,
                category=old_folder_path,
                is_folder=True,
                plan=self.plan,
                base_dir=self.base_dir,
            )

            def on_folder_renamed(new_name: Optional[str | Any]) -> None:
                if not new_name:
                    return

                from app.core.path_utils import _is_absolute_path, _merge_plan_dicts

                dest_override = getattr(new_name, "destination_override", None)
                if isinstance(new_name, tuple):
                    new_name_str = str(new_name[0])
                else:
                    new_name_str = str(new_name)
                raw_target = str(dest_override if dest_override else new_name_str)
                if not raw_target:
                    return

                if dest_override or _is_absolute_path(raw_target):
                    eff_folder_path = (
                        raw_target
                        if _is_absolute_path(raw_target)
                        else raw_target.replace("\\", "/").strip("/")
                    )
                elif "/" in raw_target or "\\" in raw_target:
                    eff_folder_path = raw_target.replace("\\", "/").strip("/")
                elif (
                    "/" in old_folder_path
                    and not _is_absolute_path(old_folder_path)
                    and old_folder_path not in self.plan
                ):
                    parent_prefix = old_folder_path.rsplit("/", 1)[0]
                    eff_folder_path = f"{parent_prefix}/{raw_target}"
                else:
                    eff_folder_path = raw_target

                if not eff_folder_path or eff_folder_path == old_folder_path:
                    return

                old_content: Optional[dict[str, Any]] = None
                if old_folder_path in self.plan and isinstance(
                    self.plan[old_folder_path], dict
                ):
                    old_content = self.plan.pop(old_folder_path)
                elif "/" in old_folder_path and not _is_absolute_path(old_folder_path):
                    parent_prefix, leaf_name = old_folder_path.rsplit("/", 1)
                    parent_dict = _resolve_plan_folder_dict(self.plan, parent_prefix)
                    if parent_dict is not None and isinstance(
                        parent_dict.get(leaf_name), dict
                    ):
                        old_content = parent_dict.pop(leaf_name)
                elif old_name in self.plan and isinstance(self.plan[old_name], dict):
                    old_content = self.plan.pop(old_name)

                if old_content is None:
                    return

                def _recursively_update_folder(
                    content_dict: dict[str, Any], parent_folder: str
                ) -> None:
                    for k, v in content_dict.items():
                        if isinstance(v, dict):
                            if v.get("__type__") == "file":
                                db_b, doc_rel, abs_fp = _resolve_source_identity(
                                    self,
                                    file_key=k,
                                    filepath=v.get("filepath"),
                                    info=v,
                                )
                                if doc_rel and not v.get("source_path"):
                                    v["source_path"] = doc_rel
                                if abs_fp and not v.get("filepath"):
                                    v["filepath"] = abs_fp
                                v["folder"] = parent_folder
                                v["is_locked"] = True
                                self.locked_files[k] = parent_folder
                                if doc_rel:
                                    self.locked_files[doc_rel] = parent_folder
                                    if self.app_session:
                                        self.app_session.db.set_user_verified_target_path(
                                            db_b, doc_rel, parent_folder
                                        )
                                    _record_manual_edit(
                                        self,
                                        db_b,
                                        doc_rel,
                                        abs_fp,
                                        folder=parent_folder,
                                        is_locked=True,
                                    )
                            else:
                                sub = f"{parent_folder}/{k}" if parent_folder else k
                                _recursively_update_folder(v, sub)

                existing_dest = _resolve_plan_folder_dict(
                    self.plan, eff_folder_path, create=False
                )
                if existing_dest is not None:
                    _merge_plan_dicts(existing_dest, old_content)
                    _recursively_update_folder(existing_dest, eff_folder_path)
                    self.rebuild_tree(run_validation=True)
                    self.announce(
                        f"Merged folder category '{old_folder_path}' into '{eff_folder_path}'"
                    )
                else:
                    dest_dict = _resolve_plan_folder_dict(
                        self.plan, eff_folder_path, create=True
                    )
                    if dest_dict is None:
                        self.plan[eff_folder_path] = old_content
                        dest_dict = self.plan[eff_folder_path]
                    else:
                        dest_dict.update(old_content)
                    _recursively_update_folder(dest_dict, eff_folder_path)
                    self.rebuild_tree(run_validation=True)
                    self.announce(
                        f"Renamed folder category '{old_folder_path}' -> '{eff_folder_path}'"
                    )

            self.push_screen(modal, on_folder_renamed)
            return

        checked_entries = self._get_checked_file_entries()
        checked_keys = [e["key"] for e in checked_entries if e.get("key")]

        # Branch to batch rename when multiple files are checked
        if len(checked_keys) > 1:
            first_folder = checked_entries[0].get("folder", "")
            default_batch_pattern = (
                str(
                    getattr(getattr(self, "settings", None), "RENAME_PATTERN", "") or ""
                ).strip()
                or "{category}_{original}"
            )
            modal = RenameModal(
                title=f"Batch Rename ({len(checked_entries)} files)",
                current_name=default_batch_pattern,
                category=first_folder,
                file_date=str(checked_entries[0].get("date") or ""),
                is_folder=False,
                plan=self.plan,
                base_dir=self.base_dir,
                files=checked_entries,
            )

            def on_batch_renamed(result: Optional[str | RenameResult]) -> None:
                if not result:
                    return

                from app.core.path_utils import _disambiguate_key

                dest_override = getattr(result, "destination_override", None)
                batch_items = getattr(result, "batch_items", None)

                if not batch_items:
                    pattern_str = str(result)
                    previews = modal.get_batch_previews(pattern_str)
                    batch_items = []
                    for idx, entry in enumerate(checked_entries):
                        _meta, fallback, key, folder, info = (
                            modal._extract_file_metadata(entry, modal.seq + idx)
                        )
                        target_fn = previews[idx] if idx < len(previews) else fallback
                        target_fld = dest_override if dest_override else folder
                        batch_items.append(
                            {
                                "key": key,
                                "folder": target_fld,
                                "new_name": target_fn,
                                "info": info or entry.get("info", {}),
                                "old_folder": entry.get("folder", ""),
                                "filepath": entry.get("filepath"),
                            }
                        )

                renamed_count = 0
                for item in batch_items:
                    old_name = str(item.get("key") or item.get("old_name") or "")
                    new_filename = str(item.get("new_name") or "")
                    old_folder = str(item.get("old_folder") or item.get("folder") or "")
                    target_folder = str(
                        dest_override
                        if dest_override
                        else (item.get("folder") or old_folder)
                    )
                    data_info = item.get("info")

                    if not old_name or not new_filename:
                        continue

                    old_container = _resolve_plan_folder_dict(self.plan, old_folder)
                    if old_container is None or old_name not in old_container:
                        found_folder, found_container, found_val = _find_file_in_plan(
                            self.plan,
                            old_name,
                            target_info=data_info
                            if isinstance(data_info, dict)
                            else None,
                        )
                        if found_container is not None:
                            old_container = found_container
                            if not item.get("folder") and not dest_override:
                                target_folder = found_folder
                            old_folder = found_folder
                            if not isinstance(data_info, dict) and isinstance(
                                found_val, dict
                            ):
                                data_info = found_val

                    if not isinstance(data_info, dict) and old_container is not None:
                        existing_val = old_container.get(old_name)
                        if isinstance(existing_val, dict):
                            data_info = existing_val

                    raw_item_fp = item.get("filepath")
                    db_base, doc_relpath, abs_fp = _resolve_source_identity(
                        self,
                        file_key=old_name,
                        filepath=raw_item_fp if isinstance(raw_item_fp, str) else None,
                        info=data_info if isinstance(data_info, dict) else None,
                    )

                    # Remove from old location in self.plan
                    if old_container is not None and old_name in old_container:
                        old_container.pop(old_name, None)

                    target_container = _resolve_plan_folder_dict(
                        self.plan, target_folder, create=True
                    )
                    if target_container is None:
                        self.plan[target_folder] = {}
                        target_container = self.plan[target_folder]

                    # Collision check & numeric disambiguation
                    if (
                        new_filename in target_container
                        and target_container[new_filename] is not data_info
                    ):
                        new_filename = _disambiguate_key(
                            target_container, new_filename, is_file=True
                        )

                    if isinstance(data_info, dict):
                        if doc_relpath and not data_info.get("source_path"):
                            data_info["source_path"] = doc_relpath
                        if abs_fp and not data_info.get("filepath"):
                            data_info["filepath"] = abs_fp
                        target_container[new_filename] = data_info
                        data_info["target_filename"] = new_filename
                        data_info["folder"] = target_folder
                        data_info["is_locked"] = True
                        data_info["confirmed"] = True
                    else:
                        new_info: dict[str, Any] = {
                            "__type__": "file",
                            "target_filename": new_filename,
                            "folder": target_folder,
                            "is_locked": True,
                            "confirmed": True,
                        }
                        if doc_relpath:
                            new_info["source_path"] = doc_relpath
                        if abs_fp:
                            new_info["filepath"] = abs_fp
                        target_container[new_filename] = new_info

                    self.locked_files[new_filename] = target_folder
                    if doc_relpath:
                        self.locked_files[doc_relpath] = target_folder
                        if self.app_session:
                            self.app_session.db.set_user_verified_target_path(
                                db_base, doc_relpath, target_folder
                            )
                        _record_manual_edit(
                            self,
                            db_base,
                            doc_relpath,
                            abs_fp,
                            target_filename=new_filename,
                            folder=target_folder,
                            is_locked=True,
                            confirmed=True,
                        )
                    renamed_count += 1

                self.rebuild_tree(run_validation=True)
                self.announce(f"Batch renamed {renamed_count} files [Locked]")

            self.push_screen(modal, on_batch_renamed)
            return

        # Single file rename workflow
        if not node or not node.data:
            self.announce("Select a file or folder node to rename [F2].")
            return

        data = node.data
        is_file = data.get("is_file")
        old_name = data.get("key")

        if is_file:
            from app.core.pattern_formatter import resolve_disk_file_date

            stem, ext = os.path.splitext(old_name)
            folder_cat = data.get("folder", "")
            info_dict = data.get("info") if isinstance(data.get("info"), dict) else {}
            filepath = _resolve_source_disk_path(
                getattr(self, "base_dir", None),
                str(old_name or ""),
                str(folder_cat or ""),
                info_dict,
                explicit_fp=data.get("filepath"),
            )
            resolved_file_date = str(
                data.get("file_date")
                or data.get("date")
                or info_dict.get("file_date")
                or info_dict.get("date")
                or resolve_disk_file_date(filepath, base_dir=self.base_dir)
                or ""
            )
            modal = RenameModal(
                title=f"Rename File: {old_name}",
                current_name=stem,
                extension=ext,
                category=folder_cat,
                file_date=resolved_file_date,
                is_folder=False,
                plan=self.plan,
                base_dir=self.base_dir,
                source_path=filepath,
            )

            def on_renamed(new_formatted: Optional[str | Any]) -> None:
                if not new_formatted:
                    return

                from app.core.path_utils import _disambiguate_key

                dest_override = getattr(new_formatted, "destination_override", None)
                if isinstance(new_formatted, tuple):
                    new_formatted_str, dest_override = new_formatted
                else:
                    new_formatted_str = str(new_formatted)

                if ext and not new_formatted_str.endswith(ext):
                    new_filename = f"{new_formatted_str}{ext}"
                else:
                    new_filename = new_formatted_str

                target_folder = dest_override if dest_override else folder_cat
                if not dest_override and new_filename == old_name:
                    return

                # Remove from old location in self.plan
                old_container = _resolve_plan_folder_dict(self.plan, folder_cat)
                if old_container is None or old_name not in old_container:
                    _, found_container, _ = _find_file_in_plan(
                        self.plan,
                        old_name,
                        target_info=data.get("info")
                        if isinstance(data.get("info"), dict)
                        else None,
                    )
                    if found_container is not None:
                        old_container = found_container
                if old_container is not None and old_name in old_container:
                    old_container.pop(old_name, None)

                target_container = _resolve_plan_folder_dict(
                    self.plan, target_folder, create=True
                )
                if target_container is None:
                    self.plan[target_folder] = {}
                    target_container = self.plan[target_folder]

                # Collision check & numeric disambiguation
                file_info = data.get("info")
                db_base, doc_relpath, abs_fp = _resolve_source_identity(
                    self,
                    file_key=old_name,
                    filepath=filepath,
                    info=file_info if isinstance(file_info, dict) else None,
                )
                if (
                    new_filename in target_container
                    and target_container[new_filename] is not file_info
                ):
                    new_filename = _disambiguate_key(
                        target_container, new_filename, is_file=True
                    )

                if isinstance(file_info, dict):
                    if doc_relpath and not file_info.get("source_path"):
                        file_info["source_path"] = doc_relpath
                    if abs_fp and not file_info.get("filepath"):
                        file_info["filepath"] = abs_fp
                    target_container[new_filename] = file_info
                    file_info["target_filename"] = new_filename
                    file_info["folder"] = target_folder
                    file_info["is_locked"] = True
                    file_info["confirmed"] = True
                else:
                    file_info = {
                        "__type__": "file",
                        "target_filename": new_filename,
                        "folder": target_folder,
                        "is_locked": True,
                        "confirmed": True,
                    }
                    if doc_relpath:
                        file_info["source_path"] = doc_relpath
                    if abs_fp:
                        file_info["filepath"] = abs_fp
                    data["info"] = file_info
                    target_container[new_filename] = file_info
                data["folder"] = target_folder
                data["is_locked"] = True
                self.locked_files[new_filename] = target_folder
                if doc_relpath:
                    self.locked_files[doc_relpath] = target_folder
                    if self.app_session:
                        self.app_session.db.set_user_verified_target_path(
                            db_base, doc_relpath, target_folder
                        )
                    _record_manual_edit(
                        self,
                        db_base,
                        doc_relpath,
                        abs_fp,
                        target_filename=new_filename,
                        folder=target_folder,
                        is_locked=True,
                        confirmed=True,
                    )
                self.rebuild_tree(run_validation=True)
                self.announce(f"Renamed target file to '{new_filename}' [Locked]")

            self.push_screen(modal, on_renamed)

    def action_new_folder(self: TUIHostProtocol) -> None:
        """Create a new folder category node [Ctrl+N]."""
        if self._is_text_control_focused(allow_tree_search=True):
            return

        def on_created(folder_name: Optional[str]) -> None:
            if not folder_name:
                return
            existing = _resolve_plan_folder_dict(self.plan, folder_name, create=False)
            if existing is None:
                created = _resolve_plan_folder_dict(self.plan, folder_name, create=True)
                if created is None:
                    self.plan[folder_name] = {}
                self._manual_empty_folders.add(folder_name)
                self.rebuild_tree()
                self.announce(f"Created new folder category: '{folder_name}'")
            else:
                self.announce(f"Folder category '{folder_name}' already exists.")

        self.push_screen(NewFolderModal(), on_created)

    def action_rate_positive(self: TUIHostProtocol) -> None:
        """Set positive ML rating feedback [+] for selected node."""
        if self._is_text_control_focused():
            return
        self._set_rating_for_selected("positive")

    def action_rate_negative(self: TUIHostProtocol) -> None:
        """Set negative ML rating feedback [-] for selected node."""
        if self._is_text_control_focused():
            return
        self._set_rating_for_selected("negative")

    def _set_rating_for_selected(self: TUIHostProtocol, rating: str) -> None:
        host: Any = self
        node = host._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            host.announce("Select a file node to rate [+][-].")
            return

        from app.core.path_utils import safe_relpath

        data = node.data
        filepath = data["filepath"]
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        raw_src = info.get("source_path") or data.get("key") or filepath
        if isinstance(raw_src, str) and "::" in raw_src:
            raw_src = raw_src.split("::", 1)[1]
        if (
            getattr(host, "base_dir", None)
            and isinstance(raw_src, str)
            and os.path.isabs(raw_src)
        ):
            rel = safe_relpath(raw_src, host.base_dir)
            doc_relpath = (
                rel.replace("\\", "/")
                if not rel.startswith("..")
                else str(data.get("key") or filepath)
            )
        else:
            doc_relpath = str(raw_src).replace("\\", "/")

        current = (
            host._ratings_cache.get(filepath)
            or host._ratings_cache.get(doc_relpath)
            or data.get("rating")
        )

        rating_to_set = None if current == rating else rating
        if rating_to_set:
            host._ratings_cache[filepath] = rating_to_set
            host._ratings_cache[doc_relpath] = rating_to_set
        else:
            host._ratings_cache.pop(filepath, None)
            host._ratings_cache.pop(doc_relpath, None)
        if isinstance(info, dict):
            info["rating"] = rating_to_set

        current_folder = data.get("folder") or (
            info.get("folder") if isinstance(info, dict) else None
        )
        if rating_to_set == "negative":
            host.locked_files.pop(str(data.get("key", "")), None)
            host.locked_files.pop(doc_relpath, None)
            host.locked_files.pop(filepath, None)
            if isinstance(info, dict):
                info["is_locked"] = False
            data["is_locked"] = False

        if host.app_session:
            host.app_session.db.set_document_rating(
                host.base_dir, doc_relpath, rating_to_set
            )
            if rating_to_set == "negative" and current_folder:
                host.app_session.db.set_user_verified_target_path(
                    host.base_dir, doc_relpath, f"!{current_folder}"
                )
            elif current == "negative" and rating_to_set != "negative":
                restored_target = (
                    current_folder
                    if rating_to_set == "positive" and current_folder
                    else None
                )
                host.app_session.db.set_user_verified_target_path(
                    host.base_dir, doc_relpath, restored_target
                )

        data["rating"] = rating_to_set
        host.rebuild_tree()
        msg = f"Set rating '{rating_to_set or 'cleared'}' for '{data['key']}'"
        host.announce(msg)
