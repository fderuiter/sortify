#!/usr/bin/env python3
"""Validate the application configuration schema and build system config."""

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

from pydantic import ValidationError

# Resolve repository root and bootstrap sys.path
repo_root = Path(__file__).resolve().parent.parent
str_repo_root = str(repo_root)
if str_repo_root in sys.path:
    sys.path.remove(str_repo_root)
sys.path.insert(0, str_repo_root)


def validate_build_system_config():
    """Validate that build-system.requires in pyproject.toml is properly bounded."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.exists():
        print("pyproject.toml not found.", file=sys.stderr)
        sys.exit(1)

    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)

    build_system = pyproject_data.get("build-system", {})
    requires = build_system.get("requires", [])

    hatchling_req = None
    for req in requires:
        if "hatchling" in req:
            hatchling_req = req
            break

    if not hatchling_req:
        print(
            "Error: hatchling build system requirement missing in pyproject.toml",
            file=sys.stderr,
        )
        sys.exit(1)

    if ">=1.27.0" not in hatchling_req or "<2.0.0" not in hatchling_req:
        print(
            f"Error: hatchling build system requirement '{hatchling_req}' must specify version bounds '>=1.27.0, <2.0.0'",
            file=sys.stderr,
        )
        sys.exit(1)


EXPECTED_POLICY_TYPES = {
    "keyword",
    "pattern",
    "override",
    "action",
    "compliance",
    "unarchive",
    "duplicate",
    "age",
    "modified_date",
    "created_date",
    "exif_date",
    "size",
}
EXPECTED_POLICY_ACTIONS = {
    "archive",
    "retain",
    "quarantine",
    "redact",
    "unarchive",
    "trash",
}
EXPECTED_POST_UNARCHIVE_ACTIONS = {
    "delete",
    "remove",
    "cleanup",
    "move",
    "archive",
    "relocate",
    "retain",
    "keep",
}


def _resolve_schema_ref(schema: dict, subschema: dict) -> dict:
    """Resolve a local JSON Schema $ref against top-level $defs if present."""
    ref = subschema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        def_name = ref[len("#/$defs/") :]
        return schema.get("$defs", {}).get(def_name, subschema)
    return subschema


def validate_schema_completeness(schema: dict) -> None:
    """Verify that every Settings property defines a description and POLICIES items are typed."""
    properties = schema.get("properties", {})
    if not properties:
        raise ValueError("Configuration schema contains no properties.")

    missing_descriptions = [
        key
        for key, prop in properties.items()
        if not isinstance(prop.get("description"), str)
        or not prop["description"].strip()
    ]
    if missing_descriptions:
        raise ValueError(
            "Configuration schema is missing descriptions for properties: "
            + ", ".join(sorted(missing_descriptions))
        )

    policies_prop = properties.get("POLICIES")
    if not isinstance(policies_prop, dict) or policies_prop.get("type") != "array":
        raise ValueError("Configuration schema must define 'POLICIES' as an array.")

    items_schema = _resolve_schema_ref(schema, policies_prop.get("items", {}))
    item_props = items_schema.get("properties")
    if not isinstance(item_props, dict):
        raise ValueError(
            "Configuration schema 'POLICIES.items' must define typed object properties."
        )

    required_keys = set(items_schema.get("required", []))
    expected_required = {"type", "expression", "target_path", "priority"}
    if not expected_required.issubset(required_keys):
        raise ValueError(
            f"POLICIES.items required keys {sorted(required_keys)} missing "
            f"{sorted(expected_required - required_keys)}."
        )

    expected_optional = {
        "halting",
        "enabled",
        "action",
        "detect_original_by",
        "post_unarchive_action",
        "rename_pattern",
        "match_mode",
        "conditions",
        "negate",
        "regex",
        "case_sensitive",
    }
    missing_optional = expected_optional - set(item_props.keys())
    if missing_optional:
        raise ValueError(
            f"POLICIES.items properties missing optional keys: {sorted(missing_optional)}."
        )

    type_enum = set(
        _resolve_schema_ref(schema, item_props.get("type", {})).get("enum", [])
    )
    if type_enum != EXPECTED_POLICY_TYPES:
        raise ValueError(
            f"POLICIES.items.type enum {sorted(type_enum)} does not match {sorted(EXPECTED_POLICY_TYPES)}."
        )

    action_enum = set(
        _resolve_schema_ref(schema, item_props.get("action", {})).get("enum", [])
    )
    if action_enum != EXPECTED_POLICY_ACTIONS:
        raise ValueError(
            f"POLICIES.items.action enum {sorted(action_enum)} does not match {sorted(EXPECTED_POLICY_ACTIONS)}."
        )

    post_enum = set(
        _resolve_schema_ref(schema, item_props.get("post_unarchive_action", {})).get(
            "enum", []
        )
    )
    if post_enum != EXPECTED_POST_UNARCHIVE_ACTIONS:
        raise ValueError(
            f"POLICIES.items.post_unarchive_action enum {sorted(post_enum)} does not match "
            f"{sorted(EXPECTED_POST_UNARCHIVE_ACTIONS)}."
        )


def generate_config_schema() -> dict:
    """Generate normalized JSON schema for application Settings."""
    from app.config import Settings

    saved_env = {
        key: os.environ.pop(key)
        for key in list(os.environ)
        if key == "AUTOSORTER_APP_DIR" or key.startswith("SORTIFY_")
    }
    try:
        # Instantiate isolated from host env/.env to ensure default schema is valid
        Settings(_env_file=None)
        schema = Settings.model_json_schema()
    finally:
        for key in [
            k
            for k in list(os.environ)
            if k == "AUTOSORTER_APP_DIR" or k.startswith("SORTIFY_")
        ]:
            os.environ.pop(key, None)
        os.environ.update(saved_env)

    # Normalize LOG_FILE default to use a placeholder or remove it to avoid user-specific paths
    if (
        "LOG_FILE" in schema.get("properties", {})
        and "default" in schema["properties"]["LOG_FILE"]
    ):
        schema["properties"]["LOG_FILE"]["default"] = "autosorter.log"

    return schema


def sync_config_schema(
    schema_path: Path | None = None,
    check_only: bool = False,
) -> bool:
    """Synchronize or verify app/config_schema.json against runtime Settings schema.

    When check_only is False (default), writes updated JSON to schema_path and returns True
    if the file was modified or newly created, False if already up to date.
    When check_only is True, compares in memory without touching disk and returns True if
    schema_path is missing, unreadable, or out of sync with the generated schema.
    """
    if schema_path is None:
        schema_path = repo_root / "app" / "config_schema.json"

    schema = generate_config_schema()
    validate_schema_completeness(schema)
    expected_text = json.dumps(schema, indent=2) + "\n"

    existing_schema = None
    existing_text = None
    if schema_path.exists():
        try:
            raw_text = schema_path.read_text(encoding="utf-8")
            existing_text = raw_text.replace("\r\n", "\n")
            existing_schema = json.loads(existing_text)
        except Exception:
            existing_schema = None
            existing_text = None

    is_drifted = existing_schema != schema or existing_text != expected_text
    if is_drifted:
        if check_only:
            return True
        schema_path.parent.mkdir(parents=True, exist_ok=True)
        with open(schema_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(expected_text)
        return True

    return False


def main(argv: list[str] | None = None) -> int:
    """Validate configuration schema and build system requirements."""
    parser = argparse.ArgumentParser(
        description="Validate or synchronize app/config_schema.json and build-system bounds."
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration schema in memory without modifying files on disk.",
    )
    mode_group.add_argument(
        "--sync",
        action="store_true",
        help="Synchronize app/config_schema.json on disk with runtime Settings.",
    )
    args = parser.parse_args(argv)
    check_only = not args.sync

    try:
        validate_build_system_config()

        schema_path = repo_root / "app" / "config_schema.json"
        drifted_or_updated = sync_config_schema(schema_path, check_only=check_only)

        if check_only:
            if drifted_or_updated:
                print(
                    "Error: Configuration schema (app/config_schema.json) is out of sync with app/config.py. "
                    "Run 'uv run python scripts/validate_config.py --sync' and commit the changes.",
                    file=sys.stderr,
                )
                return 1
            print(
                "Configuration schema and build-system requirements are valid and up-to-date."
            )
            return 0

        if drifted_or_updated:
            print("Configuration schema synchronized at app/config_schema.json.")
        else:
            print(
                "Configuration schema and build-system requirements are valid and up-to-date."
            )
        return 0
    except ValidationError as e:
        print(f"Configuration schema validation failed:\n{e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Unexpected error validating configuration schema: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
