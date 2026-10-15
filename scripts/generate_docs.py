"""Backward-compatibility wrapper for documentation generator."""

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli.generate_docs import *  # noqa: F403, F401
from app.cli.generate_docs import main

if __name__ == "__main__":
    main()
