#!/usr/bin/env python3
"""Validate the application configuration schema and build system config."""

import json
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


try:
    validate_build_system_config()

    from app.config import Settings

    # Instantiate to ensure default schema is valid
    Settings()

    schema = Settings.model_json_schema()
    # Normalize LOG_FILE default to use a placeholder or remove it to avoid user-specific paths
    if (
        "LOG_FILE" in schema["properties"]
        and "default" in schema["properties"]["LOG_FILE"]
    ):
        schema["properties"]["LOG_FILE"]["default"] = "autosorter.log"

    schema_path = repo_root / "app" / "config_schema.json"

    existing_schema = None
    if schema_path.exists():
        with open(schema_path, "r", encoding="utf-8") as f:
            existing_schema = json.load(f)

    if existing_schema != schema:
        with open(schema_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(schema, f, indent=2)
            f.write("\n")
        print(
            "Configuration schema updated. Please commit the changes.", file=sys.stderr
        )
        sys.exit(1)

    print(
        "Configuration schema and build-system requirements are valid and up-to-date."
    )
    sys.exit(0)
except ValidationError as e:
    print(f"Configuration schema validation failed:\n{e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Unexpected error validating configuration schema: {e}", file=sys.stderr)
    sys.exit(1)
