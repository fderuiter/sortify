"""Utility for generating and verifying Ed25519-signed SHA-256 manifests for agent prompts."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

MANIFEST_PATH = Path(".github/AGENTS/manifest.json")
AGENTS_DIR = Path(".github/AGENTS")

# Pinned 32-byte raw Ed25519 public key (hex-encoded) for verifying .github/AGENTS/manifest.json.
# Guarded by CODEOWNERS and PROTECTED_PATHS in scripts/classify_pr.py.
MANIFEST_ED25519_PUB_HEX = (
    "e7c983e2526688dcf486e805f8df368242dbb8b206f0c51a1527478853e092e4"
)
PINNED_PUBLIC_KEY_HEX = MANIFEST_ED25519_PUB_HEX


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

    content = content_bytes.decode("utf-8-sig")

    # Normalize line endings to LF
    content_lf = content.replace("\r\n", "\n").replace("\r", "\n")

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


def compute_sha256(filepath: Path) -> str:
    """Compute the SHA-256 hash of decoupled and normalized prompt body text."""
    _, body_text = parse_and_validate_prompt(filepath)
    sha256_hash = hashlib.sha256()
    # Ensure line endings are normalized to LF
    normalized_body = body_text.replace("\r\n", "\n").replace("\r", "\n")
    sha256_hash.update(normalized_body.encode("utf-8"))
    return sha256_hash.hexdigest()


def get_hashes() -> dict[str, str]:
    """Retrieve SHA-256 hashes for all agent prompt files."""
    hashes: dict[str, str] = {}
    for filepath in sorted(AGENTS_DIR.glob("*")):
        if filepath.is_file() and filepath.name != "manifest.json":
            hashes[filepath.name] = compute_sha256(filepath)
    return hashes


def serialize_manifest_payload(hashes: dict[str, str]) -> bytes:
    """Serialize prompt content hashes sorted alphabetically into LF-normalized UTF-8 bytes."""
    sorted_files = sorted(f for f in hashes if f != "_signature")
    lines = [f"{f}:{hashes[f]}" for f in sorted_files]
    payload = "\n".join(lines)
    if lines:
        payload += "\n"
    normalized_payload = payload.replace("\r\n", "\n").replace("\r", "\n")
    return normalized_payload.encode("utf-8")


def _load_private_key(private_key_hex: str) -> Ed25519PrivateKey:
    """Parse a 64-character hex string into an Ed25519PrivateKey without echoing key material."""
    cleaned = private_key_hex.strip()
    try:
        raw_bytes = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(
            "Invalid Ed25519 private key: expected 64-character hexadecimal string."
        ) from exc
    if len(raw_bytes) != 32:
        raise ValueError(
            f"Invalid Ed25519 private key length: expected 32 bytes, got {len(raw_bytes)}."
        )
    try:
        return Ed25519PrivateKey.from_private_bytes(raw_bytes)
    except ValueError as exc:
        raise ValueError("Invalid Ed25519 private key bytes.") from exc


def _load_public_key(public_key_hex: str) -> Ed25519PublicKey:
    """Parse a 64-character hex string into an Ed25519PublicKey."""
    cleaned = public_key_hex.strip()
    try:
        raw_bytes = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(
            "Invalid Ed25519 public key: expected 64-character hexadecimal string."
        ) from exc
    if len(raw_bytes) != 32:
        raise ValueError(
            f"Invalid Ed25519 public key length: expected 32 bytes, got {len(raw_bytes)}."
        )
    try:
        return Ed25519PublicKey.from_public_bytes(raw_bytes)
    except ValueError as exc:
        raise ValueError("Invalid Ed25519 public key bytes.") from exc


def generate_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair and return (private_key_hex, public_key_hex)."""
    private_key = Ed25519PrivateKey.generate()
    private_hex = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    public_hex = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return private_hex, public_hex


def keygen() -> tuple[str, str]:
    """Generate an Ed25519 keypair and print hex-encoded private and public keys."""
    private_hex, public_hex = generate_keypair()
    print(f"PROMPT_MANIFEST_PRIVATE_KEY={private_hex}")
    print(f"PINNED_PUBLIC_KEY_HEX={public_hex}")
    return private_hex, public_hex


def sign_manifest_hashes(hashes: dict[str, str], private_key_hex: str) -> str:
    """Compute hex-encoded Ed25519 signature across all prompt content hashes sorted alphabetically."""
    private_key = _load_private_key(private_key_hex)
    payload = serialize_manifest_payload(hashes)
    return private_key.sign(payload).hex()


def verify_manifest_signature(
    hashes: dict[str, str],
    signature_hex: str,
    public_key_hex: str | None = None,
) -> bool:
    """Verify hex-encoded Ed25519 signature across sorted prompt content hashes."""
    resolved_pub = (
        public_key_hex if public_key_hex is not None else PINNED_PUBLIC_KEY_HEX
    )
    public_key = _load_public_key(resolved_pub)
    if len(signature_hex) != 128:
        return False
    try:
        signature_bytes = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    payload = serialize_manifest_payload(hashes)
    try:
        public_key.verify(signature_bytes, payload)
        return True
    except InvalidSignature:
        return False


def generate(private_key: str | None = None) -> None:
    """Generate the manifest.json file containing hashes of agent prompts and an Ed25519 signature."""
    resolved_private_key = (
        private_key
        if private_key is not None
        else os.environ.get("PROMPT_MANIFEST_PRIVATE_KEY")
    )
    if not resolved_private_key or not resolved_private_key.strip():
        print(
            "Error: PROMPT_MANIFEST_PRIVATE_KEY is required to generate the manifest signature.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        hashes = get_hashes()
        signature = sign_manifest_hashes(hashes, resolved_private_key)
    except (ValueError, TypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    manifest_data = {"_signature": signature, **hashes}

    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="") as f:
        json.dump(manifest_data, f, indent=4, sort_keys=True)
        f.write("\n")
    print(f"Manifest successfully generated at {MANIFEST_PATH}")


def verify(public_key: str | None = None, strict: bool = False) -> None:
    """Verify that all agent prompt files match the hashes in manifest.json and the Ed25519 signature is valid."""
    _ = strict  # Verification is unconditionally strict across local hooks and CI.
    resolved_public_key = (
        public_key if public_key is not None else PINNED_PUBLIC_KEY_HEX
    )
    if not resolved_public_key or not resolved_public_key.strip():
        print(
            "Error: Pinned Ed25519 public key is missing.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        _load_public_key(resolved_public_key)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

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
        manifest_data = json.loads(manifest_bytes.decode("utf-8-sig"))
    except json.JSONDecodeError:
        print("Error: Manifest file is not valid JSON.", file=sys.stderr)
        sys.exit(1)

    if not isinstance(manifest_data, dict):
        print("Error: Manifest file must be a JSON dictionary.", file=sys.stderr)
        sys.exit(1)

    try:
        current_hashes = get_hashes()
    except (ValueError, TypeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mismatches = False

    # Unconditionally verify Ed25519 signature
    if "_signature" not in manifest_data or manifest_data["_signature"] in (None, ""):
        print(
            "Error: Manifest signature '_signature' is missing.",
            file=sys.stderr,
        )
        mismatches = True
    else:
        manifest_signature = manifest_data["_signature"]
        if not isinstance(manifest_signature, str):
            print(
                "Error: Manifest signature '_signature' must be a string.",
                file=sys.stderr,
            )
            mismatches = True
        elif not verify_manifest_signature(
            current_hashes, manifest_signature, resolved_public_key
        ):
            print(
                "Error: Manifest Ed25519 signature verification failed.",
                file=sys.stderr,
            )
            mismatches = True

    # Check for missing files or mismatched hashes
    manifest_hashes = {
        filename: hsh
        for filename, hsh in manifest_data.items()
        if filename != "_signature"
    }

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
            "Please use 'PROMPT_MANIFEST_PRIVATE_KEY=<hex> uv run python scripts/prompt_manifest.py generate' to update the manifest.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print("Verification passed. All agent prompt files match the manifest.")


def main() -> None:
    """Execute the main entry point for the manifest utility."""
    parser = argparse.ArgumentParser(
        description="Agent Prompts Ed25519-Signed SHA-256 Manifest Utility"
    )
    parser.add_argument(
        "action",
        choices=["keygen", "generate", "verify"],
        help="Action to perform",
    )
    parser.add_argument(
        "--private-key",
        default=None,
        help="Hex-encoded Ed25519 private key for signing (defaults to PROMPT_MANIFEST_PRIVATE_KEY env var)",
    )
    parser.add_argument(
        "--public-key",
        default=None,
        help="Hex-encoded Ed25519 public key for verification (defaults to PINNED_PUBLIC_KEY_HEX)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce strict signature and manifest verification",
    )

    args = parser.parse_args()

    if args.action == "keygen":
        keygen()
    elif args.action == "generate":
        generate(private_key=args.private_key)
    elif args.action == "verify":
        verify(public_key=args.public_key, strict=args.strict)


if __name__ == "__main__":
    main()
