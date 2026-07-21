#!/usr/bin/env python3
"""Validate the application configuration schema and export it."""

import sys
import json
import os
from pathlib import Path

from pydantic import ValidationError

try:
    from app.config import Settings
    # Instantiate to ensure default schema is valid
    Settings()
    
    schema = Settings.model_json_schema()
    # Normalize LOG_FILE default to use a placeholder or remove it to avoid user-specific paths
    if "LOG_FILE" in schema["properties"] and "default" in schema["properties"]["LOG_FILE"]:
        schema["properties"]["LOG_FILE"]["default"] = "autosorter.log"
    
    schema_path = Path("app/config_schema.json")
    
    existing_schema = None
    if schema_path.exists():
        with open(schema_path, "r", encoding="utf-8") as f:
            existing_schema = json.load(f)
            
    if existing_schema != schema:
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2)
            f.write("\n")
        print("Configuration schema updated. Please commit the changes.", file=sys.stderr)
        sys.exit(1)
        
    print("Configuration schema is valid and up-to-date.")
    sys.exit(0)
except ValidationError as e:
    print(f"Configuration schema validation failed:\n{e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Unexpected error validating configuration schema: {e}", file=sys.stderr)
    sys.exit(1)
