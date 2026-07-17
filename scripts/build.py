"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import platform
import sys

import customtkinter
import PyInstaller.__main__


def main():
    """Build the standalone executable."""
    import shutil
    import subprocess
    
    # Download precompiled sqlcipher3-wheels for the current platform
    print("Downloading precompiled SQLCipher shared libraries...")
    sqlcipher_dir = os.path.join(os.getcwd(), "build_tmp", "sqlcipher")
    if os.path.exists(sqlcipher_dir):
        shutil.rmtree(sqlcipher_dir)
    os.makedirs(sqlcipher_dir, exist_ok=True)
    
    subprocess.run([
        sys.executable, "-m", "pip", "install", 
        "sqlcipher3-wheels==0.5.7", 
        "--target", sqlcipher_dir,
        "--only-binary=:all:",
        "--no-cache-dir"
    ], check=True)

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
        f'--add-data={os.path.join(sqlcipher_dir, "sqlcipher3")}{path_separator}sqlcipher3',
        '--clean'
    ]
    
    for b in binaries:
        cmd.extend(['--add-binary', b])
        
    PyInstaller.__main__.run(cmd)

if __name__ == '__main__':
    main()
