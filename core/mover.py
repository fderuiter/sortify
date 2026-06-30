# core/mover.py
import os
import shutil
import logging

def get_safe_path(dest_dir, filename):
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.exists(safe_path):
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path

def execute_moves(base_dir, plan):
    """Creates directories and safely moves files, tracking file-system errors."""
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