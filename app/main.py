"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI.
"""

import logging

from app.config import AppSettings
from app.ui.app import run_app


def main():
    """Execute the main application GUI."""
    settings = AppSettings()
    
    # Configure Centralized Logger
    logging.basicConfig(
        filename=settings.LOG_FILE,
        level=logging.ERROR,
        format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    )
    
    run_app(settings)

if __name__ == "__main__":
    main()
