"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import platform
import sys

import customtkinter
import PyInstaller.__main__


def main():
    """Build the standalone executable."""
    ctk_path = os.path.dirname(customtkinter.__file__)
    
    # Bundle tcl/tk from virtual environment if they exist (macOS uses dylib)
    python_lib_dir = os.path.join(sys.base_prefix, 'lib')
    
    binaries = []
    if platform.system() == "Darwin":
        if os.path.exists(os.path.join(python_lib_dir, 'libtcl9.0.dylib')):
            binaries.append(f"{os.path.join(python_lib_dir, 'libtcl9.0.dylib')}:.")
        if os.path.exists(os.path.join(python_lib_dir, 'libtcl9tk9.0.dylib')):
            binaries.append(f"{os.path.join(python_lib_dir, 'libtcl9tk9.0.dylib')}:.")

    # Semicolon-separated paths on Windows and colon-separated on macOS and Linux
    if platform.system() == "Windows":
        path_separator = ";"
    else:
        path_separator = ":"

    cmd = [
        'app/main.py',
        '--name', 'smart-autosorter',
        '--noconfirm',
        '--onedir',
        '--windowed',
        f'--add-data={ctk_path}{path_separator}customtkinter',
        '--clean'
    ]
    
    for b in binaries:
        cmd.extend(['--add-binary', b])
        
    PyInstaller.__main__.run(cmd)

if __name__ == '__main__':
    main()
