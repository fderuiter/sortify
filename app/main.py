"""Entry point for the Smart AutoSorter AI Pro application.

This script imports and runs the main application GUI or CLI demo.
"""

import argparse
import logging
from pathlib import Path

from app.config import AppSettings
from app.log_filter import LogScrubbingFilter


def main():
    """Execute the main application GUI or Demo."""
    settings = AppSettings()

    # Configure Centralized Logger
    logging.basicConfig(
        filename=settings.LOG_FILE,
        level=logging.ERROR,
        format="%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    )

    # Create and add the log scrubbing filter to the root logger
    root_logger = logging.getLogger()

    # Also apply to handlers to ensure child loggers are filtered
    scrubber = LogScrubbingFilter(str(Path.home()))
    root_logger.addFilter(scrubber)
    for handler in root_logger.handlers:
        handler.addFilter(scrubber)

    parser = argparse.ArgumentParser(description="Smart AutoSorter AI Pro")
    parser.add_argument(
        "--demo", action="store_true", help="Run interactive CLI demo mode"
    )
    parser.add_argument(
        "directory", nargs="?", default=None, help="Directory to analyze automatically"
    )

    args = parser.parse_args()

    if args.demo:
        from app.demo import run_demo

        run_demo(settings)
    else:
        from app.ui.app import run_app

        run_app(settings, args.directory)


if __name__ == "__main__":
    main()
