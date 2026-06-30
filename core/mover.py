# core/mover.py
import os
import shutil

def get_safe_path(dest_dir, filename):
    """Ensures no files are overwritten by appending numbers if it exists."""
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.exists(safe_path):
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path

def execute_moves(base_dir, plan):
    """Creates directories and safely moves files."""
    for folder, files in plan.items():
        if not files: 
            continue
            
        folder_path = os.path.join(base_dir, folder)
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
            
        for item in files:
            source_path = os.path.join(base_dir, item)
            dest_path = get_safe_path(folder_path, item)
            try:
                shutil.move(source_path, dest_path)
            except Exception as e:
                print(f"Error moving {item}: {e}")