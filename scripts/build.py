"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import platform
import sys

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

    cmd = [
        'smart-autosorter.spec',
        '--noconfirm',
        '--clean'
    ]
        
    PyInstaller.__main__.run(cmd)

if __name__ == '__main__':
    main()
