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

# Bundle secure database shared libraries directly from the active virtual environment using collect_all
try:
    sqlcipher_datas, sqlcipher_binaries, sqlcipher_hiddenimports = collect_all('sqlcipher3')
    datas.extend(sqlcipher_datas)
    binaries.extend(sqlcipher_binaries)
    hiddenimports.extend(sqlcipher_hiddenimports)
except Exception as e:
    print(f"Warning: Could not collect sqlcipher3 package via collect_all: {e}")

# Explicitly collect any platform-specific binary extensions and Python source/data files from the sqlcipher3 package directory
# to ensure they are never missed by collect_all (e.g. _sqlite3.pyd and python modules on Windows)
sqlcipher_spec = importlib.util.find_spec("sqlcipher3")
if sqlcipher_spec and sqlcipher_spec.submodule_search_locations:
    sqlcipher_dir = sqlcipher_spec.submodule_search_locations[0]
    for root, dirs, files in os.walk(sqlcipher_dir):
        for file in files:
            abs_file_path = os.path.abspath(os.path.join(root, file))
            rel_path = os.path.relpath(abs_file_path, sqlcipher_dir)
            dest_dir = os.path.join('sqlcipher3', os.path.dirname(rel_path))
            file_lower = file.lower()
            if file_lower.endswith(('.dll', '.dylib', '.so', '.pyd')) or '.so.' in file_lower:
                # Prevent duplicates
                dup = False
                for b_src, b_dst in binaries:
                    if os.path.abspath(b_src) == abs_file_path and b_dst == dest_dir:
                        dup = True
                        break
                if not dup:
                    print(f"Explicitly bundling sqlcipher3 binary extension: {abs_file_path} -> {dest_dir}")
                    binaries.append((abs_file_path, dest_dir))
            else:
                # Prevent duplicates
                dup = False
                for d_src, d_dst in datas:
                    if os.path.abspath(d_src) == abs_file_path and d_dst == dest_dir:
                        dup = True
                        break
                if not dup:
                    print(f"Explicitly bundling sqlcipher3 data/source asset: {abs_file_path} -> {dest_dir}")
                    datas.append((abs_file_path, dest_dir))

# On Windows, find and bundle any dependent OpenSSL/SQLCipher DLLs from the active Python or virtualenv environments
if platform.system().lower() == "windows" or sys.platform == "win32":
    sqlcipher_spec = importlib.util.find_spec("sqlcipher3")
    search_dirs = []
    # Prioritize active virtual environment (sys.prefix) and its subdirectories
    if sys.prefix:
        search_dirs.append(sys.prefix)
        for sub in ["Library/bin", "DLLs", "Scripts"]:
            p = os.path.join(sys.prefix, sub.replace("/", os.sep))
            if os.path.isdir(p):
                search_dirs.append(p)
                
    # Fallback to base python prefix (sys.base_prefix) and its subdirectories only if different
    if sys.base_prefix and sys.base_prefix != sys.prefix:
        search_dirs.append(sys.base_prefix)
        for sub in ["Library/bin", "DLLs", "Scripts"]:
            p = os.path.join(sys.base_prefix, sub.replace("/", os.sep))
            if os.path.isdir(p):
                search_dirs.append(p)
                
    # Finally, check executable directory
    exe_dir = os.path.dirname(sys.executable)
    if exe_dir and exe_dir not in search_dirs:
        search_dirs.append(exe_dir)

    # Also add directories from system PATH to find system-installed OpenSSL DLLs on GHA Windows runner
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        if path_dir and os.path.isdir(path_dir) and path_dir not in search_dirs:
            search_dirs.append(path_dir)
                
    found_dll_names = set()
    found_dlls = set()
    dll_patterns = ["libcrypto", "libssl", "sqlcipher", "libsqlcipher", "sqlite3"]
    
    # 1. Check recursively inside the installed sqlcipher3 package directory itself for any DLLs
    if sqlcipher_spec and sqlcipher_spec.submodule_search_locations:
        sqlcipher_dir = sqlcipher_spec.submodule_search_locations[0]
        for root, dirs, files in os.walk(sqlcipher_dir):
            for file in files:
                file_lower = file.lower()
                if file_lower.endswith(".dll"):
                    dll_path = os.path.abspath(os.path.join(root, file))
                    if file_lower not in found_dll_names:
                        found_dll_names.add(file_lower)
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
                if file_lower not in found_dll_names:
                    found_dll_names.add(file_lower)
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


def is_standard_sqlite_asset(dest, src):
    dest_lower = dest.lower().replace('\\', '/')
    src_lower = src.lower().replace('\\', '/')
    
    # We want to keep SQLCipher's own sqlite3 / _sqlite3 assets
    if "sqlcipher3" in dest_lower or "sqlcipher3" in src_lower:
        return False
        
    filename_dest = os.path.basename(dest_lower)
    filename_src = os.path.basename(src_lower)
    
    # Filter out standard SQLite binaries / DLLs
    for pat in ("_sqlite3", "sqlite3", "libsqlite3"):
        if pat in filename_dest or pat in filename_src:
            return True
        
    # Filter out standard sqlite3 package folder in datas/binaries
    dest_parts = dest_lower.split('/')
    src_parts = src_lower.split('/')
    if "sqlite3" in dest_parts or "_sqlite3" in dest_parts:
        return True
    if "sqlite3" in src_parts or "_sqlite3" in src_parts:
        return True
        
    return False


a.binaries = [x for x in a.binaries if not is_tcl_tk_asset(x[0]) and not is_prunable_asset(x[0]) and not is_standard_sqlite_asset(x[0], x[1])]
a.datas = [x for x in a.datas if not is_tcl_tk_asset(x[0]) and not is_prunable_asset(x[0]) and not is_standard_sqlite_asset(x[0], x[1])]

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
