"""Utility for generating and verifying SHA-256 manifests for agent prompts."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_PATH = Path(".github/AGENTS/manifest.json")
AGENTS_DIR = Path(".github/AGENTS")


def compute_sha256(filepath):
    """Compute the SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_hashes():
    """Retrieve SHA-256 hashes for all agent prompt files."""
    hashes = {}
    for filepath in AGENTS_DIR.glob("*"):
        if filepath.is_file() and filepath.name != "manifest.json":
            hashes[filepath.name] = compute_sha256(filepath)
    return hashes


def generate():
    """Generate the manifest.json file containing hashes of agent prompts."""
    hashes = get_hashes()
    with open(MANIFEST_PATH, "w") as f:
        json.dump(hashes, f, indent=4, sort_keys=True)
        f.write("\n")
    print(f"Manifest successfully generated at {MANIFEST_PATH}")


def verify():
    """Verify that all agent prompt files match the hashes in manifest.json."""
    if not MANIFEST_PATH.exists():
        print(f"Error: Manifest file {MANIFEST_PATH} does not exist.")
        sys.exit(1)

    with open(MANIFEST_PATH, "r") as f:
        try:
            manifest_hashes = json.load(f)
        except json.JSONDecodeError:
            print("Error: Manifest file is not valid JSON.")
            sys.exit(1)

    current_hashes = get_hashes()

    mismatches = False

    # Check for missing files or mismatched hashes
    for filename, current_hash in current_hashes.items():
        if filename not in manifest_hashes:
            print(f"Error: File {filename} is missing from the manifest.")
            mismatches = True
        elif manifest_hashes[filename] != current_hash:
            print(f"Error: Hash mismatch for {filename}.")
            mismatches = True

    # Check for deleted files
    for filename in manifest_hashes:
        if filename not in current_hashes:
            print(f"Error: File {filename} is in the manifest but no longer exists.")
            mismatches = True

    if mismatches:
        print("Verification failed. Some agent prompt files do not match the manifest.")
        print(
            "Please use 'uv run python scripts/prompt_manifest.py generate' to update the manifest."
        )
        sys.exit(1)
    else:
        print("Verification passed. All agent prompt files match the manifest.")


def main():
    """Execute the main entry point for the manifest utility."""
    parser = argparse.ArgumentParser(
        description="Agent Prompts SHA-256 Manifest Utility"
    )
    parser.add_argument(
        "action", choices=["generate", "verify"], help="Action to perform"
    )

    args = parser.parse_args()

    if args.action == "generate":
        generate()
    elif args.action == "verify":
        verify()


if __name__ == "__main__":
    main()
