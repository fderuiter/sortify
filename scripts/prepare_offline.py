# /// script
# requires-python = ">=3.12"
# ///
"""Utility script to prepare an offline deployment bundle."""

import os
import shutil
import subprocess
from pathlib import Path


def main():
    """Prepare an offline bundle by downloading dependencies and model weights."""
    import sys

    is_cpu = "--cpu" in sys.argv
    if is_cpu:
        sys.argv.remove("--cpu")

    print("Preparing offline bundle...")
    bundle_dir = Path("offline_bundle")
    bundle_dir.mkdir(exist_ok=True)

    # Clean up previous ZIP output if any
    zip_output = Path("offline_bundle.zip")
    if zip_output.exists():
        try:
            zip_output.unlink()
        except OSError as e:
            print(f"Warning: could not delete {zip_output}: {e}")

    # Clean up wheels directory specifically
    wheels_dir = bundle_dir / "wheels"
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir)
    wheels_dir.mkdir()

    # 1. Compile requirements with CPU-only PyTorch
    print("Compiling requirements...")
    reqs_file = bundle_dir / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            "pyproject.toml",
            "--generate-hashes",
            "--extra-index-url",
            "https://download.pytorch.org/whl/cpu",
            "-o",
            str(reqs_file),
        ],
        check=True,
    )

    # 2. Download wheels
    print("Downloading Python dependencies...")
    subprocess.run(["uv", "venv", "--seed", ".tmp_seed_venv"], check=True)

    pip_path = (
        ".tmp_seed_venv/bin/pip"
        if os.name != "nt"
        else r".tmp_seed_venv\Scripts\pip.exe"
    )

    subprocess.run(
        [
            pip_path,
            "download",
            "-r",
            str(reqs_file),
            "--extra-index-url",
            "https://download.pytorch.org/whl/cpu",
            "-d",
            str(wheels_dir),
        ],
        check=True,
    )

    shutil.rmtree(".tmp_seed_venv")

    # 3. Validate CPU wheels
    if is_cpu:
        print(
            "Validating that only CPU-specific PyTorch wheels were compiled and downloaded..."
        )
        if reqs_file.exists():
            with open(reqs_file, "r") as f:
                content = f.read()
            import re

            torch_entries = re.findall(r"(torch[a-z0-9\-]*==[^\s]+)", content)
            for entry in torch_entries:
                if sys.platform in ("win32", "linux"):
                    if "+cpu" not in entry:
                        print(
                            f"Error: Non-CPU PyTorch dependency found in requirements.txt: {entry}"
                        )
                        sys.exit(1)
                if "+cu" in entry or "cuda" in entry.lower():
                    print(
                        f"Error: CUDA/GPU dependency found in requirements.txt: {entry}"
                    )
                    sys.exit(1)

        if wheels_dir.exists():
            for whl in wheels_dir.glob("*.whl"):
                name_lower = whl.name.lower()
                if "torch" in name_lower:
                    if sys.platform in ("win32", "linux"):
                        if "cpu" not in name_lower:
                            print(
                                f"Error: Non-CPU PyTorch wheel found in downloads: {whl.name}"
                            )
                            sys.exit(1)
                    if "cu" in name_lower or "cuda" in name_lower:
                        print(
                            f"Error: CUDA/GPU PyTorch wheel found in downloads: {whl.name}"
                        )
                        sys.exit(1)
        print(
            "Validation complete: Only CPU-specific PyTorch wheels are present in the offline bundle."
        )

    # 5. Package bundle
    print("Zipping bundle...")
    shutil.make_archive("offline_bundle", "zip", "offline_bundle")

    print("Done! Transfer offline_bundle.zip to the isolated environment.")


if __name__ == "__main__":
    main()
