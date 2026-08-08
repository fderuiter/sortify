"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys


def update_binaries_and_manifest():
    """Copy real compiled sqlcipher3 binaries from the active environment to app/binaries/<platform>/sqlcipher3 and update manifest.json."""
    import hashlib
    import importlib.util
    import json
    import shutil
    from pathlib import Path

    if "pytest" in sys.modules:
        print("Running in tests. Skipping binaries and manifest update.")
        return

    spec = importlib.util.find_spec("sqlcipher3")
    if not spec or not spec.submodule_search_locations:
        print("Warning: sqlcipher3 not found in active environment. Cannot update binaries.")
        return

    sqlcipher_dir = Path(spec.submodule_search_locations[0])
    if not sqlcipher_dir.exists():
        print(f"Warning: sqlcipher3 directory {sqlcipher_dir} does not exist. Skipping update.")
        return

    system_platform = sys.platform
    if system_platform == "win32":
        platform_key = "windows"
    elif system_platform == "darwin":
        platform_key = "macos"
    else:
        platform_key = "linux"

    target_dir = Path("app") / "binaries" / platform_key / "sqlcipher3"

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    copied_files = []
    for path in sqlcipher_dir.glob("**/*"):
        if path.is_file():
            if "__pycache__" in path.parts:
                continue
            rel_path = path.relative_to(sqlcipher_dir)
            dest_path = target_dir / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest_path)
            copied_files.append(rel_path.as_posix())

    if system_platform == "win32":
        # Ensure sqlite3.dll is copied to the binaries directory if not already copied
        has_sqlite3_dll = any(f.lower() == "sqlite3.dll" for f in copied_files)
        if not has_sqlite3_dll:
            dll_src = None
            # 1. Search sys.prefix (virtualenv)
            if sys.prefix:
                candidate = Path(sys.prefix) / "Library" / "bin" / "sqlite3.dll"
                if candidate.exists():
                    dll_src = candidate
                else:
                    for root, dirs, files in os.walk(sys.prefix):
                        if any(p in root.lower() for p in ('site-packages/torch', 'site-packages/easyocr', 'site-packages/scipy')):
                            continue
                        for file in files:
                            if file.lower() == "sqlite3.dll":
                                dll_src = Path(root) / file
                                break
                        if dll_src:
                            break
            # 2. Search sys.base_prefix as a fallback if different
            if not dll_src and sys.base_prefix and sys.base_prefix != sys.prefix:
                candidate = Path(sys.base_prefix) / "Library" / "bin" / "sqlite3.dll"
                if candidate.exists():
                    dll_src = candidate
                else:
                    for root, dirs, files in os.walk(sys.base_prefix):
                        if any(p in root.lower() for p in ('site-packages/torch', 'site-packages/easyocr', 'site-packages/scipy')):
                            continue
                        for file in files:
                            if file.lower() == "sqlite3.dll":
                                dll_src = Path(root) / file
                                break
                        if dll_src:
                            break
            # 3. Search directory of python executable
            if not dll_src and sys.executable:
                exe_dir = os.path.dirname(sys.executable)
                if exe_dir:
                    candidate = Path(exe_dir) / "Library" / "bin" / "sqlite3.dll"
                    if candidate.exists():
                        dll_src = candidate
                    else:
                        candidate2 = Path(exe_dir) / "sqlite3.dll"
                        if candidate2.exists():
                            dll_src = candidate2
            if dll_src and dll_src.exists():
                dest_path = target_dir / "sqlite3.dll"
                shutil.copy2(dll_src, dest_path)
                copied_files.append("sqlite3.dll")
                print(f"Manually copied required Windows dependency sqlite3.dll from {dll_src} to {dest_path}")

    manifest_path = Path("app") / "binaries" / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = {}

    platform_hashes = {}
    for rel_path_str in copied_files:
        file_path = target_dir / rel_path_str
        hasher = hashlib.sha256()

        is_text_file = file_path.suffix in (".py", ".pyi", ".typed")
        if is_text_file:
            with open(file_path, "r", encoding="utf-8-sig", newline=None) as fh:
                content = fh.read()
            normalized_bytes = content.replace("\r\n", "\n").encode("utf-8")
            hasher.update(normalized_bytes)
        else:
            with open(file_path, "rb") as fh:
                while chunk := fh.read(8192):
                    hasher.update(chunk)

        actual_hash = hasher.hexdigest()
        platform_hashes[f"sqlcipher3/{rel_path_str}"] = actual_hash

    manifest[platform_key] = platform_hashes

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, sort_keys=True)

    print(f"Successfully copied real compiled binaries and updated manifest for platform: {platform_key}")


def main():
    """Build the standalone executable."""
    import importlib.util

    import PyInstaller.__main__

    is_lite = "--lite" in sys.argv
    if is_lite:
        sys.argv.remove("--lite")
        os.environ["LITE_BUILD"] = "1"
        print(
            "Lite profile enabled. Heavy ML packages will be excluded from the build."
        )
    else:
        print("Verifying machine learning packages in active environment...")
        ml_packages = [
            ("PyTorch", "torch"),
            ("EasyOCR", "easyocr"),
            ("Transformers", "transformers"),
            ("Scikit-Learn", "sklearn"),
            ("llama-cpp-python", "llama_cpp"),
            ("ONNX Runtime", "onnxruntime"),
            ("NumPy", "numpy"),
            ("Pandas", "pandas"),
            ("Pillow", "PIL"),
        ]
        for name, imp in ml_packages:
            try:
                spec = importlib.util.find_spec(imp)
                if spec is None:
                    raise ImportError()
            except (ImportError, ValueError, AttributeError, TypeError):
                print(f"Error: Missing machine learning package: {name}")
                sys.exit(1)

    print("Verifying SQLCipher in active environment...")
    spec = importlib.util.find_spec("sqlcipher3")
    if not spec or not spec.submodule_search_locations:
        print(
            "Error: sqlcipher3 not found in active environment. Please ensure dependencies are installed."
        )
        sys.exit(1)

    update_binaries_and_manifest()

    cmd = ["smart-autosorter.spec", "--noconfirm", "--clean"]

    PyInstaller.__main__.run(cmd)


if __name__ == "__main__":
    main()
