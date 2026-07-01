"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI.
"""

import logging

from app.config import settings
from app.ui.app import run_app

# Configure Centralized Logger
logging.basicConfig(
    filename=settings.LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

def main():
    """Execute the main application GUI."""
    run_app()

if __name__ == "__main__":
    main()
