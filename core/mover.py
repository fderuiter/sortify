# core/mover.py
"""
Mover Module

This module handles the safe moving of files into their designated folders
based on the generated sorting plan.
"""

import os
import shutil
import logging

def get_safe_path(dest_dir, filename):
    """
    Generates a safe destination path to avoid overwriting existing files.

    If a file with the same name already exists in the destination directory,
    this function appends a numerical counter to the filename.

    :param dest_dir: The directory where the file is intended to be moved.
    :type dest_dir: str
    :param filename: The original name of the file.
    :type filename: str
    :return: A safe, non-conflicting absolute file path.
    :rtype: str
    """
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.exists(safe_path):
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path

def execute_moves(base_dir, plan):
    """
    Creates directories and safely moves files, tracking file-system errors.

    :param base_dir: The base directory where the folders will be created and files moved from.
    :type base_dir: str
    :param plan: A dictionary mapping folder names to lists of filenames to move into them.
    :type plan: dict
    :return: None
    :rtype: None
    :raises Exception: Catches and logs errors during directory creation or file moving.
    """
    for folder, files in plan.items():
        if not files: 
            continue
            
        folder_path = os.path.join(base_dir, folder)
        if not os.path.exists(folder_path):
            try:
                os.makedirs(folder_path)
            except Exception as e:
                logging.error(f"Failed to create directory {folder_path}. Error: {str(e)}")
                continue
            
        for item in files:
            source_path = os.path.join(base_dir, item)
            dest_path = get_safe_path(folder_path, item)
            try:
                shutil.move(source_path, dest_path)
            except Exception as e:
                # Logs permissions blocks or file system locks centrally
                logging.error(f"Failed to move file {item} to {folder_path}. Check if file is open elsewhere. Error: {str(e)}")