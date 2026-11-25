"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.resilient_file_ops import resilient_file_hash  # noqa: E402
from scripts.offline_bundle_utils import (  # noqa: E402
    format_hashes_registry,
    sync_hashes_registry,
)

PINNED_HASHES = {
    "generative_naming": {
        "config.json": "3c8e94b12f4a7e5109d2b8a4c6e173f90b2d45a81c7e9034f6a82b5d1e39c704",
        "model.gguf": "a94f28c61b7d03e5924a8c1f63d7b09e54a281c7f09b3e6d41c82a57f90e1b36",
    },
    "model_download": {
        "config.json": "7135149f7cffa1a573466c6e4d8423ed73b62fd2332c575bf738a0d033f70df7",
        "model.onnx": "759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e",
        "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
        "tokenizer.json": "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0",
        "tokenizer_config.json": "9261e7d79b44c8195c1cada2b453e55b00aeb81e907a6664974b4d7776172ab3",
        "version.txt": "92521fc3cbd964bdc9f584a991b89fddaa5754ed1cc96d6d42445338669c1305",
        "vocab.txt": "07eced375cec144d27c900241f3e339478dec958f92fddbc551f295c992038a3",
    },
    "easyocr": {
        "craft_mlt_25k.pth": "4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17",
        "english_g2.pth": "e2272681d9d67a04e2dff396b6e95077bc19001f8f6d3593c307b9852e1c29e8",
    },
    "florence-2": {
        "config.json": "7135149f7cffa1a573466c6e4d8423ed73b62fd2332c575bf738a0d033f70df7",
        "model.safetensors": "759c3cd2b7fe7e93933ad23c4c9181b7396442a2ed746ec7c1d46192c469c46e",
    },
}


def download_file(
    url,
    dest_path,
    timeout=30.0,
    max_retries=3,
    backoff_factor=1.0,
    expected_sha256=None,
):
    """Download a file from url to dest_path with explicit socket timeouts, automatic retry logic, cryptographic hash verification, and cleanup on failure.

    Uses Python standard library utilities alone.
    """
    import hashlib
    import shutil
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    if isinstance(timeout, str) and expected_sha256 is None:
        expected_sha256 = timeout
        timeout = 30.0

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

            if expected_sha256:
                hasher = hashlib.sha256()
                with open(dest_path, "rb") as f:
                    while chunk := f.read(65536):
                        hasher.update(chunk)
                actual_hash = hasher.hexdigest()
                if actual_hash.lower() != expected_sha256.lower():
                    raise ValueError(
                        f"Hash mismatch for {dest_path}: expected {expected_sha256}, got {actual_hash}"
                    )
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


def safe_extract_archive(zip_path, extract_to):
    """Safely extract a ZIP archive while enforcing path traversal guards using safe_extract_path."""
    import zipfile
    from pathlib import Path

    from app.core.path_utils import safe_extract_path

    extract_to_path = Path(extract_to).resolve()
    extract_to_path.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        # Pre-validate all members before writing any files to disk
        for member in zip_ref.infolist():
            member_path = member.filename
            safe_extract_path(extract_to_path, member_path, allow_same_dir=True)
            normalized_parts = Path(member_path.replace("\\", "/")).parts
            if ".." in normalized_parts:
                raise ValueError(
                    f"Directory traversal detected in archive member: '{member_path}'"
                )

        # Extract members once all paths are verified safe
        for member in zip_ref.infolist():
            zip_ref.extract(member, extract_to_path)


def download_and_prepare_weights():
    """Ensure that the build process downloads, verifies, and bundles all necessary model weights."""
    import shutil
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
        expected_hash = PINNED_HASHES["model_download"][name]
        if dest.exists():
            current_hash = resilient_file_hash(dest)
            if current_hash == expected_hash:
                continue
            print(
                f"Existing model file {dest} hash mismatch ({current_hash} != {expected_hash}). Re-downloading..."
            )
            dest.unlink()
        download_file(url, dest, expected_sha256=expected_hash)

    version_txt = model_dir / "version.txt"
    if not version_txt.exists():
        version_txt.write_text("1.0.0", encoding="utf-8")
    expected_version_hash = PINNED_HASHES["model_download"]["version.txt"]
    actual_version_hash = resilient_file_hash(version_txt)
    if actual_version_hash != expected_version_hash:
        raise ValueError(
            f"Hash mismatch for version.txt: expected {expected_version_hash}, got {actual_version_hash}"
        )

    # 2. Download/prepare EasyOCR weights
    easyocr_sources = {
        "craft_mlt_25k.pth": "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip",
        "english_g2.pth": "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/english_g2.zip",
    }

    home_easyocr_dir = Path.home() / ".EasyOCR" / "model"
    for name, url in easyocr_sources.items():
        dest = easyocr_dir / name
        expected_hash = PINNED_HASHES["easyocr"][name]
        if dest.exists():
            current_hash = resilient_file_hash(dest)
            if current_hash == expected_hash:
                continue
            print(
                f"Existing easyocr file {dest} hash mismatch ({current_hash} != {expected_hash}). Re-fetching..."
            )
            dest.unlink()

        # Try local cache copy first
        cache_src = home_easyocr_dir / name
        if cache_src.exists():
            print(f"Validating cached easyocr model {name} from {cache_src}...")
            cache_hash = resilient_file_hash(cache_src)
            if cache_hash != expected_hash:
                raise ValueError(
                    f"Cached asset hash mismatch for {cache_src}: expected {expected_hash}, got {cache_hash}"
                )
            print(f"Copying verified cached easyocr model {name} from {cache_src}...")
            shutil.copy2(cache_src, dest)
        else:
            zip_dest = easyocr_dir / f"{name}.zip"
            try:
                download_file(url, zip_dest)
                print(f"Extracting {zip_dest}...")
                safe_extract_archive(zip_dest, easyocr_dir)
                if not dest.exists():
                    raise FileNotFoundError(
                        f"Expected model file {dest} not found after extracting {zip_dest}"
                    )
                extracted_hash = resilient_file_hash(dest)
                if extracted_hash != expected_hash:
                    dest.unlink()
                    raise ValueError(
                        f"Extracted archive hash mismatch for {name}: expected {expected_hash}, got {extracted_hash}"
                    )
            finally:
                if zip_dest.exists():
                    zip_dest.unlink()

    # 3. Validate all downloaded weights against pinned hashes before writing registry
    hashes: dict[str, dict[str, str]] = {"model_download": {}, "easyocr": {}}
    for category, file_dir in [
        ("model_download", model_dir),
        ("easyocr", easyocr_dir),
    ]:
        for item in sorted(file_dir.glob("*"), key=lambda p: p.name):
            if item.is_file():
                actual_hash = resilient_file_hash(item)
                expected_hash = PINNED_HASHES.get(category, {}).get(item.name)
                if expected_hash is None or actual_hash != expected_hash:
                    raise ValueError(
                        f"Computed file hash does not match pinned registry hash for {category}/{item.name}: "
                        f"expected {expected_hash}, got {actual_hash}"
                    )
                hashes[category][item.name] = actual_hash

        for expected_file in PINNED_HASHES[category]:
            if expected_file not in hashes[category]:
                raise ValueError(
                    f"Missing required model weight in {category}: {expected_file}"
                )

    registry_data = {cat: dict(entries) for cat, entries in PINNED_HASHES.items()}
    registry_data["model_download"] = dict(hashes["model_download"])
    registry_data["easyocr"] = dict(hashes["easyocr"])

    hashes_registry_path = Path("app/core/hashes_registry.py")
    sync_hashes_registry(hashes_registry_path, registry_data)
    print(
        f"Successfully prepared weights and verified model hashes against pinned registry: {hashes_registry_path}"
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
        for file_path in sorted(offline_bundle.rglob("*")):
            if file_path.is_file():
                rel_path = file_path.relative_to(offline_bundle)
                zip_file.write(file_path, arcname=rel_path)

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


def verify_documentation_assets(dist_dir: str) -> bool:
    """Statically verify that bundled markdown documentation and compiled SVG diagrams exist."""
    print("Executing post-build documentation asset verification...")
    docs_dist = os.path.join(dist_dir, "docs")
    if not os.path.exists(docs_dist):
        internal_docs = os.path.join(dist_dir, "_internal", "docs")
        if os.path.exists(internal_docs):
            docs_dist = internal_docs
        else:
            print(
                f"Error: Missing documentation directory in standalone bundle: {docs_dist}"
            )
            sys.exit(1)

    required_docs = [
        "user_guide.md",
        "admin_guide.md",
        "architecture.md",
        "troubleshooting.md",
        "keyboard_shortcuts.md",
    ]

    missing_docs = [
        doc for doc in required_docs if not os.path.exists(os.path.join(docs_dist, doc))
    ]
    if missing_docs:
        print(
            f"Error: Post-build documentation verification failed! Missing required documentation files in {docs_dist}: {missing_docs}"
        )
        sys.exit(1)

    # Verify diagram assets if present in source repository
    src_diagrams_dir = os.path.join("docs", "assets", "diagrams")
    dist_diagrams_dir = os.path.join(docs_dist, "assets", "diagrams")

    if os.path.exists(src_diagrams_dir):
        src_svgs = [
            f for f in os.listdir(src_diagrams_dir) if f.lower().endswith(".svg")
        ]
        if src_svgs:
            if not os.path.exists(dist_diagrams_dir):
                print(
                    f"Error: Post-build documentation verification failed! Diagram assets directory missing in bundle: {dist_diagrams_dir}"
                )
                sys.exit(1)
            missing_svgs = [
                svg
                for svg in src_svgs
                if not os.path.exists(os.path.join(dist_diagrams_dir, svg))
            ]
            if missing_svgs:
                print(
                    f"Error: Post-build documentation verification failed! Missing compiled diagram SVG files in {dist_diagrams_dir}: {missing_svgs}"
                )
                sys.exit(1)
            print(
                f"Verification passed: Verified {len(src_svgs)} diagram SVG assets in {dist_diagrams_dir}"
            )
        else:
            print(
                "Notice: Source diagram directory is empty; skipping SVG diagram verification."
            )
    else:
        if os.path.exists(dist_diagrams_dir):
            dist_svgs = [
                f for f in os.listdir(dist_diagrams_dir) if f.lower().endswith(".svg")
            ]
            print(
                f"Verification passed: Verified {len(dist_svgs)} diagram SVG assets in {dist_diagrams_dir}"
            )
        else:
            print(
                "Notice: No source diagrams directory found; skipping SVG diagram verification."
            )

    print(
        f"Verification passed: Verified required documentation files ({', '.join(required_docs)}) in {docs_dist}"
    )
    return True


def main(argv=None):
    """Build the standalone executable."""
    import importlib.util

    parser = argparse.ArgumentParser(
        description="Build the standalone executable for Smart AutoSorter."
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Enable CPU-only build profile (excludes GPU/CUDA dependencies).",
    )
    parser.add_argument(
        "--lite",
        action="store_true",
        help="Enable Lite build profile (excludes heavy machine learning packages).",
    )
    args = parser.parse_args(argv)

    is_cpu = args.cpu
    if is_cpu:
        os.environ["CPU_BUILD"] = "1"
        print("CPU-only build profile enabled.")

    is_lite = args.lite
    if is_lite:
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
                f.write(format_hashes_registry({}))
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

    try:
        import PyInstaller.__main__
    except ImportError:
        print(
            "Error: PyInstaller is not installed. Please install build dependencies (e.g., 'pip install .[build]' or 'uv sync --group build')."
        )
        sys.exit(1)

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

        verify_documentation_assets(dist_dir)

        if not is_lite:
            verify_scientific_binaries(dist_dir)

        if sys.platform == "win32":
            from pathlib import Path

            if Path(dist_dir).is_dir():
                print("Executing post-build PE binary dependency verification...")
                from scripts.verify_pe_dependencies import verify_pe_dependencies

                pe_success, _ = verify_pe_dependencies(
                    Path(dist_dir), allow_empty=False
                )
                if not pe_success:
                    print("Error: Post-build PE binary dependency verification failed!")
                    sys.exit(1)
    else:
        print(
            f"Warning: Standalone bundle directory {dist_dir} not found for post-build verification."
        )

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
            gpu_binary_exts = (
                ".so",
                ".dll",
                ".dylib",
                ".pyd",
                ".bc",
                ".cubin",
                ".fatbin",
                ".ptx",
            )
            found_gpu_libs = []
            for root, dirs, files in os.walk(dist_dir):
                for file in files:
                    name_lower = file.lower()
                    is_gpu_binary = (
                        name_lower.endswith(gpu_binary_exts) or ".so." in name_lower
                    )
                    if is_gpu_binary and any(term in name_lower for term in gpu_terms):
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
