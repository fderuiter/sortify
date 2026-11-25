#!/usr/bin/env python3
"""Offline installation and verification script."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def safe_extract_zip(zip_path, extract_to):
    """Safely extract a ZIP archive while enforcing restricted permissions and validating member paths."""
    resolved_extract_to = Path(extract_to).resolve()

    # Enforce restricted host directory access permissions (0o700) on target directory
    os.makedirs(resolved_extract_to, mode=0o700, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(resolved_extract_to, 0o700)

    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for member in zip_ref.infolist():
            member_path = member.filename
            normalized_path = member_path.replace("\\", "/")

            # Inspect archive paths inline for absolute paths or relative directory traversal
            if (
                os.path.isabs(member_path)
                or normalized_path.startswith("/")
                or bool(re.match(r"^[a-zA-Z]:", member_path))
            ):
                raise ValueError(
                    f"Directory traversal or absolute path detected in archive member: '{member_path}'"
                )

            parts = Path(normalized_path).parts
            if ".." in parts:
                raise ValueError(
                    f"Directory traversal detected in archive member: '{member_path}'"
                )

            # Resolve target path and verify it stays strictly inside resolved_extract_to
            target_path = (resolved_extract_to / normalized_path).resolve()
            try:
                target_path.relative_to(resolved_extract_to)
            except ValueError:
                raise ValueError(
                    f"Directory traversal attempt detected: member path '{member_path}' resolves outside target directory"
                )

        # Extract all members safely once validated
        for member in zip_ref.infolist():
            zip_ref.extract(member, resolved_extract_to)
            target_file = resolved_extract_to / member.filename
            if member.is_dir() and sys.platform != "win32":
                try:
                    os.chmod(target_file, 0o700)
                except OSError:
                    pass


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


def _is_repo_checkout() -> bool:
    """Return True when running inside a git or Python project checkout."""
    return os.path.exists(".git") or os.path.exists("pyproject.toml")


def _resolve_target_venv(
    venv_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve the target virtual environment directory for offline installation."""
    if venv_dir:
        return Path(venv_dir)

    uv_project_env = os.environ.get("UV_PROJECT_ENVIRONMENT", "").strip()
    if uv_project_env:
        return Path(uv_project_env)

    in_checkout = _is_repo_checkout()
    virtual_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if virtual_env:
        # `uv run --no-project` inside a checkout still exports VIRTUAL_ENV=<cwd>/.venv
        # when `.venv` exists; ignore that implicit contributor venv so offline-install
        # never clobbers dev/type-check dependencies unless explicitly requested.
        if not (
            in_checkout
            and Path(virtual_env).resolve() == (Path.cwd() / ".venv").resolve()
        ):
            return Path(virtual_env)

    if in_checkout:
        return Path(".venv-offline")
    return Path(".venv")


def _venv_python_path(venv_dir: Path) -> Path:
    """Return the Python interpreter path inside `venv_dir`."""
    if sys.platform == "win32" or os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _extract_and_install_offline(
    uv_cmd: str, venv_dir: str | os.PathLike[str] | None = None
) -> Path:
    if os.path.exists("offline_bundle.zip"):
        print("Detected offline_bundle.zip. Extracting...")
        try:
            safe_extract_zip("offline_bundle.zip", "offline_bundle")
        except Exception as e:
            print(f"Error extracting bundle: {e}")
            sys.exit(1)
    elif not os.path.isdir("offline_bundle"):
        print("Error: offline_bundle.zip not found.")
        sys.exit(1)

    for manifest_path in (
        "offline_bundle/requirements.txt",
        "offline_bundle/app_wheel.txt",
    ):
        if not os.path.exists(manifest_path):
            print(f"Error: {manifest_path} not found.")
            sys.exit(1)

    target_venv = _resolve_target_venv(venv_dir)
    venv_python = _venv_python_path(target_venv)

    print(f"Using offline wheels from bundle into {target_venv}...")
    try:
        if not os.path.isdir(target_venv):
            subprocess.run([uv_cmd, "venv", str(target_venv)], check=True)
        subprocess.run(
            [
                uv_cmd,
                "pip",
                "install",
                "--python",
                str(venv_python),
                "--offline",
                "--no-index",
                "--find-links",
                "offline_bundle/wheels",
                "--require-hashes",
                "--no-deps",
                "-r",
                "offline_bundle/requirements.txt",
                "-r",
                "offline_bundle/app_wheel.txt",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"Package synchronization failed: {e}")
        sys.exit(1)

    return target_venv


def offline_install(args):
    """Air-gapped installation mode."""
    print("Starting offline installation...")
    uv_cmd = get_uv_cmd()

    venv_arg = getattr(args, "venv", None)
    if isinstance(venv_arg, (str, Path)):
        _extract_and_install_offline(uv_cmd, venv_dir=venv_arg)
    else:
        _extract_and_install_offline(uv_cmd)

    print("Offline installation complete.")


def main(argv: list[str] | None = None):
    """Execute the offline installation runner."""
    parser = argparse.ArgumentParser(
        description="Offline install runner for Smart AutoSorter AI Pro."
    )
    parser.add_argument(
        "--venv",
        default=None,
        help="Target virtual environment directory for offline installation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    offline_parser = subparsers.add_parser(
        "offline-install", help="Perform offline installation"
    )
    offline_parser.add_argument(
        "--venv",
        default=argparse.SUPPRESS,
        help="Target virtual environment directory for offline installation.",
    )
    offline_parser.set_defaults(func=offline_install)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
