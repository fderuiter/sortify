"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys


def update_binaries_and_manifest(system_platform=None, bypass_pytest_check=False):
    """Copy real compiled sqlcipher3 binaries from the active environment to app/binaries/<platform>/sqlcipher3 and update manifest.json."""
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

    saved_dbapi2 = None
    existing_dbapi2 = target_dir / "dbapi2.py"
    if existing_dbapi2.exists():
        try:
            txt = existing_dbapi2.read_text(encoding="utf-8")
            if "_load_sqlcipher_native_libraries" in txt:
                saved_dbapi2 = txt
        except Exception:
            pass

    if target_dir.exists():
        from app.core.resilient_file_ops import resilient_rmtree

        resilient_rmtree(target_dir)
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

    if saved_dbapi2 is not None:
        (target_dir / "dbapi2.py").write_text(saved_dbapi2, encoding="utf-8")

    if system_platform == "win32":
        # Ensure all required Windows DLLs (sqlite3, OpenSSL, MSVC Redist, UCRT) are copied to the binaries directory
        dll_patterns = [
            "libcrypto",
            "libssl",
            "sqlcipher",
            "libsqlcipher",
            "sqlite3",
            "msvcp140",
            "vcruntime140",
            "vcruntime140_1",
            "concrt140",
            "vcomp140",
            "ucrtbase",
            "api-ms-win-crt-",
        ]

        def matches_any_pattern(fname: str) -> bool:
            f_lower = fname.lower()
            if not f_lower.endswith(".dll"):
                return False
            return any(pat in f_lower for pat in dll_patterns)

        # Build list of search directories
        search_dirs = []
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

        for vd in venv_dirs:
            for sub in [
                Path("."),
                Path("Library") / "bin",
                Path("DLLs"),
                Path("Scripts"),
                Path("Lib") / "site-packages" / "sqlcipher3",
                Path("Lib") / "site-packages" / "sqlcipher3" / ".libs",
                Path("Lib") / "site-packages" / "sqlcipher3.libs",
                Path("app") / "binaries" / "windows" / "sqlcipher3",
            ]:
                candidate_dir = vd / sub
                if candidate_dir.exists() and candidate_dir not in search_dirs:
                    search_dirs.append(candidate_dir)

        if sys.base_prefix and Path(sys.base_prefix) not in search_dirs:
            for sub in [
                Path("."),
                Path("Library") / "bin",
                Path("DLLs"),
                Path("Scripts"),
            ]:
                candidate_dir = Path(sys.base_prefix) / sub
                if candidate_dir.exists() and candidate_dir not in search_dirs:
                    search_dirs.append(candidate_dir)

        if sys.executable:
            exe_dir = Path(os.path.dirname(sys.executable))
            for sub in [Path("."), Path("Library") / "bin", Path("DLLs")]:
                candidate_dir = exe_dir / sub
                if candidate_dir.exists() and candidate_dir not in search_dirs:
                    search_dirs.append(candidate_dir)

        common_redist_dirs = [
            Path("C:/Program Files/OpenSSL-Win64/bin"),
            Path("C:/Program Files/OpenSSL/bin"),
            Path(
                "C:/Program Files/Microsoft Visual Studio/2022/Enterprise/VC/Redist/MSVC"
            ),
            Path(
                "C:/Program Files/Microsoft Visual Studio/2022/Community/VC/Redist/MSVC"
            ),
            Path(
                "C:/Program Files/Microsoft Visual Studio/2022/BuildTools/VC/Redist/MSVC"
            ),
            Path(
                "C:/Program Files (x86)/Microsoft Visual Studio/2019/Enterprise/VC/Redist/MSVC"
            ),
            Path(
                "C:/Program Files (x86)/Microsoft Visual Studio/2019/Community/VC/Redist/MSVC"
            ),
            Path(
                "C:/Program Files (x86)/Microsoft Visual Studio/2019/BuildTools/VC/Redist/MSVC"
            ),
            Path("C:/Program Files (x86)/Windows Kits/10/Redist"),
            Path("C:/Program Files/Windows Kits/10/Redist"),
            Path("C:/Windows/System32"),
            Path("C:/Windows/SysWOW64"),
        ]
        vc_redist_env = os.environ.get("VCToolsRedistDir")
        if vc_redist_env and Path(vc_redist_env).exists():
            common_redist_dirs.append(Path(vc_redist_env))

        for cd in common_redist_dirs:
            if cd.exists() and cd not in search_dirs:
                search_dirs.append(cd)

        for s_dir in search_dirs:
            try:
                if not s_dir.exists():
                    continue
                for root, dirs, files in os.walk(s_dir):
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
                            "node_modules",
                        )
                    ]
                    for f in files:
                        if matches_any_pattern(f):
                            src_file = Path(root) / f
                            dest_file = target_dir / f
                            if not dest_file.exists():
                                shutil.copy2(src_file, dest_file)
                                if f not in copied_files:
                                    copied_files.append(f)
                                print(
                                    f"Harvested Windows dependency DLL {f} from {src_file} to {dest_file}"
                                )
            except Exception as scan_err:
                print(f"Warning: Exception scanning {s_dir}: {scan_err}")

    manifest_path = Path("app") / "binaries" / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = {}

    from app.core.resilient_file_ops import resilient_file_hash

    platform_hashes = {}
    for rel_path_str in copied_files:
        file_path = target_dir / rel_path_str
        is_text_file = file_path.suffix in (".py", ".pyi", ".typed")
        actual_hash = resilient_file_hash(file_path, normalize_text=is_text_file)
        platform_hashes[f"sqlcipher3/{rel_path_str}"] = actual_hash

    manifest[platform_key] = platform_hashes

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, sort_keys=True)

    print(
        f"Successfully copied real compiled binaries and updated manifest for platform: {platform_key}"
    )


def download_file(
    url,
    dest_path,
    timeout=30.0,
    max_retries=3,
    backoff_factor=1.0,
):
    """Download a file from url to dest_path with explicit socket timeouts, automatic retry logic, and cleanup on failure.

    Uses Python standard library utilities alone.
    """
    import shutil
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {url} to {dest_path}...")
    req = urllib.request.Request(str(url), headers={"User-Agent": "Mozilla/5.0"})
    last_exception = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            delay = backoff_factor * (2 ** (attempt - 2))
            print(f"Retry attempt {attempt}/{max_retries} for {url} in {delay:.1f}s...")
            time.sleep(delay)

        try:
            with (
                urllib.request.urlopen(req, timeout=timeout) as response,
                open(dest_path, "wb") as out_file,
            ):
                shutil.copyfileobj(response, out_file)
            return
        except BaseException as err:
            last_exception = err if isinstance(err, Exception) else Exception(str(err))
            if dest_path.exists():
                try:
                    dest_path.unlink()
                except OSError as remove_err:
                    print(
                        f"Warning: Failed to clean up partial file {dest_path}: {remove_err}"
                    )
            print(f"Download attempt {attempt}/{max_retries} failed for {url}: {err}")
            if isinstance(err, (KeyboardInterrupt, SystemExit)):
                raise

    err_desc = (
        f"Failed to download asset {url} after {max_retries} attempts: {last_exception}"
    )
    print(err_desc)
    if last_exception is not None:
        raise last_exception
    else:
        raise RuntimeError(err_desc)


def download_and_prepare_weights():
    """Ensure that the build process downloads and bundles all necessary model weights."""
    import shutil
    import zipfile
    from pathlib import Path

    print("Preparing and downloading model weights for offline execution...")
    offline_bundle = Path("offline_bundle")
    model_dir = offline_bundle / "model"
    easyocr_dir = offline_bundle / "easyocr"

    model_dir.mkdir(parents=True, exist_ok=True)
    easyocr_dir.mkdir(parents=True, exist_ok=True)

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
    from app.core.resilient_file_ops import resilient_file_hash

    hashes = {"generative_naming": {}, "easyocr": {}}
    for item in model_dir.glob("*"):
        if item.is_file():
            hashes["generative_naming"][item.name] = resilient_file_hash(item)

    for item in easyocr_dir.glob("*"):
        if item.is_file():
            hashes["easyocr"][item.name] = resilient_file_hash(item)

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
    package_sidecar_models()


def package_sidecar_models():
    """Package offline_bundle model weights into smart-autosorter-models.zip sidecar artifact."""
    import zipfile
    from pathlib import Path

    offline_bundle = Path("offline_bundle")
    if not offline_bundle.exists() or not any(offline_bundle.iterdir()):
        print(
            "Warning: offline_bundle directory empty or missing. Skipping sidecar packaging."
        )
        return

    dist_dir = Path("dist")
    dist_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dist_dir / "smart-autosorter-models.zip"

    print(f"Creating sidecar model package at {zip_path}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for file_path in offline_bundle.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(offline_bundle)
                zip_file.write(file_path, arcname=rel_path)

    root_zip = Path("smart-autosorter-models.zip")
    if zip_path.exists():
        import shutil

        shutil.copy2(zip_path, root_zip)

    print(f"Successfully packaged sidecar model archive: {zip_path}")


def verify_scientific_binaries(dist_dir: str) -> bool:
    """Statically verify that SciPy package files and native C-extensions exist in the bundle folder."""
    print("Executing post-build static inspection for scientific computing C-extensions...")
    if not os.path.exists(dist_dir):
        print(
            f"Error: Standalone bundle directory {dist_dir} not found for static scientific inspection."
        )
        sys.exit(1)

    scipy_dir_found = False
    scipy_binaries = []

    for root, dirs, files in os.walk(dist_dir):
        rel_root = os.path.relpath(root, dist_dir).replace("\\", "/")
        parts = rel_root.lower().split("/")
        if "scipy" in parts:
            scipy_dir_found = True
            for file in files:
                if file.lower().endswith((".pyd", ".so", ".dylib", ".dll")):
                    scipy_binaries.append(os.path.join(root, file))

    if not scipy_dir_found:
        print(
            f"Error: Static inspection failed. SciPy package directory is missing in {dist_dir}!"
        )
        sys.exit(1)

    if not scipy_binaries:
        print(
            f"Error: Static inspection failed. No compiled scientific C-extension binaries (.pyd, .so, .dylib, .dll) found in SciPy package within {dist_dir}!"
        )
        sys.exit(1)

    print(
        f"Static post-build verification passed: Verified SciPy package and found {len(scipy_binaries)} native C-extension binaries in {dist_dir}."
    )
    return True


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
            ("SciPy", "scipy"),
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

    dist_dir = os.path.join("dist", "smart-autosorter")
    if os.path.exists(dist_dir):
        exe_ext = ".exe" if sys.platform == "win32" else ""
        cli_exe = os.path.join(dist_dir, f"smart-autosorter{exe_ext}")
        if not os.path.exists(cli_exe):
            print(
                f"Error: Missing expected executable target in {dist_dir}: smart-autosorter{exe_ext}"
            )
            sys.exit(1)

        print(
            f"Verification passed: Verified executable target (smart-autosorter{exe_ext}) in {dist_dir}"
        )

        if not is_lite:
            verify_scientific_binaries(dist_dir)
    else:
        print(
            f"Warning: Standalone bundle directory {dist_dir} not found for post-build verification."
        )

    print("Executing post-build PE binary dependency verification...")
    from pathlib import Path

    from scripts.verify_pe_dependencies import verify_pe_dependencies

    pe_success, _ = verify_pe_dependencies(
        Path("dist") / "smart-autosorter", allow_empty=True
    )
    if not pe_success:
        print("Error: Post-build PE binary dependency verification failed!")
        sys.exit(1)

    if is_cpu:
        print("Scanning standalone package for GPU/CUDA/cuDNN binaries...")
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
