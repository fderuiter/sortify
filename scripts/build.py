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
        print(
            "Warning: sqlcipher3 not found in active environment. Cannot update binaries."
        )
        return

    sqlcipher_dir = Path(spec.submodule_search_locations[0])
    if not sqlcipher_dir.exists():
        print(
            f"Warning: sqlcipher3 directory {sqlcipher_dir} does not exist. Skipping update."
        )
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
        # Ensure all required Windows DLLs (sqlite3, OpenSSL) are copied to the binaries directory if not already copied
        dll_patterns = ["libcrypto", "libssl", "sqlcipher", "libsqlcipher", "sqlite3"]

        # We will build a list of missing patterns to search for
        for pat in dll_patterns:
            # Check if we already have a copied DLL file containing this pattern (case-insensitive)
            already_copied = any(
                f.lower().endswith(".dll") and pat in f.lower() for f in copied_files
            )
            if not already_copied:
                # We need to search for a DLL matching this pattern
                # Let's find all matching DLLs in the prioritized search paths
                dll_srcs = []

                # List of search paths
                venv_dirs = []
                v_env = os.environ.get("VIRTUAL_ENV")
                if v_env:
                    venv_dirs.append(Path(v_env))
                local_venv = Path(__file__).resolve().parent.parent / ".venv"
                if local_venv.exists() and local_venv not in venv_dirs:
                    venv_dirs.append(local_venv)
                if sys.prefix and Path(sys.prefix) not in venv_dirs:
                    venv_dirs.append(Path(sys.prefix))

                found_for_pattern = False

                # Check candidate locations in prioritized venv directories
                for vd in venv_dirs:
                    for sub in [
                        Path("."),
                        Path("Library") / "bin",
                        Path("Scripts"),
                        Path("Lib") / "site-packages" / "sqlcipher3",
                    ]:
                        candidate_dir = vd / sub
                        if candidate_dir.exists():
                            for f in os.listdir(candidate_dir):
                                if f.lower().endswith(".dll") and pat in f.lower():
                                    dll_srcs.append(candidate_dir / f)
                                    found_for_pattern = True
                    if found_for_pattern:
                        break

                    # If not found in candidate paths, walk the venv directory recursively
                    for root, dirs, files in os.walk(vd):
                        if any(
                            p in root.lower()
                            for p in (
                                "site-packages/torch",
                                "site-packages/easyocr",
                                "site-packages/scipy",
                            )
                        ):
                            continue
                        for file in files:
                            if file.lower().endswith(".dll") and pat in file.lower():
                                dll_srcs.append(Path(root) / file)
                                found_for_pattern = True
                        if found_for_pattern:
                            break
                    if found_for_pattern:
                        break

                # Search sys.base_prefix as a fallback if different
                if (
                    not found_for_pattern
                    and sys.base_prefix
                    and sys.base_prefix != sys.prefix
                ):
                    for sub in [
                        Path("."),
                        Path("Library") / "bin",
                        Path("DLLs"),
                        Path("Scripts"),
                    ]:
                        candidate_dir = Path(sys.base_prefix) / sub
                        if candidate_dir.exists():
                            for f in os.listdir(candidate_dir):
                                if f.lower().endswith(".dll") and pat in f.lower():
                                    dll_srcs.append(candidate_dir / f)
                                    found_for_pattern = True
                    if not found_for_pattern:
                        for root, dirs, files in os.walk(sys.base_prefix):
                            if any(
                                p in root.lower()
                                for p in (
                                    "site-packages/torch",
                                    "site-packages/easyocr",
                                    "site-packages/scipy",
                                )
                            ):
                                continue
                            for file in files:
                                if (
                                    file.lower().endswith(".dll")
                                    and pat in file.lower()
                                ):
                                    dll_srcs.append(Path(root) / file)
                                    found_for_pattern = True
                            if found_for_pattern:
                                break

                # Search directory of python executable
                if not found_for_pattern and sys.executable:
                    exe_dir = os.path.dirname(sys.executable)
                    if exe_dir:
                        for sub in [Path("."), Path("Library") / "bin", Path("DLLs")]:
                            candidate_dir = Path(exe_dir) / sub
                            if candidate_dir.exists():
                                for f in os.listdir(candidate_dir):
                                    if f.lower().endswith(".dll") and pat in f.lower():
                                        dll_srcs.append(candidate_dir / f)
                                        found_for_pattern = True
                        if not found_for_pattern:
                            for f in os.listdir(exe_dir):
                                if f.lower().endswith(".dll") and pat in f.lower():
                                    dll_srcs.append(Path(exe_dir) / f)
                                    found_for_pattern = True

                # Search standard OpenSSL installation paths on Windows as fallback
                if not found_for_pattern:
                    common_openssl_dirs = [
                        Path("C:/Program Files/OpenSSL-Win64/bin"),
                        Path("C:/Program Files/OpenSSL/bin"),
                        Path("C:/Program Files/OpenSSL-Win64"),
                        Path("C:/Program Files/OpenSSL"),
                        Path("C:/OpenSSL-Win64/bin"),
                        Path("C:/OpenSSL-Win64"),
                        Path("C:/Program Files/Common Files/SSL"),
                    ]
                    for cd in common_openssl_dirs:
                        if cd.exists():
                            try:
                                for f in os.listdir(cd):
                                    if f.lower().endswith(".dll") and pat in f.lower():
                                        dll_srcs.append(cd / f)
                                        found_for_pattern = True
                            except Exception:
                                continue
                            if found_for_pattern:
                                break

                # Search system PATH as a fallback (excluding System32 and Windows)
                if not found_for_pattern:
                    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
                        if path_dir and os.path.isdir(path_dir):
                            dir_lower = path_dir.lower()
                            if "system32" in dir_lower or "windows" in dir_lower:
                                continue
                            try:
                                for f in os.listdir(path_dir):
                                    if f.lower().endswith(".dll") and pat in f.lower():
                                        dll_srcs.append(Path(path_dir) / f)
                                        found_for_pattern = True
                            except Exception:
                                continue
                            if found_for_pattern:
                                break

                for src in dll_srcs:
                    if src.exists():
                        dest_path = target_dir / src.name
                        shutil.copy2(src, dest_path)
                        copied_files.append(src.name)
                        print(
                            f"Manually copied required Windows dependency {src.name} from {src} to {dest_path}"
                        )

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

    print(
        f"Successfully copied real compiled binaries and updated manifest for platform: {platform_key}"
    )


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
