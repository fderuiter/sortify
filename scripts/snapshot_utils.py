"""Shared snapshot validation and hashing helpers."""

import hashlib
import json

__all__ = [
    "compute_payload_checksum",
    "extract_metadata_and_payload",
    "is_conflicted_json_payload",
]


def compute_payload_checksum(payload: dict) -> str:
    """Compute SHA-256 checksum of canonical JSON payload definitions."""
    canonical_json = json.dumps(payload, indent=2, sort_keys=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def extract_metadata_and_payload(snapshot_data: dict) -> tuple[dict, dict]:
    """Separate metadata header from definition payload dictionary.

    Returns an empty dictionary when metadata is absent so callers avoid attribute errors.
    """
    metadata: dict = {}
    if not isinstance(snapshot_data, dict):
        return metadata, {}

    if "_metadata" in snapshot_data and isinstance(snapshot_data["_metadata"], dict):
        metadata = snapshot_data["_metadata"]
    elif "metadata" in snapshot_data and isinstance(snapshot_data["metadata"], dict):
        metadata = snapshot_data["metadata"]

    payload = {
        k: v for k, v in snapshot_data.items() if k not in ("_metadata", "metadata")
    }
    return metadata, payload


def is_conflicted_json_payload(snapshot_data: dict) -> bool:
    """Check if snapshot JSON data dictionary represents a structured conflict payload."""
    if not isinstance(snapshot_data, dict):
        return False
    meta = snapshot_data.get("_metadata")
    if isinstance(meta, dict) and meta.get("conflict") is True:
        return True
    legacy_meta = snapshot_data.get("metadata")
    if isinstance(legacy_meta, dict) and legacy_meta.get("conflict") is True:
        return True
    if snapshot_data.get("conflict") is True:
        return True
    if "local" in snapshot_data and "incoming" in snapshot_data:
        return True
    if "ours" in snapshot_data and "theirs" in snapshot_data:
        return True
    return False
