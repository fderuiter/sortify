#!/usr/bin/env python3
"""Offline installation and verification script."""

import argparse
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


def _extract_and_install_offline(uv_cmd):
    if os.path.exists("offline_bundle.zip"):
        print("Detected offline_bundle.zip. Extracting...")
        try:
            with zipfile.ZipFile("offline_bundle.zip", "r") as zip_ref:
                zip_ref.extractall("offline_bundle")
        except Exception as e:
            print(f"Error extracting bundle: {e}")
            sys.exit(1)
    elif not os.path.isdir("offline_bundle"):
        print("Error: offline_bundle.zip not found.")
        sys.exit(1)

    print("Using offline wheels from bundle...")
    try:
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
    except subprocess.CalledProcessError as e:
        print(f"Package synchronization failed: {e}")
        sys.exit(1)


def offline_install(args):
    """Air-gapped installation mode."""
    print("Starting offline installation...")
    uv_cmd = get_uv_cmd()

    _extract_and_install_offline(uv_cmd)

    print("Offline installation complete.")


def main():
    """Execute the offline installation runner."""
    parser = argparse.ArgumentParser(
        description="Offline install runner for Smart AutoSorter AI Pro."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    offline_parser = subparsers.add_parser(
        "offline-install", help="Perform offline installation"
    )
    offline_parser.set_defaults(func=offline_install)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
