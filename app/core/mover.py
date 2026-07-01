"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

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
            except OSError:
                pass
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


def _execute_moves_recursive(base_dir: str, plan: dict, current_dest: str = "", path_map: dict = None) -> None:
    """Recursively move files according to the plan."""
    if path_map is None:
        path_map = {}
        
    if not isinstance(plan, dict) or plan.get("__type__") == "file":
        return

    for key, content in plan.items():
        if content is None or (
            isinstance(content, dict) and content.get("__type__") == "file"
        ):
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
                    abs_target = os.path.normpath(os.path.join(os.path.dirname(source_path), original_target))
                    
                new_abs_target = path_map.get(abs_target, abs_target)
                
                # Check if we need to update the link
                needs_update = (dest_path != source_path) or (new_abs_target != abs_target)
                
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
                        if dest_path != source_path and os.path.exists(source_path):
                            os.remove(source_path)
                        moved_as_link = True
                        
                    elif link_info["type"] == "lnk" and pylnk3:
                        try:
                            parsed = pylnk3.parse(source_path)
                            kwargs = {
                                'arguments': parsed.arguments,
                                'description': parsed.description,
                                'icon_file': parsed.icon,
                                'icon_index': getattr(parsed, 'icon_index', 0),
                                'work_dir': parsed.work_dir,
                                'window_mode': parsed.window_mode
                            }
                            # If updating in place
                            if dest_path == source_path:
                                os.remove(source_path)
                            pylnk3.for_file(new_abs_target, lnk_name=dest_path, **kwargs)
                            if dest_path != source_path and os.path.exists(source_path):
                                os.remove(source_path)
                            moved_as_link = True
                        except Exception:
                            pass
                            
            if dest_path == source_path:
                continue

            if not moved_as_link:
                shutil.move(source_path, dest_path)
        else:
            # It's a folder
            _execute_moves_recursive(base_dir, content, os.path.join(current_dest, key), path_map)


def execute_moves(base_dir: str, plan: dict) -> None:
    """Create directories and safely move files, tracking file-system errors."""
    # Build path mapping to track where targets move
    moves_list = VerificationEngine.get_moves(base_dir, plan)
    path_map = {}
    for rel_src, src, dst in moves_list:
        path_map[os.path.abspath(src)] = os.path.abspath(dst)
        
    # Execute all moves first
    _execute_moves_recursive(base_dir, plan, "", path_map)

    # Then clean up empty source directories
    for entry in os.listdir(base_dir):
        entry_path = os.path.join(base_dir, entry)
        if os.path.isdir(entry_path) and entry not in plan:
            # Only clean up directories that weren't generated as top-level folders in the plan
            _remove_empty_dirs(entry_path)
