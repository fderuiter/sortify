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

# Explicitly bundle offline_bundle models and EasyOCR weights
offline_bundle_src = 'offline_bundle'
if os.path.exists(offline_bundle_src):
    for root, dirs, files in os.walk(offline_bundle_src):
        for file in files:
            abs_file_path = os.path.abspath(os.path.join(root, file))
            rel_sub = os.path.relpath(root, offline_bundle_src)
            if rel_sub == '.':
                dest_dir = 'offline_bundle'
            else:
                dest_dir = os.path.join('offline_bundle', rel_sub)
            datas.append((abs_file_path, dest_dir))

# On Windows, find and bundle any dependent OpenSSL/SQLCipher DLLs from the active Python or virtualenv environments
if (platform.system().lower() == "windows" or sys.platform == "win32") and "pytest" not in sys.modules:
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
    path_dirs = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cleaned = d.strip().strip('"')
        if cleaned:
            try:
                cleaned_lower = cleaned.lower()
                is_candidate_dir = (
                    "openssl" in cleaned_lower
                    or "ssl" in cleaned_lower
                    or "sqlcipher" in cleaned_lower
                    or "sqlite" in cleaned_lower
                    or "git" in cleaned_lower
                    or "python" in cleaned_lower
                    or "venv" in cleaned_lower
                    or "site-packages" in cleaned_lower
                )
                p_abs = os.path.abspath(cleaned).lower().replace('\\', '/')
                is_sys_dir = (
                    "system32" in p_abs
                    or "syswow64" in p_abs
                    or p_abs == "c:/windows"
                    or p_abs.startswith("c:/windows/")
                )
                if is_candidate_dir and not is_sys_dir and os.path.isdir(cleaned):
                    path_dirs.append(cleaned)
            except Exception:
                pass
    for path_dir in path_dirs:
        if path_dir not in search_dirs:
            search_dirs.append(path_dir)
            
    # Also add common/standard Windows OpenSSL installation directories
    common_openssl_dirs = [
        "C:\\Program Files\\OpenSSL-Win64\\bin",
        "C:\\Program Files\\OpenSSL\\bin",
        "C:\\Program Files\\OpenSSL-Win64",
        "C:\\Program Files\\OpenSSL",
        "C:\\OpenSSL-Win64\\bin",
        "C:\\OpenSSL-Win64",
        "C:\\Program Files\\Common Files\\SSL",
    ]
    for cod in common_openssl_dirs:
        if os.path.isdir(cod) and cod not in search_dirs:
            search_dirs.append(cod)
                
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
excludes = ['tkinter', 'tcl', 'tk', '_tkinter', 'sqlite3', '_sqlite3']
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
    safety_keywords = ("weight", "bin", "model", "checkpoint", "offline_bundle", "easyocr")
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


# Prevent any standard non-cryptographic sqlite binaries from being bundled
def is_standard_sqlite_binary(dest_name, src_path):
    dest_lower = dest_name.lower().replace('\\', '/')
    src_lower = src_path.lower().replace('\\', '/')
    
    # Identify any standard sqlite3 binary files
    if any(term in dest_lower for term in ('sqlite3', '_sqlite3')):
        # Allow it only if it originates from sqlcipher3 or app/binaries
        if 'sqlcipher3' in src_lower or 'app/binaries' in src_lower or 'app_binaries' in src_lower:
            return False
            
        # Build list of virtualenv directories to check
        venv_dirs = []
        v_env = os.environ.get("VIRTUAL_ENV")
        if v_env:
            venv_dirs.append(os.path.abspath(v_env))
        if "pytest" not in sys.modules:
            local_venv = os.path.abspath(os.path.join(os.path.dirname(__file__) if '__file__' in locals() else '.', ".venv"))
            if os.path.exists(local_venv) and local_venv not in venv_dirs:
                venv_dirs.append(local_venv)
        if sys.prefix and os.path.abspath(sys.prefix) not in venv_dirs:
            venv_dirs.append(os.path.abspath(sys.prefix))
            
        for vd in venv_dirs:
            prefix_lower = vd.lower().replace('\\', '/')
            if prefix_lower in src_lower:
                return False
                
        if sys.base_prefix:
            base_lower = sys.base_prefix.lower().replace('\\', '/')
            if base_lower in src_lower:
                return True
                
        return True
    return False


# Find and preserve the custom sqlite3.dll path if available to redirect standard dependencies
custom_sqlite3_dll = None
if "pytest" not in sys.modules:
    if sqlcipher_spec and sqlcipher_spec.submodule_search_locations:
        sqlcipher_dir = sqlcipher_spec.submodule_search_locations[0]
        for root, dirs, files in os.walk(sqlcipher_dir):
            for file in files:
                if file.lower() == "sqlite3.dll":
                    custom_sqlite3_dll = os.path.abspath(os.path.join(root, file))
                    break
            if custom_sqlite3_dll:
                break

    if not custom_sqlite3_dll:
        # Build list of virtualenv directories to search
        venv_dirs = []
        v_env = os.environ.get("VIRTUAL_ENV")
        if v_env:
            venv_dirs.append(os.path.abspath(v_env))
        local_venv = os.path.abspath(os.path.join(os.path.dirname(__file__) if '__file__' in locals() else '.', ".venv"))
        if os.path.exists(local_venv) and local_venv not in venv_dirs:
            venv_dirs.append(local_venv)
        if sys.prefix and os.path.abspath(sys.prefix) not in venv_dirs:
            venv_dirs.append(os.path.abspath(sys.prefix))
            
        for vd in venv_dirs:
            prefix_lower = vd.lower().replace('\\', '/')
            base_lower = sys.base_prefix.lower().replace('\\', '/') if sys.base_prefix else None
            
            # Check standard candidate paths inside virtualenv first
            for sub in ["Library/bin", "Scripts", "Lib/site-packages/sqlcipher3"]:
                candidate_dir = os.path.join(vd, sub.replace("/", os.sep))
                if os.path.isdir(candidate_dir):
                    candidate_path = os.path.abspath(os.path.join(candidate_dir, "sqlite3.dll"))
                    if os.path.exists(candidate_path):
                        custom_sqlite3_dll = candidate_path
                        break
            if custom_sqlite3_dll:
                break
                
            for root, dirs, files in os.walk(vd):
                # Filter out heavy directories in-place to prevent os.walk from recursing into them
                dirs[:] = [d for d in dirs if d.lower() not in ("torch", "easyocr", "scipy", "transformers", "numpy", "pandas", "sklearn", "matplotlib", "jinja2", "anyio", "aiohttp", "pydantic", "pydantic_core")]
                # Skip some common heavy directories to make it faster
                if any(p in root.lower().replace('\\', '/') for p in ('site-packages/torch', 'site-packages/easyocr', 'site-packages/scipy')):
                    continue
                for file in files:
                    if file.lower() == "sqlite3.dll":
                        candidate_path = os.path.abspath(os.path.join(root, file))
                        cand_lower = candidate_path.lower().replace('\\', '/')
                        # Make sure it's not from base_prefix
                        if base_lower and base_lower in cand_lower and base_lower != prefix_lower:
                            continue
                        custom_sqlite3_dll = candidate_path
                        break
                if custom_sqlite3_dll:
                    break
            if custom_sqlite3_dll:
                break

    if not custom_sqlite3_dll:
        # Walk the app/binaries directory to find the custom sqlite3.dll as a reliable fallback
        app_bin_dir = os.path.join('app', 'binaries')
        if os.path.exists(app_bin_dir):
            for root, dirs, files in os.walk(app_bin_dir):
                for file in files:
                    if file.lower() == "sqlite3.dll":
                        custom_sqlite3_dll = os.path.abspath(os.path.join(root, file))
                        break
                if custom_sqlite3_dll:
                    break

new_binaries = []
for x in a.binaries:
    dest_name, src_path = x[0], x[1]
    dest_lower = dest_name.lower().replace('\\', '/')
    src_lower = src_path.lower().replace('\\', '/')
    
    if is_tcl_tk_asset(dest_name) or is_prunable_asset(dest_name):
        continue
        
    # Redirect standard sqlite3.dll to our custom one instead of discarding it to satisfy pefile/dependency requirements
    if dest_lower.endswith("sqlite3.dll") and custom_sqlite3_dll:
        if not ('sqlcipher3' in src_lower or 'app/binaries' in src_lower or 'app_binaries' in src_lower):
            print(f"Redirecting standard sqlite3.dll dependency {src_path} -> custom {custom_sqlite3_dll}")
            new_binaries.append((dest_name, custom_sqlite3_dll, x[2]))
            continue

    if is_standard_sqlite_binary(dest_name, src_path):
        print(f"Filtering out standard sqlite binary: {dest_name} from {src_path}")
        continue
        
    new_binaries.append(x)

a.binaries = new_binaries
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
    console=True if os.environ.get("LITE_BUILD") == "1" else False,  # enabled for debugging/smoke testing on GHA
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
