"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys


def update_binaries_and_manifest(system_platform=None, bypass_pytest_check=False):
    """Copy real compiled sqlcipher3 binaries from the active environment to app/binaries/<platform>/sqlcipher3 and update manifest.json."""
    import hashlib
    import importlib.util
    import json
    import shutil
    from pathlib import Path

    if "pytest" in sys.modules and not bypass_pytest_check:
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

    if system_platform is None:
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
                if "pytest" not in sys.modules:
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
                            try:
                                for f in os.listdir(candidate_dir):
                                    if f.lower().endswith(".dll") and pat in f.lower():
                                        dll_srcs.append(candidate_dir / f)
                                        found_for_pattern = True
                            except Exception:
                                pass
                    if found_for_pattern:
                        break

                    # If not found in candidate paths, walk the venv directory recursively
                    for root, dirs, files in os.walk(vd):
                        # Filter out heavy directories in-place to prevent os.walk from recursing into them
                        dirs[:] = [
                            d
                            for d in dirs
                            if d.lower()
                            not in (
                                "torch",
                                "easyocr",
                                "scipy",
                                "transformers",
                                "numpy",
                                "pandas",
                                "sklearn",
                                "matplotlib",
                                "jinja2",
                                "anyio",
                                "aiohttp",
                                "pydantic",
                                "pydantic_core",
                            )
                        ]

                        root_normalized = root.lower().replace("\\", "/")
                        if any(
                            p in root_normalized
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
                            try:
                                for f in os.listdir(candidate_dir):
                                    if f.lower().endswith(".dll") and pat in f.lower():
                                        dll_srcs.append(candidate_dir / f)
                                        found_for_pattern = True
                            except Exception:
                                pass
                    if not found_for_pattern:
                        for root, dirs, files in os.walk(sys.base_prefix):
                            # Filter out heavy directories in-place to prevent os.walk from recursing into them
                            dirs[:] = [
                                d
                                for d in dirs
                                if d.lower()
                                not in (
                                    "torch",
                                    "easyocr",
                                    "scipy",
                                    "transformers",
                                    "numpy",
                                    "pandas",
                                    "sklearn",
                                    "matplotlib",
                                    "jinja2",
                                    "anyio",
                                    "aiohttp",
                                    "pydantic",
                                    "pydantic_core",
                                )
                            ]

                            root_normalized = root.lower().replace("\\", "/")
                            if any(
                                p in root_normalized
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
                                try:
                                    for f in os.listdir(candidate_dir):
                                        if (
                                            f.lower().endswith(".dll")
                                            and pat in f.lower()
                                        ):
                                            dll_srcs.append(candidate_dir / f)
                                            found_for_pattern = True
                                except Exception:
                                    pass
                        if not found_for_pattern:
                            try:
                                for f in os.listdir(exe_dir):
                                    if f.lower().endswith(".dll") and pat in f.lower():
                                        dll_srcs.append(Path(exe_dir) / f)
                                        found_for_pattern = True
                            except Exception:
                                pass

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

                # Search system PATH as a fallback (excluding standard system directories)
                if not found_for_pattern:
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
                                p_abs = (
                                    os.path.abspath(cleaned).lower().replace("\\", "/")
                                )
                                is_sys_dir = (
                                    "system32" in p_abs
                                    or "syswow64" in p_abs
                                    or p_abs == "c:/windows"
                                    or p_abs.startswith("c:/windows/")
                                )
                                if (
                                    is_candidate_dir
                                    and not is_sys_dir
                                    and os.path.isdir(cleaned)
                                ):
                                    path_dirs.append(cleaned)
                            except Exception:
                                pass
                    for path_dir in path_dirs:
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


def download_and_prepare_weights():
    """Ensure that the build process downloads and bundles all necessary model weights."""
    import hashlib
    import shutil
    import urllib.request
    import zipfile
    from pathlib import Path

    print("Preparing and downloading model weights for offline execution...")
    offline_bundle = Path("offline_bundle")
    model_dir = offline_bundle / "model"
    easyocr_dir = offline_bundle / "easyocr"

    model_dir.mkdir(parents=True, exist_ok=True)
    easyocr_dir.mkdir(parents=True, exist_ok=True)

    # Helper function to download file safely with User-Agent header
    def download_file(url, dest_path):
        print(f"Downloading {url} to {dest_path}...")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as response, open(dest_path, "wb") as out_file:
            shutil.copyfileobj(response, out_file)

    # 1. Download/prepare sentence-transformers model
    hf_base_url = "".join(
        ["https://", "huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main"]
    )
    files_to_download = {
        "model.onnx": f"{hf_base_url}/onnx/model.onnx",
        "config.json": f"{hf_base_url}/config.json",
        "tokenizer.json": f"{hf_base_url}/tokenizer.json",
        "tokenizer_config.json": f"{hf_base_url}/tokenizer_config.json",
        "special_tokens_map.json": f"{hf_base_url}/special_tokens_map.json",
        "vocab.txt": f"{hf_base_url}/vocab.txt",
    }

    for name, url in files_to_download.items():
        dest = model_dir / name
        if not dest.exists():
            download_file(url, dest)

    version_txt = model_dir / "version.txt"
    if not version_txt.exists():
        version_txt.write_text("1.0.0", encoding="utf-8")

    # 2. Download/prepare EasyOCR weights
    easyocr_sources = {
        "craft_mlt_25k.pth": "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip",
        "english_g2.pth": "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip",
    }

    home_easyocr_dir = Path.home() / ".EasyOCR" / "model"
    for name, url in easyocr_sources.items():
        dest = easyocr_dir / name
        if dest.exists():
            continue
        # Try local cache copy first
        cache_src = home_easyocr_dir / name
        if cache_src.exists():
            print(f"Copying cached easyocr model {name} from {cache_src}...")
            shutil.copy2(cache_src, dest)
        else:
            zip_dest = easyocr_dir / f"{name}.zip"
            download_file(url, zip_dest)
            print(f"Extracting {zip_dest}...")
            with zipfile.ZipFile(zip_dest, "r") as zip_ref:
                zip_ref.extractall(easyocr_dir)
            zip_dest.unlink()

    # 3. Compute SHA-256 hashes and write to app/core/hashes_registry.py
    hashes = {"generative_naming": {}, "easyocr": {}}
    for item in model_dir.glob("*"):
        if item.is_file():
            hasher = hashlib.sha256()
            with open(item, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            hashes["generative_naming"][item.name] = hasher.hexdigest()

    for item in easyocr_dir.glob("*"):
        if item.is_file():
            hasher = hashlib.sha256()
            with open(item, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            hashes["easyocr"][item.name] = hasher.hexdigest()

    hashes_registry_path = Path("app/core/hashes_registry.py")
    hashes_registry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hashes_registry_path, "w", encoding="utf-8") as f:
        f.write(
            '"""Registry containing expected hashes of the embedded model weights."""\n\n'
        )
        f.write("# This file is generated during the build process.\n")
        f.write(f"HASHES = {repr(hashes)}\n")
    print(
        f"Successfully prepared weights and wrote model hashes to {hashes_registry_path}"
    )


def main():
    """Build the standalone executable."""
    import importlib.util

    import PyInstaller.__main__

    is_cpu = "--cpu" in sys.argv
    if is_cpu:
        sys.argv.remove("--cpu")
        os.environ["CPU_BUILD"] = "1"
        print("CPU-only build profile enabled.")

    is_lite = "--lite" in sys.argv
    if is_lite:
        sys.argv.remove("--lite")
        os.environ["LITE_BUILD"] = "1"
        print(
            "Lite profile enabled. Heavy ML packages will be excluded from the build."
        )
        # Ensure hashes_registry.py exists to prevent import errors in lite builds
        from pathlib import Path

        hashes_registry_path = Path("app") / "core" / "hashes_registry.py"
        if not hashes_registry_path.exists():
            hashes_registry_path.parent.mkdir(parents=True, exist_ok=True)
            with open(hashes_registry_path, "w", encoding="utf-8") as f:
                f.write(
                    '"""Registry containing expected hashes of the embedded model weights."""\n\nHASHES = {}\n'
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

        if is_cpu:
            print("Verifying CPU-only PyTorch...")
            try:
                import torch

                version = getattr(torch, "__version__", "")
                is_cpu_torch = False
                if sys.platform in ("win32", "linux"):
                    if "+cpu" in version:
                        is_cpu_torch = True
                elif sys.platform == "darwin":
                    if not torch.cuda.is_available():
                        is_cpu_torch = True

                if not is_cpu_torch or "+cu" in version or "cuda" in version.lower():
                    print(
                        f"Error: Non-CPU PyTorch detected: {version}. CPU-only PyTorch is required."
                    )
                    sys.exit(1)
                else:
                    print(f"CPU-only PyTorch verified: {version}")
            except Exception as e:
                print(f"Error during CPU-only PyTorch verification: {e}")
                sys.exit(1)

        download_and_prepare_weights()

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

    if is_cpu:
        print("Scanning standalone package for GPU/CUDA/cuDNN binaries...")
        dist_dir = "dist/smart-autosorter"
        if os.path.exists(dist_dir):
            gpu_terms = [
                "cuda",
                "cudnn",
                "cublas",
                "nvrtc",
                "cudart",
                "nvtx",
                "libdevice",
            ]
            found_gpu_libs = []
            for root, dirs, files in os.walk(dist_dir):
                for file in files:
                    name_lower = file.lower()
                    if any(term in name_lower for term in gpu_terms):
                        found_gpu_libs.append(os.path.join(root, file))
            if found_gpu_libs:
                print("Error: Standalone bundle contains GPU/CUDA/cuDNN binaries!")
                for lib in found_gpu_libs:
                    print(f"  - Found: {lib}")
                sys.exit(1)
            else:
                print(
                    "Validation passed: No GPU/CUDA/cuDNN binaries found in the standalone package."
                )
        else:
            print(
                f"Warning: Standalone bundle directory {dist_dir} not found for post-build verification."
            )


if __name__ == "__main__":
    main()
