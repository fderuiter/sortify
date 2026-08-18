"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import logging
import os
import shutil  # noqa: F401

from app.core.link_manager import LinkManager
from app.core.path_utils import fallback_parse_lnk, resolve_mapped_path
from app.core.verifier import VerificationEngine

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def _is_same_path(p1: str, p2: str) -> bool:
    if p1 is None or p2 is None:
        return p1 == p2
    return os.path.normcase(os.path.abspath(p1)) == os.path.normcase(
        os.path.abspath(p2)
    )


def is_subpath_or_equal(child: str, parent: str) -> bool:
    """Check if child path is equal to or nested within parent path (case-insensitive)."""
    if child is None or parent is None:
        return False
    abs_child = os.path.normcase(os.path.abspath(child))
    abs_parent = os.path.normcase(os.path.abspath(parent))
    if abs_child == abs_parent:
        return True
    if not abs_parent.endswith(os.sep):
        abs_parent += os.sep
    return abs_child.startswith(abs_parent)


def get_safe_path(dest_dir: str, filename: str, source_path: str = None) -> str:
    """Generate a safe file path to avoid overwriting existing files."""
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.lexists(safe_path):
        if source_path and os.path.lexists(source_path):
            try:
                if os.path.samefile(safe_path, source_path):
                    return safe_path
            except OSError as e:
                if _is_same_path(safe_path, source_path):
                    return safe_path
                logging.error(
                    f"Failed to verify if paths conflict for {safe_path} and {source_path}: {e}",
                    exc_info=True,
                )
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path


def _remove_empty_dirs(path: str, protected_paths: list[str] = None):
    """Recursively remove empty directories, respecting protected paths."""
    if protected_paths:
        for p in protected_paths:
            if is_subpath_or_equal(path, p):
                return

    if not os.path.isdir(path):
        return

    for entry in os.listdir(path):
        entry_path = os.path.join(path, entry)
        if os.path.isdir(entry_path):
            _remove_empty_dirs(entry_path, protected_paths)

    if not os.listdir(path):
        from app.core.resilient_file_ops import resilient_remove

        try:
            resilient_remove(path)
        except OSError:
            pass


def _execute_moves_recursive(
    base_dir: str,
    plan: dict,
    db,
    current_dest: str = "",
    path_map: dict = None,
    db_updates_batch: list = None,
    active_parent_path: str = "",
    depth: int = 0,
    runtime_settings=None,
) -> None:
    """Recursively move files according to the plan."""
    base_dir = os.path.normpath(base_dir)
    if path_map is None:
        path_map = {}

    if not isinstance(plan, dict) or plan.get("__type__") in ("file", "directory"):
        return

    for key, content in plan.items():
        if content is None or (
            isinstance(content, dict)
            and content.get("__type__") in ("file", "directory")
        ):
            is_dir = isinstance(content, dict) and content.get("__type__") == "directory"

            if depth > 0 and not is_dir and isinstance(content, dict):
                if "relative_source" not in content and "source_path" not in content:
                    raise ValueError(
                        f"Missing required relative source metadata field for nested item '{key}'"
                    )

            if isinstance(content, dict) and "source_path" in content:
                src_p = content["source_path"]
                source_path = os.path.normpath(src_p if os.path.isabs(src_p) else os.path.join(base_dir, src_p))
            elif isinstance(content, dict) and "relative_source" in content:
                rel_src = content["relative_source"]
                cand1 = os.path.normpath(os.path.join(base_dir, active_parent_path, rel_src))
                cand2 = os.path.normpath(os.path.join(base_dir, rel_src))
                if os.path.lexists(cand1):
                    source_path = cand1
                elif os.path.lexists(cand2):
                    source_path = cand2
                else:
                    source_path = cand1 if depth > 0 else cand2
            else:
                cand1 = os.path.normpath(os.path.join(base_dir, active_parent_path, key))
                cand2 = os.path.normpath(os.path.join(base_dir, key))
                if os.path.lexists(cand1):
                    source_path = cand1
                elif os.path.lexists(cand2):
                    source_path = cand2
                else:
                    source_path = cand1 if depth > 0 else cand2

            if isinstance(content, dict) and "target_filename" in content:
                filename = content["target_filename"]
            else:
                filename = os.path.basename(key)

            if is_dir:
                if os.path.exists(source_path) and os.path.isdir(source_path):
                    sub_plan = {}
                    for item in os.listdir(source_path):
                        item_abs = os.path.join(source_path, item)
                        if os.path.isdir(item_abs):
                            sub_plan[item] = {
                                "__type__": "directory",
                                "relative_source": item,
                                "source_path": item_abs,
                            }
                        else:
                            sub_plan[item] = {
                                "__type__": "file",
                                "relative_source": item,
                                "source_path": item_abs,
                            }
                    _execute_moves_recursive(
                        base_dir,
                        sub_plan,
                        db,
                        os.path.join(current_dest, filename),
                        path_map,
                        db_updates_batch,
                        os.path.join(active_parent_path, key),
                        depth + 1,
                        runtime_settings,
                    )
                continue

            import unicodedata

            dest_dir = os.path.normpath(os.path.join(base_dir, current_dest))
            dest_dir = unicodedata.normalize("NFC", dest_dir)

            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)

            target_path = os.path.normpath(os.path.join(dest_dir, filename))
            collision = False
            if os.path.lexists(target_path):
                is_same = False
                if os.path.lexists(source_path):
                    try:
                        is_same = os.path.samefile(target_path, source_path)
                    except OSError:
                        is_same = _is_same_path(target_path, source_path)
                if not is_same:
                    collision = True

            conflict_policy = "rename"
            if runtime_settings:
                conflict_policy = getattr(runtime_settings, "CONFLICT_POLICY", "rename")

            if collision and conflict_policy == "skip":
                logging.info(
                    f"Collision detected for {target_path}. Policy is 'skip', bypassing move."
                )
                continue

            dest_path = os.path.normpath(get_safe_path(dest_dir, filename, source_path))
            dest_path = unicodedata.normalize("NFC", dest_path)

            link_info = LinkManager.get_link_info(source_path)
            if not link_info:
                if os.path.islink(source_path):
                    try:
                        target = os.readlink(source_path)
                        link_info = {"type": "symlink", "target": target}
                    except OSError:
                        pass
                elif source_path.lower().endswith(".lnk"):
                    if pylnk3:
                        try:
                            lnk = pylnk3.parse(source_path)
                            if lnk and getattr(lnk, "path", None):
                                link_info = {"type": "lnk", "target": lnk.path}
                        except Exception:
                            pass
                    if not link_info:
                        target = fallback_parse_lnk(source_path)
                        if target:
                            link_info = {"type": "lnk", "target": target}

            moved_as_link = False

            if link_info:
                original_target = link_info["target"]
                abs_target = original_target
                if not os.path.isabs(original_target):
                    abs_target = os.path.normpath(
                        os.path.join(os.path.dirname(source_path), original_target)
                    )

                new_abs_target = resolve_mapped_path(path_map, abs_target)

                # Check if we need to update the link
                needs_update = not _is_same_path(
                    dest_path, source_path
                ) or not _is_same_path(new_abs_target, abs_target)

                if needs_update:
                    import uuid

                    shadow_name = f"{dest_path}.shadow_{uuid.uuid4().hex}"

                    if link_info["type"] == "symlink":
                        if not os.path.isabs(original_target):
                            final_target = os.path.relpath(new_abs_target, dest_dir)
                        else:
                            final_target = new_abs_target

                        try:
                            os.symlink(final_target, shadow_name)
                            if not os.path.lexists(shadow_name):
                                raise RuntimeError(
                                    "Shadow link creation failed validation."
                                )

                            os.replace(shadow_name, dest_path)
                            if not _is_same_path(dest_path, source_path):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            if os.path.lexists(shadow_name):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(shadow_name)
                            logging.error(
                                f"Failed to atomically update symlink {source_path}: {e}",
                                exc_info=True,
                            )
                            raise

                    elif link_info["type"] == "lnk":
                        if pylnk3:
                            try:
                                parsed = pylnk3.parse(source_path)
                                kwargs = {
                                    "arguments": getattr(parsed, "arguments", None),
                                    "description": getattr(parsed, "description", None),
                                    "icon_file": getattr(parsed, "icon", None),
                                    "icon_index": getattr(parsed, "icon_index", 0),
                                    "work_dir": getattr(parsed, "work_dir", None),
                                    "window_mode": getattr(parsed, "window_mode", None),
                                }

                                pylnk3.for_file(
                                    new_abs_target, lnk_name=shadow_name, **kwargs
                                )
                                if not os.path.lexists(shadow_name):
                                    raise RuntimeError(
                                        "Shadow link creation failed validation."
                                    )

                                os.replace(shadow_name, dest_path)
                                if not _is_same_path(dest_path, source_path):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(source_path)
                                moved_as_link = True
                            except Exception as e:
                                if os.path.lexists(shadow_name):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(shadow_name)
                                logging.error(
                                    f"Failed to atomically update Windows shortcut {source_path}: {e}",
                                    exc_info=True,
                                )
                                raise
                        else:
                            try:
                                with open(shadow_name, "w", encoding="utf-8") as f:
                                    f.write(new_abs_target)
                                os.replace(shadow_name, dest_path)
                                if not _is_same_path(dest_path, source_path):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(source_path)
                                moved_as_link = True
                            except Exception as e:
                                if os.path.lexists(shadow_name):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(shadow_name)
                                logging.error(
                                    f"Failed to atomically update Windows shortcut fallback {source_path}: {e}",
                                    exc_info=True,
                                )
                                raise

            source_rel_path = os.path.relpath(source_path, base_dir).replace("\\", "/")
            doc = db.get_document(base_dir, source_rel_path)

            if dest_path == source_path:
                # Still record user verified target if needed even if not moving
                if doc and doc.get("file_hash"):
                    if db_updates_batch is not None:
                        db_updates_batch.append(
                            {
                                "type": "verified_target",
                                "args": (
                                    base_dir,
                                    doc["file_hash"],
                                    current_dest.replace("\\", "/"),
                                ),
                            }
                        )
                    else:
                        db.set_user_verified_target(
                            base_dir, doc["file_hash"], current_dest.replace("\\", "/")
                        )
                continue

            if not moved_as_link:
                from app.core.resilient_file_ops import resilient_move

                resilient_move(source_path, dest_path)

            # Record user verified target and update filepath only after successful move
            if doc and doc.get("file_hash"):
                if db_updates_batch is not None:
                    db_updates_batch.append(
                        {
                            "type": "verified_target",
                            "args": (
                                base_dir,
                                doc["file_hash"],
                                current_dest.replace("\\", "/"),
                            ),
                        }
                    )
                else:
                    db.set_user_verified_target(
                        base_dir, doc["file_hash"], current_dest.replace("\\", "/")
                    )

            # Update filepath in database
            rel_dest = os.path.relpath(dest_path, base_dir).replace("\\", "/")
            if db_updates_batch is not None:
                db_updates_batch.append(
                    {
                        "type": "document_path",
                        "args": (base_dir, source_rel_path, rel_dest),
                    }
                )
            else:
                db.update_document_path(base_dir, source_rel_path, rel_dest)
        else:
            # It's a folder
            _execute_moves_recursive(
                base_dir,
                content,
                db,
                os.path.join(current_dest, key),
                path_map,
                db_updates_batch,
                os.path.join(active_parent_path, key),
                depth + 1,
                runtime_settings,
            )


def execute_moves(
    base_dir: str,
    plan: dict,
    db,
    history_manager,
    runtime_settings=None,
    resume: bool = False,
) -> dict:
    """Create directories and safely move files, tracking file-system errors."""
    base_dir = os.path.normpath(base_dir)
    session_id = None
    if not resume:
        # Create a full snapshot of the directory tree and metadata before moving files
        session_id = history_manager.create_snapshot(base_dir)
        logging.info(f"Created snapshot session {session_id} for {base_dir}")
    else:
        logging.info(f"Resuming snapshot session for {base_dir}")
        try:
            sessions = history_manager.get_sessions()
            for s in sessions:
                if s["base_dir"] == base_dir and s["status"] == "active":
                    session_id = s["session_id"]
                    break
        except Exception:
            pass

    # Build path mapping to track where targets move
    moves_list = VerificationEngine.get_moves(base_dir, plan)
    path_map = {}
    for rel_src, src, dst in moves_list:
        path_map[os.path.normcase(os.path.abspath(src))] = os.path.abspath(dst)

    # Execute all moves first
    db_updates_batch = []
    try:
        _execute_moves_recursive(
            base_dir,
            plan,
            db,
            "",
            path_map,
            db_updates_batch,
            runtime_settings=runtime_settings,
        )

        summary = {"deleted_folders": 0, "protected_folders": 0}
        cleanup_enabled = (
            getattr(runtime_settings, "CLEANUP_EMPTY_FOLDERS", True)
            if runtime_settings
            else True
        )

        # Find the directory nodes in the plan
        dirs_to_process = []

        def _find_dir_nodes(node):
            if not isinstance(node, dict) or node.get("__type__") in (
                "file",
                "directory",
            ):
                return
            for k, v in node.items():
                if isinstance(v, dict) and v.get("__type__") == "directory":
                    dirs_to_process.append(v)
                elif isinstance(v, dict) and v.get("__type__") != "file":
                    _find_dir_nodes(v)

        _find_dir_nodes(plan)

        # Sort by descending depth to delete subdirectories before parents
        dirs_to_process.sort(
            key=lambda x: len((x.get("source_path") or x.get("relative_source") or "").replace("\\", "/").split("/")),
            reverse=True,
        )

        if cleanup_enabled:
            protected_paths = getattr(runtime_settings, "PROTECTED_PATHS", [])
            protected_paths = [os.path.normpath(p) for p in protected_paths]

            for node in dirs_to_process:
                is_protected = node.get("protected")
                source_path = node.get("source_path")
                if not is_protected and source_path:
                    for p in protected_paths:
                        if is_subpath_or_equal(source_path, p):
                            is_protected = True
                            break

                if is_protected:
                    summary["protected_folders"] += 1
                elif node.get("status") == "To Be Deleted":
                    try:
                        if os.path.isdir(node["source_path"]) and not os.listdir(
                            node["source_path"]
                        ):
                            from app.core.resilient_file_ops import resilient_remove

                            resilient_remove(node["source_path"])
                            summary["deleted_folders"] += 1
                    except OSError:
                        pass

            # Guarantee complete cleanup of empty directories after all explicit plan folders are processed
            for entry in os.listdir(base_dir):
                entry_path = os.path.join(base_dir, entry)
                if os.path.isdir(entry_path):
                    _remove_empty_dirs(entry_path, protected_paths)
        else:
            for node in dirs_to_process:
                summary["protected_folders"] += 1

        db.execute_batch_updates(db_updates_batch)
        return summary

    except Exception as e:
        try:
            db.execute_batch_updates(db_updates_batch)
        except Exception:
            pass

        if session_id:
            logging.error(
                f"Error during background sorting: {e}. Initiating automatic rollback for session {session_id}"
            )
            try:
                history_manager.rollback(session_id, ignore_missing=True)
                logging.info(
                    f"Automatic rollback completed successfully for session {session_id}"
                )
            except Exception as rollback_err:
                logging.error(
                    f"Automatic rollback failed for session {session_id}: {rollback_err}",
                    exc_info=True,
                )
        raise e
