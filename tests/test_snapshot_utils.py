"""Unit tests for shared snapshot validation and hashing utilities."""

import hashlib
import json

from scripts.snapshot_utils import (
    compute_payload_checksum,
    extract_metadata_and_payload,
    is_conflicted_json_payload,
)


def test_compute_payload_checksum_canonical():
    """Verify checksum is deterministic and canonical regardless of key order."""
    payload1 = {
        "b": 2,
        "a": 1,
        "nested": {"z": 26, "y": 25},
    }
    payload2 = {
        "a": 1,
        "nested": {"y": 25, "z": 26},
        "b": 2,
    }
    checksum1 = compute_payload_checksum(payload1)
    checksum2 = compute_payload_checksum(payload2)

    assert checksum1 == checksum2
    assert len(checksum1) == 64
    assert int(checksum1, 16) > 0

    # Ensure canonical serialization matches sha256 of formatted json
    expected_json = json.dumps(payload1, indent=2, sort_keys=True)
    expected_checksum = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    assert checksum1 == expected_checksum


def test_compute_payload_checksum_empty_dict():
    """Verify checksum computation for empty dict."""
    checksum = compute_payload_checksum({})
    assert len(checksum) == 64
    expected = hashlib.sha256(b"{}").hexdigest()
    assert checksum == expected


def test_extract_metadata_and_payload_with_metadata():
    """Verify extraction of standard _metadata header."""
    data = {
        "_metadata": {"checksum": "abc123", "version": 1},
        "classes": [{"name": "A"}],
        "functions": [],
    }
    meta, payload = extract_metadata_and_payload(data)
    assert meta == {"checksum": "abc123", "version": 1}
    assert payload == {"classes": [{"name": "A"}], "functions": []}
    assert "_metadata" not in payload


def test_extract_metadata_and_payload_legacy_metadata():
    """Verify extraction of legacy metadata header."""
    data = {
        "metadata": {"checksum": "xyz789"},
        "classes": [],
        "functions": [{"name": "foo"}],
    }
    meta, payload = extract_metadata_and_payload(data)
    assert meta == {"checksum": "xyz789"}
    assert payload == {"classes": [], "functions": [{"name": "foo"}]}
    assert "metadata" not in payload


def test_extract_metadata_and_payload_precedence():
    """Verify _metadata takes precedence over legacy metadata when both exist."""
    data = {
        "_metadata": {"checksum": "new"},
        "metadata": {"checksum": "old"},
        "classes": [],
    }
    meta, payload = extract_metadata_and_payload(data)
    assert meta == {"checksum": "new"}
    assert payload == {"classes": []}
    assert "_metadata" not in payload
    assert "metadata" not in payload


def test_extract_metadata_and_payload_missing_metadata():
    """Verify missing metadata returns an empty dict rather than None to prevent attribute errors."""
    data = {
        "classes": [{"name": "B"}],
        "functions": [],
    }
    meta, payload = extract_metadata_and_payload(data)
    assert meta == {}
    assert isinstance(meta, dict)
    # Safe attribute/method lookups without AttributeError
    assert meta.get("checksum") is None
    assert meta.get("sha256") is None
    # Boolean check for missing header evaluates to False
    assert not meta
    assert payload == {"classes": [{"name": "B"}], "functions": []}


def test_extract_metadata_and_payload_edge_cases():
    """Verify extraction with non-dict snapshot_data and invalid metadata values."""
    assert extract_metadata_and_payload(None) == ({}, {})
    assert extract_metadata_and_payload([]) == ({}, {})
    assert extract_metadata_and_payload("string") == ({}, {})
    assert extract_metadata_and_payload(42) == ({}, {})

    # Non-dict _metadata or metadata values
    data_none_meta = {"_metadata": None, "classes": []}
    meta, payload = extract_metadata_and_payload(data_none_meta)
    assert meta == {}
    assert payload == {"classes": []}

    data_str_meta = {"_metadata": "not_a_dict", "classes": []}
    meta, payload = extract_metadata_and_payload(data_str_meta)
    assert meta == {}
    assert payload == {"classes": []}


def test_is_conflicted_json_payload():
    """Verify conflict detection for various structured conflict representations."""
    # Structured conflict in _metadata
    assert is_conflicted_json_payload({"_metadata": {"conflict": True}}) is True
    # Structured conflict in legacy metadata
    assert is_conflicted_json_payload({"metadata": {"conflict": True}}) is True
    # Top-level conflict flag
    assert is_conflicted_json_payload({"conflict": True}) is True
    # Local and incoming keys
    assert is_conflicted_json_payload({"local": {}, "incoming": {}}) is True
    # Ours and theirs keys
    assert is_conflicted_json_payload({"ours": {}, "theirs": {}}) is True

    # Normal payloads are not conflicted
    assert (
        is_conflicted_json_payload({"_metadata": {"checksum": "abc"}, "classes": []})
        is False
    )
    assert is_conflicted_json_payload({"classes": [], "functions": []}) is False
    assert is_conflicted_json_payload({}) is False

    # Edge cases: non-dict inputs or invalid _metadata value
    assert is_conflicted_json_payload(None) is False
    assert is_conflicted_json_payload([]) is False
    assert is_conflicted_json_payload("not a json dict") is False
    assert is_conflicted_json_payload({"_metadata": None}) is False
    assert is_conflicted_json_payload({"_metadata": "str"}) is False
    assert is_conflicted_json_payload({"_metadata": {"conflict": False}}) is False


def test_backwards_compatible_reexports():
    """Verify that validate_signatures re-exports shared snapshot helpers."""
    from scripts import snapshot_utils, validate_signatures

    assert (
        validate_signatures.compute_payload_checksum
        is snapshot_utils.compute_payload_checksum
    )
    assert (
        validate_signatures.extract_metadata_and_payload
        is snapshot_utils.extract_metadata_and_payload
    )
    assert (
        validate_signatures.is_conflicted_json_payload
        is snapshot_utils.is_conflicted_json_payload
    )
