"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI.
"""

import logging
import sys

from pydantic import ValidationError

from app.config import AppSettings
from app.ui.app import run_app


def main():
    """Execute the main application GUI."""
    try:
        settings = AppSettings()
    except ValidationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Configure Centralized Logger
    logging.basicConfig(
        filename=settings.LOG_FILE,
        level=logging.ERROR,
        format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    )
    
    run_app(settings)

if __name__ == "__main__":
    main()
