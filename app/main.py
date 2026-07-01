"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI or CLI demo.
"""

import argparse
import logging

from app.config import settings

# Configure Centralized Logger
logging.basicConfig(
    filename=settings.LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
)

def main():
    """Execute the main application GUI or Demo."""
    parser = argparse.ArgumentParser(description="Smart AutoSorter AI Pro")
    parser.add_argument("--demo", action="store_true", help="Run interactive CLI demo mode")
    
    args = parser.parse_args()
    
    if args.demo:
        from app.demo import run_demo
        run_demo()
    else:
        from app.ui.app import run_app
        run_app()

if __name__ == "__main__":
    main()
