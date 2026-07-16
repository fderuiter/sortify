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


def get_safe_path(dest_dir: str, filename: str, source_path: str = None) -> str:
    """Generate a safe file path to avoid overwriting existing files."""
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.exists(safe_path):
        if source_path and os.path.exists(source_path):
            try:
                if os.path.samefile(safe_path, source_path):
                    return safe_path
            except OSError as e:
                logging.error(
                    f"Failed to verify if paths conflict for {safe_path} and {source_path}: {e}",
                    exc_info=True,
                )
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path


def _remove_empty_dirs(path: str):
    """Recursively remove empty directories."""
    if not os.path.isdir(path):
        return

    for entry in os.listdir(path):
        entry_path = os.path.join(path, entry)
        if os.path.isdir(entry_path):
            _remove_empty_dirs(entry_path)

    if not os.listdir(path):
        os.rmdir(path)


def _execute_moves_recursive(
    base_dir: str, plan: dict, db, current_dest: str = "", path_map: dict = None
) -> None:
    """Recursively move files according to the plan."""
    if path_map is None:
        path_map = {}

    if not isinstance(plan, dict) or plan.get("__type__") in ("file", "directory"):
        return

    for key, content in plan.items():
        if content is None or (
            isinstance(content, dict) and content.get("__type__") in ("file", "directory")
        ):
            if isinstance(content, dict) and content.get("__type__") == "directory":
                continue
                
            if isinstance(content, dict) and content.get("status") == "Already Sorted":
                # Even if already sorted, the target might have moved, so we still process links
                pass

            # It's a file, key is the original relative path
            source_path = os.path.join(base_dir, key)
            if not os.path.exists(source_path):
                continue

            if isinstance(content, dict) and "target_filename" in content:
                filename = content["target_filename"]
            else:
                filename = os.path.basename(key)

            dest_dir = os.path.join(base_dir, current_dest)

            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)

            dest_path = get_safe_path(dest_dir, filename, source_path)

            link_info = LinkManager.get_link_info(source_path)
            moved_as_link = False

            if link_info:
                original_target = link_info["target"]
                abs_target = original_target
                if not os.path.isabs(original_target):
                    abs_target = os.path.normpath(
                        os.path.join(os.path.dirname(source_path), original_target)
                    )

                new_abs_target = path_map.get(abs_target, abs_target)

                # Check if we need to update the link
                needs_update = (dest_path != source_path) or (
                    new_abs_target != abs_target
                )

                if needs_update:
                    if link_info["type"] == "symlink":
                        if not os.path.isabs(original_target):
                            final_target = os.path.relpath(new_abs_target, dest_dir)
                        else:
                            final_target = new_abs_target

                        # If updating in place
                        if dest_path == source_path:
                            os.remove(source_path)
                        os.symlink(final_target, dest_path)
                        if dest_path != source_path:
                            os.remove(source_path)
                        moved_as_link = True

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
                            # If updating in place
                            if dest_path == source_path:
                                os.remove(source_path)
                            pylnk3.for_file(
                                new_abs_target, lnk_name=dest_path, **kwargs
                            )
                            if dest_path != source_path:
                                os.remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            logging.error(
                                f"Failed to update Windows shortcut {source_path}: {e}",
                                exc_info=True,
                            )

            # Record user verified target
            doc = db.get_document(base_dir, key)
            if doc and doc.get("file_hash"):
                db.set_user_verified_target(base_dir, doc["file_hash"], current_dest.replace("\\", "/"))

            if dest_path == source_path:
                continue

            if not moved_as_link:
                shutil.move(source_path, dest_path)
                
            # Update filepath in database
            rel_dest = os.path.relpath(dest_path, base_dir)
            db.update_document_path(base_dir, key, rel_dest)
        else:
            # It's a folder
            _execute_moves_recursive(base_dir, content, db, os.path.join(current_dest, key), path_map)


def execute_moves(base_dir: str, plan: dict, db, history_manager, runtime_settings=None) -> dict:
    """Create directories and safely move files, tracking file-system errors."""
    # Create a full snapshot of the directory tree and metadata before moving files
    session_id = history_manager.create_snapshot(base_dir)
    logging.info(f"Created snapshot session {session_id} for {base_dir}")

    # Build path mapping to track where targets move
    moves_list = VerificationEngine.get_moves(base_dir, plan)
    path_map = {}
    for rel_src, src, dst in moves_list:
        path_map[os.path.abspath(src)] = os.path.abspath(dst)

    # Execute all moves first
    _execute_moves_recursive(base_dir, plan, db, "", path_map)

    summary = {"deleted_folders": 0, "protected_folders": 0}
    cleanup_enabled = getattr(runtime_settings, "CLEANUP_EMPTY_FOLDERS", True) if runtime_settings else True

    # Find the directory nodes in the plan
    dirs_to_process = []
    
    def _find_dir_nodes(node):
        if not isinstance(node, dict) or node.get("__type__") in ("file", "directory"):
            return
        for k, v in node.items():
            if isinstance(v, dict) and v.get("__type__") == "directory":
                dirs_to_process.append(v)
            elif isinstance(v, dict) and v.get("__type__") != "file":
                _find_dir_nodes(v)
                
    _find_dir_nodes(plan)
    
    # Sort by descending depth to delete subdirectories before parents
    dirs_to_process.sort(key=lambda x: len(x["source_path"].split(os.sep)), reverse=True)

    if cleanup_enabled:
        for node in dirs_to_process:
            if node.get("protected"):
                summary["protected_folders"] += 1
            elif node.get("status") == "To Be Deleted":
                try:
                    if os.path.isdir(node["source_path"]) and not os.listdir(node["source_path"]):
                        os.rmdir(node["source_path"])
                        summary["deleted_folders"] += 1
                except OSError:
                    pass
                    
        # Guarantee complete cleanup of empty directories after all explicit plan folders are processed
        for entry in os.listdir(base_dir):
            entry_path = os.path.join(base_dir, entry)
            if os.path.isdir(entry_path):
                _remove_empty_dirs(entry_path)
    else:
        for node in dirs_to_process:
            summary["protected_folders"] += 1

    return summary
