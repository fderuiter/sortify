"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import logging
import os
import shutil


def get_safe_path(dest_dir: str, filename: str) -> str:
    """Generate a safe file path to avoid overwriting existing files.

    Parameters
    ----------
    dest_dir : str
        The destination directory path.
    filename : str
        The original filename.

    Returns
    -------
    str
        A unique file path that does not already exist in the destination.

    """
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.exists(safe_path):
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path


def execute_moves(base_dir: str, plan: dict) -> None:
    """Create directories and safely move files, tracking file-system errors.

    Parameters
    ----------
    base_dir : str
        The base directory where files are located and folders will be created.
    plan : dict
        A mapping of destination folder names to lists of files to be moved there.

    Returns
    -------
    None

    """
    for folder, files in plan.items():
        if not files:
            continue

        folder_path = os.path.join(base_dir, folder)
        if not os.path.exists(folder_path):
            try:
                os.makedirs(folder_path)
            except Exception as e:
                logging.error(
                    f"Failed to create directory {folder_path}. Error: {str(e)}"
                )
                continue

        for item in files:
            source_path = os.path.join(base_dir, item)
            dest_path = get_safe_path(folder_path, item)
            try:
                shutil.move(source_path, dest_path)
            except Exception as e:
                # Logs permissions blocks or file system locks centrally
                logging.error(
                    f"Failed to move file {item} to {folder_path}. "
                    f"Check if file is open elsewhere. Error: {str(e)}"
                )
