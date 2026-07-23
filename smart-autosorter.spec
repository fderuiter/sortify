# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa

import importlib.util
import os
import platform
import sys

# Removed customtkinter
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Core machine learning and NLP dependencies required for offline processing
ml_packages = []

datas = []
binaries = []
hiddenimports = []

# Collect all dynamic libraries, weights, and hidden imports for ML packages
for pkg in ml_packages:
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
        datas.extend(pkg_datas)
        binaries.extend(pkg_binaries)
        hiddenimports.extend(pkg_hiddenimports)
    except Exception as e:
        print(f"Warning: Could not collect package {pkg}: {e}")

# Bundle nicegui static assets and dependencies
try:
    nicegui_datas, nicegui_binaries, nicegui_hiddenimports = collect_all('nicegui')
    datas.extend(nicegui_datas)
    binaries.extend(nicegui_binaries)
    hiddenimports.extend(nicegui_hiddenimports)
except Exception as e:
    print(f"Warning: Could not collect nicegui package: {e}")

# Bundle secure database shared libraries directly from the active virtual environment
sqlcipher_spec = importlib.util.find_spec("sqlcipher3")
if sqlcipher_spec and sqlcipher_spec.submodule_search_locations:
    sqlcipher_dir = sqlcipher_spec.submodule_search_locations[0]
    for root, dirs, files in os.walk(sqlcipher_dir):
        for file in files:
            abs_file_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_file_path, sqlcipher_dir)
            dest_dir = os.path.join('sqlcipher3', os.path.dirname(rel_path))
            
            # Identify platform-specific binary extensions (.dll, .dylib, .so)
            file_lower = file.lower()
            if file_lower.endswith(('.dll', '.dylib', '.so')) or '.so.' in file_lower:
                binaries.append((abs_file_path, dest_dir))
            else:
                datas.append((abs_file_path, dest_dir))
else:
    print("Warning: sqlcipher3 not found in active environment.")

is_lite = os.environ.get("LITE_BUILD") == "1"
excludes = ['tkinter', 'tcl', 'tk', '_tkinter']
if is_lite:
    excludes.extend([
        'torch', 'torchvision', 'triton', 'nvidia', 'easyocr', 'scipy',
        'sklearn', 'scikit-learn', 'pandas', 'cv2', 'numpy', 'skimage',
        'scikit-image', 'sympy', 'lxml', 'mypy', 'matplotlib'
    ])

a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Filter out Tcl/Tk components unconditionally to reduce bundle size on all platforms
def is_tcl_tk_asset(name):
    name_lower = name.lower().replace('\\', '/')
    parts = name_lower.split('/')
    for p in parts:
        if p in ('_tcl_data', '_tk_data', 'tcl', 'tk', 'tcl8', 'tk8', 'tcl9', 'tk9'):
            return True
        if p.startswith('libtcl') or p.startswith('libtk'):
            return True
        if p.startswith('tcl8') or p.startswith('tk8') or p.startswith('tcl9') or p.startswith('tk9'):
            return True
        if '_tkinter' in p:
            return True
        if p in ('tcl.framework', 'tk.framework'):
            return True
    return False

a.binaries = [x for x in a.binaries if not is_tcl_tk_asset(x[0])]
a.datas = [x for x in a.datas if not is_tcl_tk_asset(x[0])]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='smart-autosorter',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed mode
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='smart-autosorter',
)
