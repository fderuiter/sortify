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
    print("Preparing offline bundle...")
    bundle_dir = Path("offline_bundle")
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir()

    wheels_dir = bundle_dir / "wheels"
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

    # 5. Package bundle
    print("Zipping bundle...")
    shutil.make_archive("offline_bundle", "zip", "offline_bundle")

    print("Done! Transfer offline_bundle.zip to the isolated environment.")


if __name__ == "__main__":
    main()
