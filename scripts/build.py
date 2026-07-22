"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys

import PyInstaller.__main__


def main():
    """Build the standalone executable."""
    # Locate precompiled sqlcipher3-wheels from the active virtual environment
    import importlib.util
    import shutil
    print("Locating SQLCipher shared libraries in active environment...")
    spec = importlib.util.find_spec("sqlcipher3")
    if not spec or not spec.submodule_search_locations:
        print("Error: sqlcipher3 not found in active environment. Please ensure dependencies are installed.")
        sys.exit(1)
        
    src_dir = spec.submodule_search_locations[0]
    sqlcipher_dir = os.path.join(os.getcwd(), "build_tmp", "sqlcipher")
    if os.path.exists(sqlcipher_dir):
        shutil.rmtree(sqlcipher_dir)
    os.makedirs(sqlcipher_dir, exist_ok=True)
    
    dest_dir = os.path.join(sqlcipher_dir, "sqlcipher3")
    print(f"Bundling sqlcipher3 from {src_dir} to {dest_dir}...")
    shutil.copytree(src_dir, dest_dir)

    cmd = [
        'smart-autosorter.spec',
        '--noconfirm',
        '--clean'
    ]
        
    PyInstaller.__main__.run(cmd)

if __name__ == '__main__':
    main()
