"""Unified build script for smart autosorter application on Windows, macOS, and Linux."""

import os
import sys

import PyInstaller.__main__


def main():
    """Build the standalone executable."""
    import importlib.util

    is_lite = "--lite" in sys.argv
    if is_lite:
        sys.argv.remove("--lite")
        os.environ["LITE_BUILD"] = "1"
        print("Lite profile enabled. Heavy ML packages will be excluded from the build.")

    print("Verifying SQLCipher in active environment...")
    spec = importlib.util.find_spec("sqlcipher3")
    if not spec or not spec.submodule_search_locations:
        print(
            "Error: sqlcipher3 not found in active environment. Please ensure dependencies are installed."
        )
        sys.exit(1)

    cmd = ["smart-autosorter.spec", "--noconfirm", "--clean"]

    PyInstaller.__main__.run(cmd)


if __name__ == "__main__":
    main()
