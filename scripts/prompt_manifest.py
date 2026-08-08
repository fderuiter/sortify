"""Utility for generating and verifying SHA-256 manifests for agent prompts."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

MANIFEST_PATH = Path(".github/AGENTS/manifest.json")
AGENTS_DIR = Path(".github/AGENTS")


def parse_and_validate_prompt(filepath: Path) -> tuple[dict, str]:
    """
    Parses and validates the YAML frontmatter of an agent prompt file,
    and returns (frontmatter_dict, body_text).
    If no frontmatter is present, treats the entire file as body_text.
    """
    try:
        with open(filepath, "rb") as f:
            content_bytes = f.read()
    except Exception as e:
        raise ValueError(f"Error reading file {filepath.name}: {e}")

    content = content_bytes.decode("utf-8")

    # Normalize line endings to LF
    content_lf = content.replace("\r\n", "\n")

    lines = content_lf.splitlines(keepends=True)
    if not lines or not lines[0].strip() == "---":
        # Backward Compatibility: Treat the entire file as the body text
        return {}, content_lf

    closing_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_idx = i
            break

    if closing_idx == -1:
        # Backward Compatibility: Treat the entire file as the body text
        return {}, content_lf

    frontmatter_lines = lines[1:closing_idx]
    body_lines = lines[closing_idx + 1 :]

    frontmatter_text = "".join(frontmatter_lines)
    body_text = "".join(body_lines)

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except Exception as e:
        raise ValueError(f"Failed to parse YAML frontmatter in {filepath.name}: {e}")

    if frontmatter is None:
        frontmatter = {}

    if not isinstance(frontmatter, dict):
        raise TypeError(
            f"Frontmatter in {filepath.name} must be a dictionary/YAML mapping."
        )

    # Predefined Strict Schema
    valid_keys = {"model", "temperature"}
    for key in frontmatter:
        if key not in valid_keys:
            raise ValueError(
                f"Invalid schema key '{key}' in frontmatter of {filepath.name}. "
                f"Valid keys are: {sorted(list(valid_keys))}"
            )

    if "model" in frontmatter:
        model_val = frontmatter["model"]
        if not isinstance(model_val, str):
            raise TypeError(
                f"Invalid type for key 'model' in {filepath.name}: "
                f"expected string, got {type(model_val).__name__}"
            )

    if "temperature" in frontmatter:
        temp_val = frontmatter["temperature"]
        # isinstance(True, int) is True, so check that it's not a boolean
        if isinstance(temp_val, bool) or not isinstance(temp_val, (int, float)):
            raise TypeError(
                f"Invalid type for key 'temperature' in {filepath.name}: "
                f"expected float or int, got {type(temp_val).__name__}"
            )
        if not (0.0 <= temp_val <= 2.0):
            raise ValueError(
                f"Value of key 'temperature' in {filepath.name} is out of bounds [0.0, 2.0]: got {temp_val}"
            )

    return frontmatter, body_text


def compute_sha256(filepath):
    """Compute the SHA-256 hash of decoupled and normalized prompt body text."""
    _, body_text = parse_and_validate_prompt(filepath)
    sha256_hash = hashlib.sha256()
    # Ensure line endings are normalized to LF
    normalized_body = body_text.replace("\r\n", "\n")
    sha256_hash.update(normalized_body.encode("utf-8"))
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
    try:
        hashes = get_hashes()
    except (ValueError, TypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="") as f:
        json.dump(hashes, f, indent=4, sort_keys=True)
        f.write("\n")
    print(f"Manifest successfully generated at {MANIFEST_PATH}")


def verify():
    """Verify that all agent prompt files match the hashes in manifest.json."""
    if not MANIFEST_PATH.exists():
        print(f"Error: Manifest file {MANIFEST_PATH} does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(MANIFEST_PATH, "rb") as f:
            manifest_bytes = f.read()
    except Exception as e:
        print(f"Error reading manifest file {MANIFEST_PATH}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        manifest_hashes = json.loads(manifest_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        print("Error: Manifest file is not valid JSON.", file=sys.stderr)
        sys.exit(1)

    try:
        current_hashes = get_hashes()
    except (ValueError, TypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mismatches = False

    # Check for missing files or mismatched hashes
    for filename, current_hash in current_hashes.items():
        if filename not in manifest_hashes:
            print(
                f"Error: File {filename} is missing from the manifest.", file=sys.stderr
            )
            mismatches = True
        elif manifest_hashes[filename] != current_hash:
            print(f"Error: Hash mismatch for {filename}.", file=sys.stderr)
            mismatches = True

    # Check for deleted files
    for filename in manifest_hashes:
        if filename not in current_hashes:
            print(
                f"Error: File {filename} is in the manifest but no longer exists.",
                file=sys.stderr,
            )
            mismatches = True

    if mismatches:
        print(
            "Verification failed. Some agent prompt files do not match the manifest.",
            file=sys.stderr,
        )
        print(
            "Please use 'uv run python scripts/prompt_manifest.py generate' to update the manifest.",
            file=sys.stderr,
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
