"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI.
"""

import logging

from app.config import LOG_FILE
from app.ui.app import run_app

# Configure Centralized Logger
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

if __name__ == "__main__":
    run_app()
