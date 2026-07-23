"""Utility for validating documentation manifest and detecting documentation drift."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Add project root to sys.path so we can import scripts.generate_docs
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MANIFEST_PATH = PROJECT_ROOT / "docs" / "doc_manifest.json"


def compute_sha256(filepath):
    """Compute the SHA-256 hash of a file with line-ending normalization."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        content = f.read().replace(b"\r\n", b"\n")
        sha256_hash.update(content)
    return sha256_hash.hexdigest()


def run_generation():
    """Run all document generation tasks and return a set of normalized relative paths."""
    import scripts.validate_links
    original_validate_url = scripts.validate_links.validate_url

    # Mock validate_url to prevent network calls and ensure fast, offline validation
    scripts.validate_links.validate_url = lambda url, bypass_domains: (
        True,
        "Offline Mode (Bypassed)",
        False,
    )

    try:
        from scripts.generate_docs import (
            generate_admin_guide,
            generate_api_docs,
            generate_ui_docs,
            update_security_md,
        )

        generated_files = []

        # Run each generator and collect returned lists of paths
        generators = [
            ("generate_api_docs", generate_api_docs),
            ("generate_ui_docs", generate_ui_docs),
            ("generate_admin_guide", generate_admin_guide),
            ("update_security_md", update_security_md),
        ]

        for name, func in generators:
            try:
                paths = func()
                if paths:
                    for p in paths:
                        # Normalize separator to forward slash and ensure relative path
                        norm_p = Path(p).as_posix()
                        generated_files.append(norm_p)
            except Exception as e:
                sys.stderr.write(f"Error executing {name}: {e}\n")
                raise e

        return set(generated_files)
    finally:
        scripts.validate_links.validate_url = original_validate_url


def generate():
    """Generate the docs/doc_manifest.json file containing hashes of all generated documentation."""
    print("Generating fresh documentation...")
    generated_files = run_generation()

    hashes = {}
    for filepath_str in sorted(generated_files):
        filepath = PROJECT_ROOT / filepath_str
        if not filepath.exists():
            sys.stderr.write(f"Error: Generated file {filepath_str} not found on disk.\n")
            sys.exit(1)
        hashes[filepath_str] = compute_sha256(filepath)

    # Ensure parent directory exists
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(hashes, f, indent=4, sort_keys=True)
        f.write("\n")

    print(f"Manifest successfully generated at {MANIFEST_PATH.relative_to(PROJECT_ROOT)}")


def verify():
    """Verify that generated documentation is in sync with code settings and manifest."""
    if not MANIFEST_PATH.exists():
        sys.stderr.write(
            f"Error: Manifest file {MANIFEST_PATH.relative_to(PROJECT_ROOT)} does not exist.\n"
        )
        sys.stderr.write("Please run: uv run python scripts/validate_docs.py generate\n")
        sys.exit(1)

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        try:
            manifest_hashes = json.load(f)
        except json.JSONDecodeError:
            sys.stderr.write(
                f"Error: Manifest file {MANIFEST_PATH.relative_to(PROJECT_ROOT)} is not valid JSON.\n"
            )
            sys.exit(1)

    # Compute pre-existing hashes of all manifest-listed files
    pre_existing_hashes = {}
    for filepath_str in manifest_hashes:
        filepath = PROJECT_ROOT / filepath_str
        if filepath.exists():
            pre_existing_hashes[filepath_str] = compute_sha256(filepath)
        else:
            pre_existing_hashes[filepath_str] = None

    print("Regenerating documentation for verification...")
    # Run generators to produce fresh files
    generated_files = run_generation()

    # Compute fresh hashes
    fresh_hashes = {}
    for filepath_str in generated_files:
        filepath = PROJECT_ROOT / filepath_str
        if filepath.exists():
            fresh_hashes[filepath_str] = compute_sha256(filepath)
        else:
            fresh_hashes[filepath_str] = None

    errors = []

    # Check 1: Unlisted Generated Files (No silent failures)
    for filepath_str in generated_files:
        if filepath_str not in manifest_hashes:
            errors.append(
                f"Unlisted Generated File: '{filepath_str}' is generated/updated but NOT registered in the manifest."
            )

    # Check 2: Orphaned Manifest Entries
    for filepath_str in manifest_hashes:
        if filepath_str not in generated_files:
            errors.append(
                f"Orphaned Manifest Entry: '{filepath_str}' is in the manifest but was NOT generated/updated."
            )

    # Check 3: Pre-commit Out of Sync (Developer changed settings but didn't run generate)
    # Check 4: Manifest Out of Sync (Manifest doesn't match the freshly generated documentation hashes)
    for filepath_str in generated_files:
        if filepath_str in manifest_hashes:
            pre_hash = pre_existing_hashes.get(filepath_str)
            fresh_hash = fresh_hashes.get(filepath_str)
            expected_manifest_hash = manifest_hashes[filepath_str]

            if pre_hash != fresh_hash:
                errors.append(
                    f"Pre-commit Out of Sync: '{filepath_str}' is out of sync with code settings.\n"
                    f"  The developer modified configuration or code but did not regenerate/commit the documentation.\n"
                    f"  Please run documentation generation and commit the updated files."
                )
            elif fresh_hash != expected_manifest_hash:
                errors.append(
                    f"Manifest Out of Sync: '{filepath_str}' has fresh hash {fresh_hash} but manifest records {expected_manifest_hash}.\n"
                    f"  Please update the manifest by running: uv run python scripts/validate_docs.py generate"
                )

    if errors:
        sys.stderr.write("Documentation Validation Failed!\n\n")
        for error in errors:
            sys.stderr.write(f"Error: {error}\n\n")
        sys.stderr.write(
            "To fix synchronization issues, run:\n"
            "  uv run python scripts/validate_docs.py generate\n"
            "And ensure you stage and commit all modified documentation files and the manifest.\n"
        )
        sys.exit(1)

    print("Verification passed! All generated files are in sync with the manifest and code configurations.")


def main():
    """Parse command line arguments and execute the requested action."""
    parser = argparse.ArgumentParser(description="Documentation Manifest Utility")
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
