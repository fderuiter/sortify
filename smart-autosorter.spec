# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import platform
# Removed customtkinter
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Core machine learning and NLP dependencies required for offline processing
ml_packages = [
    'torch',
    'transformers',
    'sentence_transformers',
    'llama_cpp',
    'huggingface_hub',
    'filelock',
    'regex',
    'tqdm',
    'numpy',
    'packaging',
    'yaml',
    'safetensors'
]

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

# No special nicegui asset bundling needed by default

# Bundle secure database shared libraries (bundled by build.py from local env)
sqlcipher_dir = os.path.join(os.getcwd(), "build_tmp", "sqlcipher", "sqlcipher3")
if os.path.exists(sqlcipher_dir):
    datas.append((sqlcipher_dir, 'sqlcipher3'))
else:
    print(f"Warning: sqlcipher3 not found at {sqlcipher_dir}")

# Bundle platform-specific Tcl/Tk dylibs on macOS
if platform.system() == "Darwin":
    python_lib_dir = os.path.join(sys.base_prefix, 'lib')
    if os.path.exists(os.path.join(python_lib_dir, 'libtcl9.0.dylib')):
        binaries.append((os.path.join(python_lib_dir, 'libtcl9.0.dylib'), '.'))
    if os.path.exists(os.path.join(python_lib_dir, 'libtcl9tk9.0.dylib')):
        binaries.append((os.path.join(python_lib_dir, 'libtcl9tk9.0.dylib'), '.'))

a = Analysis(
    ['app/main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

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
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='smart-autosorter',
)
