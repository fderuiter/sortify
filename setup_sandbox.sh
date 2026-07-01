#!/bin/bash
set -e
echo "Setting up the sandbox environment..."
uv run python3 sandbox_cli.py reset
echo "Sandbox setup complete. You can now use ./sandbox_cli.py to interact with the sandbox."
