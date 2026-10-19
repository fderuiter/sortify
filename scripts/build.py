"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys


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


def bundle_license(dist_dir):
    """Copy LICENSE to the top of the bundle; the GPL requires it to ship with binaries."""
    import shutil
    from pathlib import Path

    license_src = Path("LICENSE")
    if not license_src.is_file():
        print("Error: LICENSE not found; refusing to build a bundle without it.")
        sys.exit(1)
    shutil.copy2(license_src, Path(dist_dir) / "LICENSE")
    print(f"Bundled LICENSE into {dist_dir}")


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
    print(
        "Executing post-build static inspection for scientific computing C-extensions..."
    )
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

        bundle_license(dist_dir)

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
