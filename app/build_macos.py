"""Build script for the smart autosorter application on macOS."""

import os
import sys

import customtkinter
import PyInstaller.__main__


def main():
    """Build the standalone executable for macOS."""
    ctk_path = os.path.dirname(customtkinter.__file__)
    
    # Bundle tcl/tk from virtual environment if they exist (macOS uses dylib)
    python_lib_dir = os.path.join(sys.base_prefix, 'lib')
    
    binaries = []
    if os.path.exists(os.path.join(python_lib_dir, 'libtcl9.0.dylib')):
        binaries.append(f"{os.path.join(python_lib_dir, 'libtcl9.0.dylib')}:.")
    if os.path.exists(os.path.join(python_lib_dir, 'libtcl9tk9.0.dylib')):
        binaries.append(f"{os.path.join(python_lib_dir, 'libtcl9tk9.0.dylib')}:.")

    cmd = [
        'app/main.py',
        '--name', 'smart-autosorter',
        '--noconfirm',
        '--onedir',
        '--windowed',
        f'--add-data={ctk_path}:customtkinter',
        '--clean'
    ]
    
    for b in binaries:
        cmd.extend(['--add-binary', b])
        
    PyInstaller.__main__.run(cmd)

if __name__ == '__main__':
    main()
