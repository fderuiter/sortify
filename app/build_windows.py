"""Build script for the smart autosorter application on Windows."""

import os
import sys

import customtkinter
import PyInstaller.__main__


def main():
    """Build the standalone executable for Windows."""
    ctk_path = os.path.dirname(customtkinter.__file__)

    cmd = [
        'app/main.py',
        '--name', 'smart-autosorter',
        '--noconfirm',
        '--onedir',
        '--windowed',
        f'--add-data={ctk_path};customtkinter',
        '--clean'
    ]
    
    PyInstaller.__main__.run(cmd)

if __name__ == '__main__':
    main()
