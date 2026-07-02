"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import os
import shutil
from pathlib import Path

from app.core.link_manager import LinkManager
from app.core.verifier import VerificationEngine

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def get_safe_path(dest_dir: str | Path, filename: str, source_path: str | Path = None) -> str:
    """Generate a safe file path to avoid overwriting existing files."""
    dest_path = Path(dest_dir)
    safe_path = dest_path / filename
    
    if not safe_path.exists():
        return safe_path.as_posix()
        
    base = safe_path.stem
    extension = safe_path.suffix
    counter = 1
    
    while safe_path.exists():
        if source_path and Path(source_path).exists():
            try:
                if safe_path.samefile(source_path):
                    return safe_path.as_posix()
            except OSError:
                pass
        safe_path = dest_path / f"{base}_{counter}{extension}"
        counter += 1
    return safe_path.as_posix()


def _remove_empty_dirs(path: str | Path):
    """Recursively remove empty directories."""
    dir_path = Path(path)
    if not dir_path.is_dir():
        return

    for entry in dir_path.iterdir():
        if entry.is_dir():
            _remove_empty_dirs(entry)

    if not any(dir_path.iterdir()):
        try:
            dir_path.rmdir()
        except OSError:
            pass


def _execute_moves_recursive(base_dir: str | Path, plan: dict, current_dest: str | Path = "", path_map: dict = None) -> None:
    """Recursively move files according to the plan."""
    if path_map is None:
        path_map = {}
        
    if not isinstance(plan, dict) or plan.get("__type__") == "file":
        return

    base_path = Path(base_dir)
    dest_path = Path(current_dest)

    for key, content in plan.items():
        if content is None or (
            isinstance(content, dict) and content.get("__type__") == "file"
        ):
            if isinstance(content, dict) and content.get("status") == "Already Sorted":
                # Even if already sorted, the target might have moved, so we still process links
                pass

            # It's a file, key is the original relative path
            source_path = base_path / key
            if not source_path.exists():
                continue

            if isinstance(content, dict) and "target_filename" in content:
                filename = content["target_filename"]
            else:
                filename = Path(key).name

            target_dir = base_path / dest_path
            target_dir.mkdir(parents=True, exist_ok=True)

            dest_file_path_str = get_safe_path(target_dir, filename, source_path)
            dest_file_path = Path(dest_file_path_str)

            link_info = LinkManager.get_link_info(source_path.as_posix())
            moved_as_link = False
            
            if link_info:
                original_target = link_info["target"]
                abs_target = original_target
                
                # Check if original_target is absolute
                is_abs = Path(original_target).is_absolute()
                if not is_abs:
                    abs_target = (source_path.parent / original_target).resolve().as_posix()
                    
                new_abs_target = path_map.get(abs_target, abs_target)
                
                # Check if we need to update the link
                needs_update = (dest_file_path_str != source_path.as_posix()) or (new_abs_target != abs_target)
                
                if needs_update:
                    if link_info["type"] == "symlink":
                        if not is_abs:
                            final_target = os.path.relpath(new_abs_target, target_dir.as_posix())
                        else:
                            final_target = new_abs_target
                            
                        # If updating in place
                        if dest_file_path_str == source_path.as_posix():
                            source_path.unlink()
                        dest_file_path.symlink_to(final_target)
                        if dest_file_path_str != source_path.as_posix() and source_path.exists():
                            source_path.unlink()
                        moved_as_link = True
                        
                    elif link_info["type"] == "lnk" and pylnk3:
                        try:
                            parsed = pylnk3.parse(source_path.as_posix())
                            kwargs = {
                                'arguments': parsed.arguments,
                                'description': parsed.description,
                                'icon_file': parsed.icon,
                                'icon_index': getattr(parsed, 'icon_index', 0),
                                'work_dir': parsed.work_dir,
                                'window_mode': parsed.window_mode
                            }
                            # If updating in place
                            if dest_file_path_str == source_path.as_posix():
                                source_path.unlink()
                            pylnk3.for_file(new_abs_target, lnk_name=dest_file_path_str, **kwargs)
                            if dest_file_path_str != source_path.as_posix() and source_path.exists():
                                source_path.unlink()
                            moved_as_link = True
                        except Exception:
                            pass
                            
            if dest_file_path_str == source_path.as_posix():
                continue

            if not moved_as_link:
                shutil.move(source_path.as_posix(), dest_file_path_str)
        else:
            # It's a folder
            _execute_moves_recursive(base_path, content, dest_path / key, path_map)


def execute_moves(base_dir: str | Path, plan: dict) -> None:
    """Create directories and safely move files, tracking file-system errors."""
    base_path = Path(base_dir)
    # Build path mapping to track where targets move
    moves_list = VerificationEngine.get_moves(base_path, plan)
    path_map = {}
    for rel_src, src, dst in moves_list:
        path_map[Path(src).resolve().as_posix()] = Path(dst).resolve().as_posix()
        
    # Execute all moves first
    _execute_moves_recursive(base_path, plan, "", path_map)

    # Then clean up empty source directories
    if not base_path.exists():
        return

    for entry in base_path.iterdir():
        if entry.is_dir() and entry.name not in plan:
            # Only clean up directories that weren't generated as top-level folders in the plan
            _remove_empty_dirs(entry)
