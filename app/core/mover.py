"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import logging
import os
import shutil

from app.core.link_manager import LinkManager
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
        try:
            os.rmdir(path)
        except OSError:
            import gc
            import time

            for _ in range(5):
                try:
                    gc.collect()
                    time.sleep(0.05)
                    os.rmdir(path)
                    break
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
            if isinstance(content, dict) and content.get("__type__") == "directory":
                continue

            if isinstance(content, dict) and content.get("status") == "Already Sorted":
                # Even if already sorted, the target might have moved, so we still process links
                pass

            if depth > 0:
                if not isinstance(content, dict) or "relative_source" not in content:
                    raise ValueError(
                        f"Missing required relative source metadata field for nested item '{key}'"
                    )
                relative_source = content["relative_source"]
                rel_src_with_parent = os.path.join(active_parent_path, relative_source)
                source_path = os.path.normpath(
                    os.path.join(base_dir, rel_src_with_parent)
                )
            else:
                if isinstance(content, dict) and "relative_source" in content:
                    relative_source = content["relative_source"]
                    source_path = os.path.normpath(
                        os.path.join(base_dir, relative_source)
                    )
                else:
                    source_path = os.path.normpath(os.path.join(base_dir, key))

            if not os.path.lexists(source_path):
                continue

            if isinstance(content, dict) and "target_filename" in content:
                filename = content["target_filename"]
            else:
                filename = os.path.basename(key)

            import unicodedata

            dest_dir = os.path.normpath(os.path.join(base_dir, current_dest))
            dest_dir = unicodedata.normalize("NFC", dest_dir)

            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)

            dest_path = os.path.normpath(get_safe_path(dest_dir, filename, source_path))
            dest_path = unicodedata.normalize("NFC", dest_path)

            link_info = LinkManager.get_link_info(source_path)
            moved_as_link = False

            if link_info:
                original_target = link_info["target"]
                abs_target = original_target
                if not os.path.isabs(original_target):
                    abs_target = os.path.normpath(
                        os.path.join(os.path.dirname(source_path), original_target)
                    )

                new_abs_target = path_map.get(
                    os.path.normcase(os.path.abspath(abs_target))
                    if abs_target
                    else abs_target,
                    abs_target,
                )

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
                                os.remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            if os.path.lexists(shadow_name):
                                os.remove(shadow_name)
                            logging.error(
                                f"Failed to atomically update symlink {source_path}: {e}",
                                exc_info=True,
                            )
                            raise

                    elif link_info["type"] == "lnk" and pylnk3:
                        try:
                            parsed = pylnk3.parse(source_path)
                            kwargs = {
                                "arguments": parsed.arguments,
                                "description": parsed.description,
                                "icon_file": parsed.icon,
                                "icon_index": getattr(parsed, "icon_index", 0),
                                "work_dir": parsed.work_dir,
                                "window_mode": parsed.window_mode,
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
                                os.remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            if os.path.lexists(shadow_name):
                                os.remove(shadow_name)
                            logging.error(
                                f"Failed to atomically update Windows shortcut {source_path}: {e}",
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
                shutil.move(source_path, dest_path)

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
        _execute_moves_recursive(base_dir, plan, db, "", path_map, db_updates_batch)

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
            key=lambda x: len(x["source_path"].replace("\\", "/").split("/")),
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
                            os.rmdir(node["source_path"])
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
