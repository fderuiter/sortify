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
is_lite = os.environ.get("LITE_BUILD") == "1"
ml_packages = []
if not is_lite:
    ml_packages = [
        'torch', 'easyocr', 'transformers', 'sklearn', 'llama_cpp',
        'onnxruntime', 'numpy', 'pandas', 'PIL'
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
            
            # Identify platform-specific binary extensions (.dll, .dylib, .so, .pyd)
            file_lower = file.lower()
            if file_lower.endswith(('.dll', '.dylib', '.so', '.pyd')) or '.so.' in file_lower:
                binaries.append((abs_file_path, dest_dir))
            else:
                datas.append((abs_file_path, dest_dir))
else:
    print("Warning: sqlcipher3 not found in active environment.")

# Explicitly bundle local precompiled platform libraries for offline usage
app_binaries_src = os.path.join('app', 'binaries')
if os.path.exists(app_binaries_src):
    for root, dirs, files in os.walk(app_binaries_src):
        for file in files:
            abs_file_path = os.path.abspath(os.path.join(root, file))
            # Determine destination subdirectory in the package (under app/binaries)
            rel_sub = os.path.relpath(root, app_binaries_src)
            if rel_sub == '.':
                dest_dir = os.path.join('app', 'binaries')
            else:
                dest_dir = os.path.join('app', 'binaries', rel_sub)
            datas.append((abs_file_path, dest_dir))

# On Windows, find and bundle any dependent OpenSSL/SQLCipher DLLs from the active Python or virtualenv environments
if platform.system().lower() == "windows" or sys.platform == "win32":
    search_dirs = [
        sys.prefix,
        sys.base_prefix,
        os.path.dirname(sys.executable),
    ]
    # Add Library/bin, DLLs and Scripts subdirectories of sys.prefix / sys.base_prefix if they exist
    for sd in list(search_dirs):
        if sd:
            lib_bin = os.path.join(sd, "Library", "bin")
            if os.path.isdir(lib_bin):
                search_dirs.append(lib_bin)
            dlls_dir = os.path.join(sd, "DLLs")
            if os.path.isdir(dlls_dir):
                search_dirs.append(dlls_dir)
            scripts_dir = os.path.join(sd, "Scripts")
            if os.path.isdir(scripts_dir):
                search_dirs.append(scripts_dir)
                
    found_dlls = set()
    dll_patterns = ["libcrypto", "libssl", "sqlcipher", "libsqlcipher"]
    
    # 1. Check recursively inside the installed sqlcipher3 package directory itself for any DLLs
    if sqlcipher_spec and sqlcipher_spec.submodule_search_locations:
        sqlcipher_dir = sqlcipher_spec.submodule_search_locations[0]
        for root, dirs, files in os.walk(sqlcipher_dir):
            for file in files:
                file_lower = file.lower()
                if file_lower.endswith(".dll"):
                    dll_path = os.path.abspath(os.path.join(root, file))
                    if dll_path not in found_dlls:
                        found_dlls.add(dll_path)
                        print(f"Bundling required Windows dependency DLL from sqlcipher3 package: {dll_path}")
                        binaries.append((dll_path, '.'))
                        binaries.append((dll_path, 'sqlcipher3'))

    # 2. Check the standard search directories for matching DLL patterns
    for s_dir in search_dirs:
        if not s_dir or not os.path.isdir(s_dir):
            continue
        try:
            files_in_dir = os.listdir(s_dir)
        except Exception as scan_err:
            print(f"Warning: Could not list directory {s_dir} during DLL discovery: {scan_err}")
            continue
        for file in files_in_dir:
            file_lower = file.lower()
            if file_lower.endswith(".dll") and any(pat in file_lower for pat in dll_patterns):
                dll_path = os.path.abspath(os.path.join(s_dir, file))
                if dll_path not in found_dlls:
                    found_dlls.add(dll_path)
                    print(f"Bundling required Windows dependency DLL: {dll_path}")
                    # Place in root and sqlcipher3 to be absolutely certain it's resolved
                    binaries.append((dll_path, '.'))
                    binaries.append((dll_path, 'sqlcipher3'))

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

# Exclude non-essential heavy development or test folders, keeping core weights/models/binaries
def is_prunable_asset(name):
    name_lower = name.lower().replace('\\', '/')
    parts = name_lower.split('/')
    
    # Safety Rule: Core weights, model files, and crucial bin targets must NEVER be pruned.
    safety_keywords = ("weight", "bin", "model", "checkpoint")
    if any(sk in name_lower for sk in safety_keywords):
        return False
        
    # Non-essential development/test folder names
    prune_folders = {
        'tests', 'test', 'include', 'cmake', 'headers', 'examples', 
        'benchmarks', 'docs', 'documentation', 'test_data', 'testing'
    }
    
    # If any parent directory matches a prune folder
    if any(p in prune_folders for p in parts):
        return True
        
    # Exclude files with non-essential extensions (development header files, markdown docs, text files)
    # as long as they don't contain safety keywords
    if name_lower.endswith(('.h', '.hpp', '.c', '.cpp', '.cmake', '.rst', '.md')):
        return True
        
    return False

a.binaries = [x for x in a.binaries if not is_tcl_tk_asset(x[0]) and not is_prunable_asset(x[0])]
a.datas = [x for x in a.datas if not is_tcl_tk_asset(x[0]) and not is_prunable_asset(x[0])]

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
