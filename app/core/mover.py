"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import os
import shutil


def get_safe_path(dest_dir: str, filename: str) -> str:
    """Generate a safe file path to avoid overwriting existing files."""
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.exists(safe_path):
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


def _execute_moves_recursive(base_dir: str, plan: dict, current_dest: str = "") -> None:
    """Recursively move files according to the plan."""
    for key, content in plan.items():
        if content is None:
            # It's a file, key is the original relative path
            source_path = os.path.join(base_dir, key)
            if not os.path.exists(source_path):
                continue
                
            filename = os.path.basename(key)
            dest_dir = os.path.join(base_dir, current_dest)
            
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)
                    
            dest_path = get_safe_path(dest_dir, filename)
            shutil.move(source_path, dest_path)
        else:
            # It's a folder
            _execute_moves_recursive(base_dir, content, os.path.join(current_dest, key))

def execute_moves(base_dir: str, plan: dict) -> None:
    """Create directories and safely move files, tracking file-system errors."""
    # Execute all moves first
    _execute_moves_recursive(base_dir, plan, "")
    
    # Then clean up empty source directories
    for entry in os.listdir(base_dir):
        entry_path = os.path.join(base_dir, entry)
        if os.path.isdir(entry_path) and entry not in plan:
            # Only clean up directories that weren't generated as top-level folders in the plan
            _remove_empty_dirs(entry_path)
