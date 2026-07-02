import PyInstaller.__main__
import customtkinter
import os
import sys

def main():
    """Build the standalone executable."""
    ctk_path = os.path.dirname(customtkinter.__file__)
    
    # Bundle tcl/tk from virtual environment if they exist
    python_lib_dir = os.path.join(sys.base_prefix, 'lib')
    
    binaries = []
    if os.path.exists(os.path.join(python_lib_dir, 'libtcl9.0.so')):
        binaries.append(f"{os.path.join(python_lib_dir, 'libtcl9.0.so')}:.")
    if os.path.exists(os.path.join(python_lib_dir, 'libtcl9tk9.0.so')):
        binaries.append(f"{os.path.join(python_lib_dir, 'libtcl9tk9.0.so')}:.")

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
