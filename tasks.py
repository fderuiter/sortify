#!/usr/bin/env python3
"""Task runner for local development and setup."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile


def get_uv_cmd():
    """Retrieve the path to the uv executable or exit if not found."""
    uv_cmd = shutil.which("uv")
    if not uv_cmd:
        local_uv = os.path.expanduser("~/.local/bin/uv")
        if os.path.exists(local_uv):
            return local_uv
        if os.path.exists(local_uv + ".exe"):
            return local_uv + ".exe"
        print("uv package manager not found.")
        print("Error: uv is not installed.")
        print("Please install uv manually before running this setup script.")
        print("")
        print("Installation instructions:")
        print("Run the following command in your terminal:")
        print("  curl -LsSf https://astral.sh/uv/install.sh | sh")
        print("")
        print(
            "Or refer to the official documentation: https://docs.astral.sh/uv/getting-started/installation/"
        )
        sys.exit(1)
    return uv_cmd


def setup(args):
    """Local workspace package syncing."""
    print("Setting up development environment...")

    offline_mode = False
    if os.path.exists("offline_bundle.zip"):
        print("Detected offline_bundle.zip. Extracting...")
        with zipfile.ZipFile("offline_bundle.zip", "r") as zip_ref:
            zip_ref.extractall("offline_bundle")

    if os.path.isdir("offline_bundle"):
        print("Offline bundle found. Enabling air-gapped installation mode.")
        offline_mode = True

    uv_cmd = get_uv_cmd()

    print("Synchronizing local environment...")
    if offline_mode:
        print("Using offline wheels from bundle...")
        if not os.path.isdir(".venv"):
            subprocess.run([uv_cmd, "venv"], check=True)
        subprocess.run(
            [
                uv_cmd,
                "pip",
                "install",
                "--offline",
                "--no-index",
                "--find-links",
                "offline_bundle/wheels",
                "-r",
                "offline_bundle/requirements.txt",
            ],
            check=True,
        )
        print(
            "Skipping pre-commit installation in offline mode to avoid network calls."
        )
    else:
        print("Synchronizing local environment with lockfile...")
        subprocess.run([uv_cmd, "sync"], check=True)
        print("Installing pre-commit hooks...")
        subprocess.run([uv_cmd, "run", "pre-commit", "install"], check=True)

    print("Setup complete. Virtual environment provisioned.")
    print("You can manually run the application anytime using:")
    print("  uv run smart-autosorter")


def verify_weights(args):
    """Offline machine learning weight validation."""
    print("Verifying local weights...")

    # Using the hf_manifest.json to check weights inside the model dir
    manifest_path = "app/core/hf_manifest.json"
    if not os.path.exists(manifest_path):
        print(f"Error: Manifest not found at {manifest_path}")
        sys.exit(1)

    model_dir = "offline_bundle/model"
    if not os.path.isdir(model_dir):
        print(f"Error: Model directory not found at {model_dir}")
        sys.exit(1)

    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"Failed to read model manifest: {e}")
        sys.exit(1)

    critical_files = ["config.json", "tokenizer.json"]
    valid_weight_found = False
    error_count = 0

    for rel_path, expected_hash in manifest.items():
        if rel_path.startswith(".cache"):
            continue

        filepath = os.path.join(model_dir, rel_path)
        if not os.path.exists(filepath):
            if rel_path in critical_files:
                print(f"Error: Missing critical model file: {rel_path}")
                error_count += 1
            continue

        if rel_path in ["pytorch_model.bin", "model.safetensors"] or rel_path.endswith(
            ".onnx"
        ):
            valid_weight_found = True

        file_hash = hashlib.sha256()
        try:
            with open(filepath, "rb") as file_obj:
                while chunk := file_obj.read(8192):
                    file_hash.update(chunk)
        except Exception as e:
            print(f"Error reading model file {rel_path}: {e}")
            error_count += 1
            continue

        if file_hash.hexdigest() != expected_hash:
            print(f"Error: Checksum mismatch for side-loaded model file: {rel_path}")
            error_count += 1

    if not valid_weight_found:
        print("Error: No valid weight formats found (PyTorch, SafeTensors, or ONNX).")
        error_count += 1

    if error_count > 0:
        print(f"Weight verification failed with {error_count} errors.")
        sys.exit(1)

    print("Weight validation successful. All checksums match the manifest.")


def test_env(args):
    """Task to verify the environment by running the test suite."""
    print("Verifying environment by running tests...")
    uv_cmd = get_uv_cmd()
    try:
        subprocess.run([uv_cmd, "run", "pytest"], check=True)
    except subprocess.CalledProcessError:
        print("Test suite failed. Please check your environment.")
        sys.exit(1)


def offline_install(args):
    """Air-gapped installation mode."""
    print("Starting offline installation...")

    if not os.path.exists("offline_bundle.zip"):
        print("Error: offline_bundle.zip not found.")
        sys.exit(1)

    print("Detected offline_bundle.zip. Extracting...")
    with zipfile.ZipFile("offline_bundle.zip", "r") as zip_ref:
        zip_ref.extractall("offline_bundle")

    uv_cmd = get_uv_cmd()
    print("Using offline wheels from bundle...")
    if not os.path.isdir(".venv"):
        subprocess.run([uv_cmd, "venv"], check=True)
    subprocess.run(
        [
            uv_cmd,
            "pip",
            "install",
            "--offline",
            "--no-index",
            "--find-links",
            "offline_bundle/wheels",
            "-r",
            "offline_bundle/requirements.txt",
        ],
        check=True,
    )

    # Call verify_weights logic here since offline-install verifies local weights
    print("\nVerifying local weights...")
    manifest_path = "app/core/hf_manifest.json"
    if not os.path.exists(manifest_path):
        print(f"Error: Manifest not found at {manifest_path}")
        sys.exit(1)

    model_dir = "offline_bundle/model"
    if not os.path.isdir(model_dir):
        print(f"Error: Model directory not found at {model_dir}")
        sys.exit(1)

    try:
        with open(manifest_path, "r") as f:
            manifest = json.load(f)
    except Exception as e:
        print(f"Failed to read model manifest: {e}")
        sys.exit(1)

    critical_files = ["config.json", "tokenizer.json"]
    valid_weight_found = False
    error_count = 0

    for rel_path, expected_hash in manifest.items():
        if rel_path.startswith(".cache"):
            continue

        filepath = os.path.join(model_dir, rel_path)
        if not os.path.exists(filepath):
            if rel_path in critical_files:
                print(f"Error: Missing critical model file: {rel_path}")
                error_count += 1
            continue

        if rel_path in ["pytorch_model.bin", "model.safetensors"] or rel_path.endswith(
            ".onnx"
        ):
            valid_weight_found = True

        file_hash = hashlib.sha256()
        try:
            with open(filepath, "rb") as file_obj:
                while chunk := file_obj.read(8192):
                    file_hash.update(chunk)
        except Exception as e:
            print(f"Error reading model file {rel_path}: {e}")
            error_count += 1
            continue

        if file_hash.hexdigest() != expected_hash:
            print(f"Error: Checksum mismatch for side-loaded model file: {rel_path}")
            error_count += 1

    if not valid_weight_found:
        print("Error: No valid weight formats found (PyTorch, SafeTensors, or ONNX).")
        error_count += 1

    if error_count > 0:
        print(f"Weight verification failed with {error_count} errors.")
        sys.exit(1)

    print("Weight validation successful. All checksums match the manifest.")
    print("Offline installation complete.")


def main():
    """Execute the task runner."""
    parser = argparse.ArgumentParser(
        description="Task runner for Smart AutoSorter AI Pro setup."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="Sync local workspace packages")
    setup_parser.set_defaults(func=setup)

    verify_parser = subparsers.add_parser(
        "verify-weights",
        help="Verify offline machine learning weights against manifest",
    )
    verify_parser.set_defaults(func=verify_weights)

    offline_parser = subparsers.add_parser(
        "offline-install", help="Perform offline installation and verify weights"
    )
    offline_parser.set_defaults(func=offline_install)

    test_parser = subparsers.add_parser(
        "test", help="Verify environment by running the test suite"
    )
    test_parser.set_defaults(func=test_env)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
