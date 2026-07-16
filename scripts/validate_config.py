#!/usr/bin/env python3
"""Validate the application configuration schema."""

import sys

from pydantic import ValidationError

try:
    from app.config import Settings
    # Instantiate to ensure default schema is valid
    Settings()
    print("Configuration schema is valid.")
    sys.exit(0)
except ValidationError as e:
    print(f"Configuration schema validation failed:\n{e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"Unexpected error validating configuration schema: {e}", file=sys.stderr)
    sys.exit(1)
