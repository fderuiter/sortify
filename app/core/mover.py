"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import shutil
from pathlib import Path


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


def _execute_moves_recursive(base_dir: str | Path, plan: dict, current_dest: str | Path = "") -> None:
    """Recursively move files according to the plan."""
    if not isinstance(plan, dict) or plan.get("__type__") == "file":
        return

    base_path = Path(base_dir)
    dest_path = Path(current_dest)

    for key, content in plan.items():
        if content is None or (
            isinstance(content, dict) and content.get("__type__") == "file"
        ):
            if isinstance(content, dict) and content.get("status") == "Already Sorted":
                continue

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
            
            if Path(dest_file_path_str) == source_path:
                continue

            shutil.move(source_path.as_posix(), dest_file_path_str)
        else:
            # It's a folder
            _execute_moves_recursive(base_path, content, dest_path / key)


def execute_moves(base_dir: str | Path, plan: dict) -> None:
    """Create directories and safely move files, tracking file-system errors."""
    base_path = Path(base_dir)
    # Execute all moves first
    _execute_moves_recursive(base_path, plan, "")

    # Then clean up empty source directories
    if not base_path.exists():
        return

    for entry in base_path.iterdir():
        if entry.is_dir() and entry.name not in plan:
            # Only clean up directories that weren't generated as top-level folders in the plan
            _remove_empty_dirs(entry)
